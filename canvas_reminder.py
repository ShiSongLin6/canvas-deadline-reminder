#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canvas 作业截止提醒 -> Server酱 微信推送。

规则
  A. 单条提醒：某作业截止前约 2 小时推一次（剩余时间落在窗口内即触发，每个作业只推一次）。
  B. 中午汇总：英国时间 12:00 之后第一次执行时，若当天有未提交的截止作业，
     推送当天清单；当天没有作业则不推。每天最多一次。

用法
  python canvas_reminder.py             正式跑（计划任务每小时调用一次）
  python canvas_reminder.py --dry-run   只打印将要推送的内容，不联网、不推送
  python canvas_reminder.py --selftest  用假数据验证判定逻辑，不联网
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    # pythonw.exe 下 sys.stdout 可能为 None，reconfigure 会抛错，忽略即可
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")

DEFAULT_CONFIG = {
    "canvas_base": "https://YOUR-SCHOOL.instructure.com",
    "canvas_token": "",
    "sct_sendkey": "",
    "tz": "Europe/London",
    "lead_minutes": 120,     # 目标提前量：截止前 2 小时
    "window_low": 95,        # 剩余时间下界（分钟）—— 保证每小时一次执行不漏
    "window_high": 165,      # 剩余时间上界（分钟）
    "noon_hour": 12,         # 英国时间几点之后发当天汇总
    "lookahead_days": 7,     # 只看未来这么多天的作业
    "course_filter": [],     # 为空=全部课程；否则只保留课程名包含这些关键词的
    "skip_locked": True,
}

LOG_LINES = []

# 课程全名片段 -> 短码（通知栏标题空间有限，必须用短码）。
# 按你自己的课程填，键是课程全名里出现的片段（不区分大小写），值为短码。
# 留空则自动取课程名各词首字母（最多 3 个）。
# 示例：
#   COURSE_CODES = [
#       ("computer vision", "CV"),
#       ("data intensive", "DIAI"),
#   ]
COURSE_CODES = []


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    LOG_LINES.append(line)
    try:
        print(line)
    except Exception:
        pass  # pythonw.exe 无 stdout，忽略（内容仍会写进日志文件）


def flush_log():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, datetime.now().strftime("%Y-%m-%d") + ".log")
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(LOG_LINES) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 配置与状态

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        raise SystemExit(
            "缺少 config.json。请复制 config.example.json 为 config.json，"
            "填入 Canvas Token 与 Server酱 SendKey。"
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg.update(json.load(f))
    for k in ("canvas_token", "sct_sendkey"):
        cfg[k] = str(cfg.get(k, "")).strip()
    return cfg


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"sent": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s.get("sent"), dict):
            return s
    except Exception:
        pass
    return {"sent": {}}


def save_state(state):
    # 清理 14 天前的记录，避免文件无限增长
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if v > cutoff}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- Canvas 取数

def canvas_get(session, base, token, path, params=None):
    """GET + 自动翻页，返回 list。"""
    url = base.rstrip("/") + "/api/v1" + path
    headers = {"Authorization": "Bearer " + token}
    out = []
    while url:
        r = session.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 401:
            raise SystemExit("Canvas 返回 401：Token 无效或已过期，请重新生成后写入 config.json。")
        if r.status_code != 200:
            raise SystemExit("Canvas 返回 %s：%s" % (r.status_code, r.text[:300]))
        data = r.json()
        if isinstance(data, dict):
            for key in ("assignments", "courses"):
                if key in data:
                    data = data[key]
                    break
        out.extend(data if isinstance(data, list) else [])
        url = None
        link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if m:
            url, params = m.group(1), None
    return out


