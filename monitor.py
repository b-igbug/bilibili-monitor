#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站 UP主 动态监控 -> 飞书推送
适用环境：GitHub Actions 定时运行（电脑关机也不影响），也可在任何可联网的常开设备上运行。

工作原理：
  1. 每次运行拉取监控列表里每位 UP 主的最新一条动态
  2. 与 state.json 中记录的上次动态ID对比
  3. 出现新动态 -> 通过飞书群机器人 webhook 推送
  4. 更新 state.json，作为下次对比的基线
  5. 首次运行（state 无记录）只建立基线，不推送，避免把历史动态刷屏

安全设计：
  - 只有"成功抓到动态 且 确认是新动态"才会推送，抓取失败（B站风控等）不推送、不改状态，
    下一轮自动重试，最多是通知延迟，绝不会误报。
"""
import json
import hashlib
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------- curl_cffi：模拟 Chrome TLS 指纹（绕过 B站 对机房 IP/脚本指纹的 412 风控） ----------
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

# ============ 配置区 ============
# 飞书群机器人 webhook 地址
# 优先读环境变量 FEISHU_WEBHOOK；为空时回退到下面内置的地址
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK") or "https://open.feishu.cn/open-apis/bot/v2/hook/87fb144d-0845-435b-aedf-f85acabf3b74"

# 监控的 UP 主列表 (uid: 用户主页 space.bilibili.com/ 后面的数字)
UP_LIST = [
    {"uid": "3706962053172003", "name": "我走路带风风风"},
    {"uid": "3747571396775777", "name": "站在黎明"},
]

# 状态文件（保存每个 UP 主最近一条动态ID）
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# B站接口地址
DYNAMIC_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
SPI_API = "https://api.bilibili.com/x/frontend/finger/spi"

# 必须带上 features=itemOpusStyle 等参数，B站才会以 OPUS 格式返回完整文字内容
# （否则文字/图文动态返回的是被置空的旧 DRAW 格式）
BILI_FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,forwardListHidden,decorationCard,commentsNewVersion,onlyfansAssetsV2,ugcDelete,onlyfansQaCard,avatarAutoTheme,sunflowerStyle,cardsEnhance,eva3CardOpus,eva3CardVideo,eva3CardComment,eva3CardUser"
DM_IMG_PARAMS = {
    "web_location": "333.1387",
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HTEUgKEludGVsLCBJbnRlbChSKSBBcmMoVE0pIEdyYXBoaWNzICgweDAwMDA3RDU1KSBEaXJlY3QzRDExIHZzXzVfMCBwc181XzAsIEQzRDExKUdvb2dsZSBJbmMuIChJbnRlbC",
    "dm_img_inter": '{"ds":[],"wh":[4581,4952,17],"of":[236,472,236]}',
    "x-bili-locale-json": '{"c_locale":{"language":"zh","script":"Hans"},"always_translate":false}',
    "x-bili-device-req-json": '{"platform":"web","device":"pc","spmid":"333.1387","mobi_app":"web_cn"}',
}

DEBUG = os.environ.get("DEBUG", "1") == "1"

# 可选：B站登录态 cookie（SESSDATA），能显著提升风控通过率（需配合 GitHub Secrets）
BILI_SESSDATA = os.environ.get("BILI_SESSDATA", "").strip()


def log(msg):
    if DEBUG:
        print("[debug]", msg)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# ==============================

# curl_cffi 的 Chrome 模拟目标（新→旧逐个尝试）
_IM_PERSONATES = ["chrome131", "chrome124", "chrome120", "chrome116", "chrome110", "chrome"]
_WORKING_IMPERSONATE = None

# B站 WBI 签名置换表（固定值）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _cffi_get_json(url, cookie=None, referer="https://www.bilibili.com/", timeout=20):
    """用 curl_cffi（Chrome TLS 指纹）请求，返回 (dict, err)"""
    global _WORKING_IMPERSONATE
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    imps = [_WORKING_IMPERSONATE] if _WORKING_IMPERSONATE else _IM_PERSONATES
    last_err = None
    for imp in imps:
        if not imp:
            continue
        try:
            r = cffi_requests.get(url, headers=headers, impersonate=imp, timeout=timeout)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            _WORKING_IMPERSONATE = imp
            return r.json(), None
        except Exception as e:
            last_err = f"cffi({imp}): {type(e).__name__}: {e}"
    return None, last_err or "cffi all-failed"


def http_get_json(url, cookie=None, referer="https://www.bilibili.com/", timeout=20):
    """带可选 cookie 的 GET，返回 (解析后的dict, 错误信息)
    优先 curl_cffi（Chrome 指纹），不可用时回退 urllib。"""
    if HAS_CFFI:
        data, err = _cffi_get_json(url, cookie=cookie, referer=referer, timeout=timeout)
        if data is not None:
            return data, None
        log(f"cffi 请求失败降级 urllib: {err}")
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def get_buvid_cookie():
    """从 finger/spi 接口获取 buvid3/buvid4（无需登录），用于降低 B站 风控"""
    parts = []
    data, err = http_get_json(SPI_API, referer="https://www.bilibili.com/")
    if err:
        log(f"spi 接口失败: {err}")
    elif not data or data.get("code") != 0:
        log(f"spi 接口返回异常: code={data.get('code') if data else 'None'}")
    else:
        b3 = (data.get("data") or {}).get("b_3") or ""
        b4 = (data.get("data") or {}).get("b_4") or ""
        if b3:
            log(f"buvid3 已获取: {b3[:8]}...")
            parts.append(f"buvid3={b3}; buvid4={b4}")
    # 可选登录态：SESSDATA（存在时大幅提升过风控率）
    if BILI_SESSDATA:
        parts.append(f"SESSDATA={BILI_SESSDATA}")
        log("已附加登录态 SESSDATA")
    return "; ".join(parts) if parts else None


# ---------- WBI 签名 ----------
def get_mixin_key(orig):
    """用置换表从 img_key+sub_key 生成 mixin_key（取前32位）"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def get_wbi_keys(cookie=None):
    """从 nav 接口获取 img_key / sub_key
    注意：nav 未登录时返回 code=-101，但 wbi_img 仍会正常返回，所以只检查 wbi_img。"""
    data, err = http_get_json(NAV_API, cookie=cookie)
    if err:
        log(f"nav 接口失败: {err}")
        return None
    wbi = ((data or {}).get("data") or {}).get("wbi_img")
    if not wbi or not wbi.get("img_url"):
        log(f"nav 未返回 wbi_img (code={data.get('code') if data else 'None'})")
        return None
    img_key = wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[1].split(".")[0]
    return img_key, sub_key


