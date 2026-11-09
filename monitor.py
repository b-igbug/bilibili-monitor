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
import urllib.parse
import urllib.request

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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# ==============================

# B站 WBI 签名置换表（固定值）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


# ---------- WBI 签名 ----------
def get_mixin_key(orig):
    """用置换表从 img_key+sub_key 生成 mixin_key（取前32位）"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def get_wbi_keys():
    """从 nav 接口获取 img_key / sub_key"""
    req = urllib.request.Request(NAV_API, headers={"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    wbi = data["data"]["wbi_img"]
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
def fetch_newest_dynamic(uid):
    """
    返回 (动态ID, 动态时间戳, 动态文本, 动态链接)；
    抓取失败/被风控返回 None（调用方不推送、不改状态）。
    """
    try:
        img_key, sub_key = get_wbi_keys()
    except Exception:
        return None

    params = enc_wbi({"host_mid": uid, "platform": "web"}, img_key, sub_key)
    url = DYNAMIC_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": f"https://space.bilibili.com/{uid}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if data.get("code") != 0 or not data.get("data") or not data["data"].get("items"):
        return None

    items = data["data"]["items"]
    # 取 id_str 最大的一条（动态ID随时间递增，天然排除置顶的旧动态）
    newest = None
    for it in items:
        id_str = str(it.get("id_str") or "")
        if not id_str.isdigit():
            continue
        if newest is None or int(id_str) > int(newest["id_str"]):
            newest = it
    if newest is None:
        return None

    id_str = str(newest["id_str"])
    ts = None
    try:
        md = newest.get("modules", {}).get("module_dynamic", {}) or {}
        ts = md.get("timestamp") or newest.get("desc", {}).get("timestamp")
    except Exception:
        ts = None
    text = extract_dynamic_text(newest)
    link = f"https://t.bilibili.com/{id_str}"
    return id_str, ts, text, link


def extract_dynamic_text(item):
    """尽量从动态中提取可读文本"""
    try:
        md = item.get("modules", {}).get("module_dynamic", {}) or {}
    except Exception:
        md = {}
    text = ""
    try:
        text = (md.get("desc") or {}).get("text") or ""
    except Exception:
        text = ""
    if not text:
        major = md.get("major") or {}
        mtype = major.get("type")
        try:
            if mtype in ("MAJOR_TYPE_VIDEO", "MAJOR_TYPE_ARCHIVE"):
                text = (major.get("archive") or {}).get("title") or ""
            elif mtype == "MAJOR_TYPE_DRAW":
                text = (major.get("draw") or {}).get("title") or ""
            elif mtype == "MAJOR_TYPE_ARTICLE":
                text = (major.get("article") or {}).get("title") or ""
            elif mtype == "MAJOR_TYPE_OPUS":
                text = ((major.get("opus") or {}).get("summary") or {}).get("text") or ""
        except Exception:
            text = text or ""
    text = (text or "").strip()
    if len(text) > 200:
        text = text[:200] + "…"
    return text or "（该动态无文字内容）"


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
        got = fetch_newest_dynamic(uid)
        if got is None or got[0] is None:
            print(f"[{now_str}] {name}({uid}) 抓取失败或暂无动态，跳过")
            continue

        id_str, ts, text, link = got
        last_id = state.get(uid)

        if last_id is None:
            state[uid] = id_str
            print(f"[{now_str}] {name}({uid}) 首次建立基线: {id_str}")
            changed = True
            continue

        if id_str == last_id:
            print(f"[{now_str}] {name}({uid}) 无新动态")
            continue

        time_str = ""
        if ts:
            try:
                time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
            except Exception:
                time_str = ""
        msg_lines = [
            f"【{name}】发布了新动态",
            f"时间：{time_str or '刚刚'}",
            f"内容：{text}",
            f"链接：{link}",
        ]
        notification = "\n".join(msg_lines)
        try:
            ret = push_feishu(notification)
            if ret.get("code") == 0:
                print(f"[{now_str}] {name}({uid}) 已推送新动态 {id_str}")
                notifications.append(f"{name}: {text}")
            else:
                print(f"[{now_str}] {name}({uid}) 推送失败: {ret}")
                continue
        except Exception as e:
            print(f"[{now_str}] {name}({uid}) 推送异常: {e}")
            continue

        state[uid] = id_str
        changed = True

    if changed:
        save_state(state)

    if notifications:
        print("NEW_DYNAMIC:", " | ".join(notifications))
    print("DONE")


if __name__ == "__main__":
    main()