#!/usr/bin/env python3
"""
AI产业新闻 · 批次合并工具（只用 Python 标准库）

定时任务每次运行时：
  1. python3 tools/merge_news.py --plan
     打印本次批次（早间/晚间）、抓取时段、已发布的新闻标题（用于去重）。
  2. 搜索并写好 batch.json（格式见 INSTRUCTIONS.md）。
  3. python3 tools/merge_news.py batch.json
     校验并合并到 news.json，同时存档到 archive/日期.json。有效新闻少于 3 条时不改动 news.json，退出码为 1。

规则：
  - 早间批（北京时间 14 点前运行）：抓前一天 20:00 至现在；开启新的一天，替换前一天的全部新闻。
  - 晚间批（14 点后运行）：抓当天 08:00 至现在；剔除早间已有的，排在早间上面一起展示。
  - 同一批次当天重跑：覆盖该批次，不影响另一批次。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_PATH = os.path.join(REPO, "news.json")
ARCHIVE_DIR = os.path.join(REPO, "archive")
TZ = dt.timezone(dt.timedelta(hours=8))  # 新加坡时间 = 北京时间
SLOTS = {"morning": "早间 08:00", "evening": "晚间 20:00"}
CATEGORIES = {"model": "大模型产业", "app": "AI应用产业", "funding": "投融资动态"}
DISCLAIMER = "公开资料整理，不涉及投资意见"


def now():
    return dt.datetime.now(TZ)


def slot_and_window(t, slot=None):
    slot = slot or ("morning" if t.hour < 14 else "evening")
    if slot == "morning":
        start = dt.datetime.combine(t.date() - dt.timedelta(days=1), dt.time(20, 0), TZ)
    else:
        start = dt.datetime.combine(t.date(), dt.time(8, 0), TZ)
    return slot, start, t


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def norm_url(u):
    try:
        p = urllib.parse.urlsplit((u or "").strip())
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith(("utm_", "spm", "from", "share"))]
        host = re.sub(r"^(www|m)\.", "", p.netloc.lower())
        return urllib.parse.urlunsplit(("https", host, p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    except Exception:  # noqa: BLE001
        return (u or "").strip()


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t or "").lower()


def parse_time(s):
    s = (s or "").strip()
    for fmt, n in (("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(s[:n], fmt).replace(tzinfo=TZ), fmt == "%Y-%m-%d"
        except ValueError:
            continue
    return None, False


def prior_batches(cur, slot, today):
    """用于去重的已发布批次：当天其他批次；早间批还包括前一天的全部（避免把昨晚的新闻再发一遍）。"""
    same_day = cur.get("date") == today.isoformat()
    bs = list(cur.get("batches") or [])
    if same_day:
        return [b for b in bs if b.get("slot") != slot]
    return bs if slot == "morning" else []


def published_sets(batches):
    urls, titles, lst = set(), set(), []
    for b in batches:
        for items in (b.get("categories") or {}).values():
            for it in items:
                urls.add(norm_url(it.get("url")))
                titles.add(norm_title(it.get("title")))
                lst.append(it.get("title", ""))
    return urls, titles, lst


def validate(items, start, end, urls, titles):
    ok, dropped = [], []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        title, body = (it.get("title") or "").strip(), (it.get("body") or "").strip()
        source = (it.get("source") or "").strip().strip("（）()")
        url = (it.get("url") or "").strip()
        pt, date_only = parse_time(it.get("published_at"))
        nu, nt = norm_url(url), norm_title(title)
        why = None
        if not (title and body and source and url.startswith("http")):
            why = "字段不全"
        elif len(title) > 40:
            why = f"标题过长（{len(title)}字）"
        elif len(body) > 220:
            why = f"正文过长（{len(body)}字）"
        elif pt is None:
            why = "发布时间无法识别"
        elif date_only and not (start.date() <= pt.date() <= end.date()):
            why = f"发布日期 {pt.date()} 不在时段内"
        elif not date_only and not (start - dt.timedelta(hours=2) <= pt <= end + dt.timedelta(minutes=30)):
            why = f"发布时间 {pt:%m-%d %H:%M} 不在时段内"
        elif nu in urls or nt in titles:
            why = "与已发布新闻重复"
        if why:
            dropped.append(f"{why}：{title[:30]}")
            continue
        urls.add(nu)
        titles.add(nt)
        ok.append({"title": title, "body": body, "source": source, "url": url,
                   "publishedAt": pt.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")})
    return ok, dropped


def main():
    ap = argparse.ArgumentParser(description="合并一个新闻批次到 news.json")
    ap.add_argument("batch", nargs="?", help="batch.json 路径")
    ap.add_argument("--plan", action="store_true", help="只打印本次批次、时段和已发布标题")
    ap.add_argument("--slot", choices=["morning", "evening"])
    args = ap.parse_args()

    t = now()
    slot, start, end = slot_and_window(t, args.slot)
    today = t.date()
    cur = load_json(NEWS_PATH, {})
    prior = prior_batches(cur, slot, today)
    urls, titles, title_list = published_sets(prior)

    if args.plan or not args.batch:
        print(f"批次：{slot}（{SLOTS[slot]}）")
        print(f"抓取时段（北京时间/新加坡时间）：{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}")
        print(f"已发布新闻 {len(title_list)} 条（不要重复）：")
        for x in title_list:
            print(f"  - {x}")
        return

    batch = load_json(args.batch, None)
    if not isinstance(batch, dict) or not isinstance(batch.get("categories"), dict):
        sys.exit("batch.json 格式不对：需要 {\"categories\": {\"model\": [...], \"app\": [...], \"funding\": [...]}}")
    result, n = {}, 0
    for key, name in CATEGORIES.items():
        ok, dropped = validate(batch["categories"].get(key, []), start, end, urls, titles)
        result[key] = ok
        n += len(ok)
        print(f"{name}：提交 {len(batch['categories'].get(key, []) or [])} 条，通过 {len(ok)} 条")
        for d in dropped:
            print(f"  丢弃 · {d}")
    if n < 3:
        print(f"有效新闻只有 {n} 条，未改动 news.json。")
        sys.exit(1)

    new_batch = {"slot": slot, "label": SLOTS[slot], "fetchedAt": t.isoformat(timespec="seconds"),
                 "window": [start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")],
                 "categories": result}
    same_day = cur.get("date") == today.isoformat()
    batches = ([b for b in cur.get("batches", []) if b.get("slot") != slot] if same_day else []) + [new_batch]
    batches.sort(key=lambda b: 0 if b["slot"] == "evening" else 1)  # 晚间在上
    out = {"date": today.isoformat(), "updatedAt": t.isoformat(timespec="seconds"),
           "categories": CATEGORIES, "disclaimer": DISCLAIMER, "batches": batches}
    save_json(NEWS_PATH, out)
    save_json(os.path.join(ARCHIVE_DIR, f"{today.isoformat()}.json"), out)
    print(f"已更新 news.json：{today} {SLOTS[slot]}，" + "，".join(f"{CATEGORIES[k]} {len(v)} 条" for k, v in result.items()))


if __name__ == "__main__":
    main()