def enc_wbi(params, img_key, sub_key):
    """给参数加 wts 时间戳并计算 w_rid 签名"""
    mixin_key = get_mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {}
    for k, v in params.items():
        filtered[k] = "".join(ch for ch in str(v) if ch not in "!'()*")
    query = urllib.parse.urlencode(filtered)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    filtered["w_rid"] = w_rid
    return filtered


# ---------- 抓取动态 ----------
def fetch_dynamics(uid, attempt=0):
    """
    返回按 id 升序的动态列表 [{id, ts, text, link}]；
    抓取失败/被风控返回 None（调用方不推送、不改状态）。
    内置多轮重试（每轮重新获取 cookie/密钥）。
    """
    cookie = get_buvid_cookie()
    keys = get_wbi_keys(cookie=cookie)
    if not keys:
        if attempt < 2:
            time.sleep(3)
            return fetch_dynamics(uid, attempt + 1)
        return None
    img_key, sub_key = keys

    # 关键：features=itemOpusStyle 等参数，否则文字动态拿不到内容
    # 海外机房 IP（如 GitHub Actions）常被B站拦截(412)，因此采用多档策略：
    #   V1 完整浏览器参数（可拿全文）→ V2 精简+features → V3 最简（能拿动态ID/视频标题/转发文字）
    variants = [
        True,   # V1: 完整浏览器参数
        False,  # V2: 仅 features
        None,   # V3: 最简参数
    ]
    data = None
    for vi, full in enumerate(variants):
        req_params = {"host_mid": uid, "timezone_offset": "-480", "platform": "web", "features": BILI_FEATURES}
        if full is True:
            req_params.update(DM_IMG_PARAMS)
        elif full is None:
            # V3: 最简参数（老格式，文字动态内容会被隐藏，但动态ID/视频标题/转发可见）
            req_params = {"host_mid": uid, "platform": "web"}
        params = enc_wbi(req_params, img_key, sub_key)
        url = DYNAMIC_API + "?" + urllib.parse.urlencode(params)
        referer = f"https://space.bilibili.com/{uid}/dynamic" if full is True else f"https://space.bilibili.com/{uid}"
        data, err = http_get_json(url, cookie=cookie, referer=referer)
        if err:
            log(f"V{vi+1} 请求失败(uid={uid}): {err}")
            data = None
        elif data.get("code") != 0:
            log(f"V{vi+1} 返回(uid={uid}): code={data.get('code')} {data.get('message')}")
            data = None
        if data and data.get("code") == 0 and ((data.get("data") or {}).get("items")):
            log(f"V{vi+1} 成功(uid={uid})")
            break
        time.sleep(2)
    if not data or data.get("code") != 0:
        # 全档失败，整体重试（换新 cookie/密钥）
        if attempt < 2:
            time.sleep(8)
            return fetch_dynamics(uid, attempt + 1)
        return None
    items = ((data.get("data") or {}).get("items")) or []
    if not items:
        log(f"动态列表为空(uid={uid})，第{attempt+1}次尝试")
        # B站风控常以"空列表"表现，同样需要重试
        if attempt < 4:
            time.sleep(4)
            return fetch_dynamics(uid, attempt + 1)
        return None

    # 解析所有动态（id_str 随时间递增，天然排除置顶旧动态）
    dynamics = []
    for it in items:
        id_str = str(it.get("id_str") or "")
        if not id_str.isdigit():
            continue
        ts = None
        try:
            ma = it.get("modules", {}).get("module_author", {}) or {}
            ts = int(ma.get("pub_ts") or 0) or None
        except Exception:
            ts = None
        dynamics.append({
            "id": id_str,
            "ts": ts,
            "text": extract_dynamic_text(it),
            "link": f"https://t.bilibili.com/{id_str}",
        })
    if not dynamics:
        return None
    dynamics.sort(key=lambda x: int(x["id"]))
    return dynamics