def fetch_assignments(cfg):
    import requests

    session = requests.Session()
    courses = canvas_get(session, cfg["canvas_base"], cfg["canvas_token"], "/courses",
                         {"enrollment_state": "active", "per_page": 100})
    log("活跃课程 %d 门" % len(courses))

    filters = [s.lower() for s in cfg.get("course_filter", []) if s]
    items = []
    for c in courses:
        name = c.get("name") or ""
        if filters and not any(f in name.lower() for f in filters):
            continue
        try:
            rows = canvas_get(session, cfg["canvas_base"], cfg["canvas_token"],
                              "/courses/%s/assignments" % c.get("id"),
                              {"per_page": 100, "bucket": "upcoming",
                               "include[]": "submission"})
        except SystemExit:
            raise
        except Exception as e:
            log("课程 %s 取作业失败：%s" % (name, e))
            continue
        for a in rows:
            due = a.get("due_at")
            if not due:
                continue
            items.append({
                "course": name,
                "course_id": c.get("id"),
                "id": a.get("id"),
                "name": a.get("name") or "(未命名)",
                "due_at": due,
                "html_url": a.get("html_url") or "",
                "submitted": bool(((a.get("submission") or {}).get("submitted_at"))),
                "locked": bool(a.get("locked_for_user")) and not bool(a.get("submission")),
            })
    return items


def parse_due(s):
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    txt = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def course_code(name):
    """把课程全名压成短码，如 'Computer Vision and Imaging' -> 'CVI'。"""
    low = (name or "").lower()
    for key, code in COURSE_CODES:
        if key in low:
            return code
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z&]*", name or "")
             if w.lower() not in ("and", "of", "the", "for", "in", "on", "to")]
    return ("".join(w[0].upper() for w in words[:3]) or "课程")[:4]


def short_name(name, limit=14):
    """作业名压短：'Week 5: Image Segmentation and Registration' -> 'Week 5'。"""
    s = (name or "").strip()
    if ":" in s:
        s = s.split(":")[0].strip()
    else:
        parts = s.split()
        if len(parts) > 2:
            s = " ".join(parts[:2])
    s = s.strip()
    if len(s) > limit:
        s = s[:limit].strip()
    return s or "作业"


def build_title(cfg, it, due_local, now_local, kind):
    """通知栏里显示的就是这个标题，所以必须短、不能带 emoji。

    实测：微信通知栏那行约 26 个字符后被截断（多出来会变成 '...'），
    emoji 会被渲染层吃掉，中文全角冒号也可能被转成半角。
    """
    code = course_code(it["course"])
    nm = short_name(it["name"])
    if due_local.date() == now_local.date():
        when = due_local.strftime("%H:%M")
    else:
        when = due_local.strftime("%m-%d %H:%M")

    if kind == "pre":
        title = "%s %s %s截止" % (code, nm, when)
    else:
        title = "%s %s %s截止" % (code, nm, when)

    # 给模板自带的 "设备收信:" 前缀留出余量，总长控制在 24 字符内
    if len(title) > 24:
        title = title[:24].strip()
    return title


# ---------------------------------------------------------------- 判定逻辑

def decide(items, now, cfg, state):
    """返回要推送的消息列表 [(kind, title, desp, dedupe_key)]。"""
    tz = ZoneInfo(cfg["tz"])
    now_local = now.astimezone(tz)
    msgs = []

    pending = []
    for it in items:
        due = parse_due(it["due_at"])
        if due is None:
            continue
        if cfg.get("skip_locked", True) and it.get("locked"):
            continue
        if due <= now:                                    # 已经过了
            continue
        if due - now > timedelta(days=int(cfg["lookahead_days"])):
            continue
        pending.append((it, due))

    log("未来 %d 天内有效作业 %d 项" % (cfg["lookahead_days"], len(pending)))

    # 规则 A：截止前约 2 小时
    for it, due in pending:
        if it["submitted"]:
            continue
        left_min = (due - now).total_seconds() / 60.0
        key = "pre:%s:%s" % (it["course_id"], it["id"])
        if state["sent"].get(key):
            continue
        if cfg["window_low"] <= left_min <= cfg["window_high"]:
            h, m = int(left_min // 60), int(left_min % 60)
            left = ("%d 小时 %d 分" % (h, m)) if h else ("%d 分钟" % m)
            due_local = due.astimezone(tz)
            desp = "\n\n".join([
                "**课程**：%s" % it["course"],
                "**作业**：%s" % it["name"],
                "**截止**：%s（英国时间）" % due_local.strftime("%Y-%m-%d %H:%M"),
                "**还剩**：%s" % left,
                "**链接**：[打开提交页](%s)" % it["html_url"],
            ])
            title = build_title(cfg, it, due_local, now_local, "pre")
            msgs.append(("pre", title, desp, key))

    # 规则 B：英国时间中午之后的当天汇总（当天无作业则不推）
    noon_key = "noon:%s" % now_local.strftime("%Y-%m-%d")
    if now_local.hour >= int(cfg["noon_hour"]) and not state["sent"].get(noon_key):
        today = now_local.date()
        today_rows = [(it, due) for it, due in pending
                      if due.astimezone(tz).date() == today]
        todo = [r for r in today_rows if not r[0]["submitted"]]   # 只数还没交的
        if todo:
            today_rows.sort(key=lambda x: x[1])
            rows = []
            for it, due in today_rows:
                dl = due.astimezone(tz)
                flag = "✅ 已交" if it["submitted"] else "⬜ 待交"
                rows.append("| %s | %s | %s | %s |" % (
                    it["course"][:22], it["name"][:26],
                    dl.strftime("%H:%M"), flag))
            desp = "\n\n".join([
                "**%s（英国时间）还有 %d 项没交**" % (today.strftime("%Y-%m-%d"), len(todo)),
                "| 课程 | 作业 | 截止 | 状态 |",
                "| --- | --- | --- | --- |",
            ] + rows)
            # 标题取最紧急的那一项（最先截止的），通知栏只显示标题
            first_it, first_due = todo[0]
            fdl = first_due.astimezone(tz)
            title = "今天%d项待交 最晚%s" % (len(todo), fdl.strftime("%H:%M"))
            if len(title) > 24:
                title = title[:24].strip()
            msgs.append(("noon", title, desp, noon_key))
        else:
            state["sent"][noon_key] = datetime.now().isoformat()
            log("当天没有待交作业，中午汇总跳过")

    return msgs


# ---------------------------------------------------------------- 推送

def check_token_expiry(cfg, now, state):
    """Canvas 强制 token 最长 30 天，快到期时提醒续期（每天最多一次）。"""
    exp = cfg.get("canvas_token_expires", "").strip()
    if not exp:
        return None
    try:
        tz = ZoneInfo(cfg["tz"])
        dt = datetime.fromisoformat(exp).replace(tzinfo=tz)
    except Exception:
        return None
    days = (dt - now).days
    if days < 0:
        key = "tokexp:expired"
        title = "Canvas令牌已过期 提醒停了"
        desp = "令牌已于 %s 过期，作业提醒已停止工作。去 Canvas 重新生成一个，更新 config.json 里的 canvas_token 和 canvas_token_expires。" % exp
    elif days <= 5:
        key = "tokexp:%s:%d" % (exp, days)
        title = "Canvas令牌还剩%d天过期" % days
        desp = "令牌 %s 到期，到期后作业提醒会停。到期前重新生成一个即可（Canvas 最长只给 30 天）。" % exp
    else:
        return None
    if state["sent"].get(key):
        return None
    return ("tokexp", title, desp, key)


def push(cfg, title, desp, dry=False):
    if dry:
        log("--- DRY RUN 标题 ---\n%s\n--- 正文 ---\n%s" % (title, desp))
        return True
    import requests

    key = cfg["sct_sendkey"]
    if not key:
        log("未配置 sct_sendkey，跳过推送")
        return False
    try:
        r = requests.post("https://sctapi.ftqq.com/%s.send" % key,
                          data={"title": title[:32], "desp": desp}, timeout=20)
        data = r.json()
        ok = data.get("code") == 0
        log("推送%s：%s / %s" % ("成功" if ok else "失败", title, data))
        return ok
    except Exception as e:
        log("推送异常：%s" % e)
        return False


# ---------------------------------------------------------------- 自测

def selftest(cfg):
    tz = ZoneInfo(cfg["tz"])
    noon = datetime(2026, 11, 5, 12, 30, tzinfo=tz)
    morning = datetime(2026, 11, 5, 9, 0, tzinfo=tz)
    cases = []

    def mk(cid, aid, name, due, submitted=False, locked=False, course="CV"):
        return {"course": course, "course_id": cid, "id": aid, "name": name,
                "due_at": due.isoformat(), "html_url": "http://x/%s" % aid,
                "submitted": submitted, "locked": locked}

    day = datetime(2026, 11, 5, 0, 0, tzinfo=tz)

    base = [
        mk(1, 101, "Lab3", noon + timedelta(minutes=130)),          # 剩 130 分 -> 命中
        mk(1, 102, "Lab2已交", noon + timedelta(minutes=130), True),  # 已交 -> 不提醒
        mk(2, 201, "Essay", noon + timedelta(minutes=300)),          # 剩 5 小时 -> 不提醒
        mk(3, 301, "Report", day + timedelta(hours=23)),             # 今天 23:00 -> 进中午清单
    ]

    cases.append(("中午12:30：提前提醒 + 当天汇总", base, noon,
                  ["pre:1:101", "noon:2026-11-05"]))
    cases.append(("上午9:00：只提前提醒，不发汇总",
                  [mk(1, 101, "Lab3", morning + timedelta(minutes=130))],
                  morning, ["pre:1:101"]))
    cases.append(("中午但当天没有待交作业：都不发",
                  [mk(2, 201, "Essay", day + timedelta(days=2))], noon, []))
    cases.append(("当天作业全已交：不发汇总",
                  [mk(3, 301, "Report", day + timedelta(hours=23), submitted=True)],
                  noon, []))
    cases.append(("已推送过不再重复", base, noon, []))

    all_ok = True
    for name, items, now, expect in cases:
        st = {"sent": {}}
        if name == "已推送过不再重复":
            st["sent"] = {"pre:1:101": datetime.now().isoformat(),
                          "noon:2026-11-05": datetime.now().isoformat()}
        got = [m[3] for m in decide(items, now, cfg, st)]
        ok = sorted(got) == sorted(expect)
        all_ok = all_ok and ok
        print("[%s] %s\n   得到 %s\n   期望 %s" % ("PASS" if ok else "FAIL", name, got, expect))

    print("\n自测结果：%s" % ("全部通过" if all_ok else "存在失败用例"))
    return 0 if all_ok else 1


# ---------------------------------------------------------------- 主流程

def main():
    argv = sys.argv[1:]
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log("config.json 读取失败：%s" % e)

    if "--selftest" in argv:
        return selftest(cfg)

    if "--test" in argv:          # 各通道都发一条测试消息
        cfg = load_config()
        ok = push(cfg, "Canvas 作业提醒已接通",
                  "这条是测试消息。\n\n收到就说明微信通道正常，之后作业截止前 2 小时、"
                  "以及英国时间中午的当天清单会自动推过来。", dry=False)
        flush_log()
        return 0 if ok else 1

    dry = "--dry-run" in argv or cfg.get("dry_run")
    cfg = load_config() if not dry else cfg
    if dry and not cfg.get("canvas_token"):
        log("dry-run 模式下未填 Token 也能跑（只验证流程）")

    state = load_state()
    try:
        if dry:
            log("dry-run：跳过联网取数")
            items = []
        else:
            items = fetch_assignments(cfg)
        msgs = decide(items, datetime.now(timezone.utc), cfg, state)
        extra = check_token_expiry(cfg, datetime.now(timezone.utc), state)
        if extra:
            msgs.append(extra)
        log("本次触发 %d 条" % len(msgs))
        for kind, title, desp, key in msgs:
            if push(cfg, title, desp, dry=dry):
                state["sent"][key] = datetime.now().isoformat()
        if dry:
            log("dry-run：未写 state.json")
        else:
            save_state(state)
    except SystemExit as e:
        log("中止：%s" % e)
        return 1
    except Exception:
        log("运行异常：\n%s" % traceback.format_exc())
        return 1
    finally:
        flush_log()
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        # 兜底：任何未捕获异常都要留下痕迹，否则 pythonw 下是静默失败
        try:
            log("未捕获异常：\n%s" % traceback.format_exc())
            flush_log()
        except Exception:
            pass
        code = 1
    sys.exit(code)