def extract_dynamic_text(item):
    """从动态中提取可读文本。
    优先级：转发评论(desc) > OPUS图文正文 > 视频标题 > 专栏标题 > 转发内容(orig)
    """
    try:
        md = item.get("modules", {}).get("module_dynamic", {}) or {}
    except Exception:
        md = {}
    major = md.get("major") or {}
    mtype = major.get("type") or ""
    text = ""
    try:
        if mtype == "MAJOR_TYPE_OPUS":
            # 新版图文动态（含纯文字），正文在 opus.summary.text
            opus = major.get("opus") or {}
            text = (opus.get("summary") or {}).get("text") or ""
            if not text:
                text = (opus.get("title") or "")
        elif mtype in ("MAJOR_TYPE_VIDEO", "MAJOR_TYPE_ARCHIVE"):
            text = (major.get("archive") or {}).get("title") or ""
        elif mtype == "MAJOR_TYPE_ARTICLE":
            text = (major.get("article") or {}).get("title") or ""
    except Exception:
        text = ""
    # 转发动态：desc 是转发评论
    try:
        desc = (md.get("desc") or {}).get("text") or ""
    except Exception:
        desc = ""
    if desc:
        text = (text + "\n" + desc).strip() if text else desc
    if not text:
        # 转发动态的原内容
        orig = item.get("orig") or {}
        try:
            omd = orig.get("modules", {}).get("module_dynamic", {}) or {}
            om = omd.get("major") or {}
            if om.get("type") in ("MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_VIDEO"):
                text = "转发视频：" + ((om.get("archive") or {}).get("title") or "")
            elif om.get("type") == "MAJOR_TYPE_OPUS":
                text = "转发动态：" + (((om.get("opus") or {}).get("summary") or {}).get("text") or "")
        except Exception:
            pass
    text = (text or "").strip()
    if len(text) > 300:
        text = text[:300] + "…"
    return text if text else "（内容暂未取到，点击链接查看）"


# ---------- 飞书推送 ----------
def push_feishu(text):
    body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        ret = json.loads(resp.read().decode("utf-8"))
    return ret


# ---------- 状态读写 ----------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------- 主流程 ----------
def main():
    state = load_state()
    now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    changed = False
    notifications = []

    for up in UP_LIST:
        uid = up["uid"]
        name = up["name"]
        dynamics = fetch_dynamics(uid)
        if not dynamics:
            print(f"[{now_str}] {name}({uid}) 抓取失败或暂无动态，跳过")
            continue

        newest_id = dynamics[-1]["id"]
        last_id = state.get(uid)

        # 首次建立基线（无记录或空值），不推送，避免历史动态刷屏
        if not last_id:
            state[uid] = newest_id
            print(f"[{now_str}] {name}({uid}) 首次建立基线: {newest_id}")
            changed = True
            continue

        # 找出所有比基线新的动态（升序）
        news = [d for d in dynamics if int(d["id"]) > int(last_id)]
        if not news:
            print(f"[{now_str}] {name}({uid}) 无新动态")
            continue

        # 所有新动态合并为一条消息推送
        msg_parts = []
        if len(news) == 1:
            d = news[0]
            time_str = ""
            if d["ts"]:
                try:
                    time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(d["ts"])))
                except Exception:
                    time_str = ""
            msg_parts = [
                f"【{name}】发布了新动态",
                f"时间：{time_str or '刚刚'}",
                f"内容：{d['text']}",
                f"链接：{d['link']}",
            ]
        else:
            lines = [f"【{name}】发布了 {len(news)} 条新动态：", ""]
            for d in news:
                time_str = ""
                if d["ts"]:
                    try:
                        time_str = time.strftime("%H:%M", time.localtime(int(d["ts"])))
                    except Exception:
                        time_str = ""
                lines.append(f"◆ [{time_str or '刚刚'}] {d['text']}")
                lines.append(f"  {d['link']}")
                lines.append("")
            msg_parts = lines
        notification = "\n".join(msg_parts).strip()

        try:
            ret = push_feishu(notification)
            if ret.get("code") == 0:
                print(f"[{now_str}] {name}({uid}) 已推送 {len(news)} 条新动态（至 {newest_id}）")
                notifications.append(f"{name}: {news[-1]['text'][:40]}")
            else:
                print(f"[{now_str}] {name}({uid}) 推送失败: {ret}")
                continue
        except Exception as e:
            print(f"[{now_str}] {name}({uid}) 推送异常: {e}")
            continue

        state[uid] = newest_id
        changed = True

    if changed:
        save_state(state)

    if notifications:
        print("NEW_DYNAMIC:", " | ".join(notifications))
    print("DONE")


if __name__ == "__main__":
    main()