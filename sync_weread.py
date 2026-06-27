#!/usr/bin/env python3
"""
微信读书《酒吧长谈》笔记同步脚本
用法：python3 sync_weread.py
需要本地网络或国内 IP 环境
"""

import json
import urllib.request
import urllib.error
import datetime
import subprocess
import sys
import os

BOOK_ID = "3300196086"
API_KEY = os.environ.get("WEREAD_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "lqwzai555")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "weread")
SKILL_VERSION = "1.0.3"
API_URL = "https://i.weread.qq.com/api/agent/gateway"
NOTES_FILE = os.path.join(os.path.dirname(__file__), "酒吧长谈.md")


def call_api(api_name, extra=None, include_book_id=True):
    payload = {
        "api_name": api_name,
        "skill_version": SKILL_VERSION,
    }
    if include_book_id:
        payload["bookId"] = BOOK_ID
    if extra:
        payload.update(extra)
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "WeRead/8.2.0",
    }
    req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if "upgrade_info" in result:
        raise SystemExit(f"[WeRead] 需要升级：{result['upgrade_info']['message']}")
    return result


def get_last_sync_ts(content):
    """从文件顶部进度表解析最近同步日期，返回 Unix 时间戳；若为 — 返回 0"""
    for line in content.splitlines():
        if "最近同步" in line and "—" not in line:
            parts = [p.strip() for p in line.split("|")]
            for p in parts:
                if len(p) == 10 and p[4] == "-" and p[7] == "-":
                    dt = datetime.datetime.strptime(p, "%Y-%m-%d")
                    return int(dt.timestamp())
    return 0


def update_sync_date(content, date_str):
    lines = content.splitlines()
    for i, line in enumerate(lines):
        if "最近同步" in line:
            parts = line.split("|")
            if len(parts) >= 3:
                parts[2] = f" {date_str} "
                lines[i] = "|".join(parts)
                break
    return "\n".join(lines)


def get_chapter_map(chapters_data):
    """构建 chapterUid -> 章节标题 映射"""
    chapter_map = {}
    if not chapters_data:
        return chapter_map
    for item in chapters_data.get("data", []):
        uid = item.get("chapterUid")
        title = item.get("title", "未知章节")
        if uid:
            chapter_map[uid] = title
    return chapter_map


def format_notes(bookmarks, reviews, chapter_map, last_ts, today_str):
    """过滤并格式化新增笔记"""
    new_marks = [b for b in bookmarks if b.get("createTime", 0) > last_ts]
    new_reviews = [r for r in reviews if r.get("createTime", 0) > last_ts]

    if not new_marks and not new_reviews:
        return f"\n### {today_str} 同步：本周无新增划线\n"

    # 按章节分组
    by_chapter = {}
    for mark in new_marks:
        uid = mark.get("chapterUid", 0)
        by_chapter.setdefault(uid, {"marks": [], "reviews": []})
        by_chapter[uid]["marks"].append(mark)

    for review in new_reviews:
        uid = review.get("chapterUid", 0)
        by_chapter.setdefault(uid, {"marks": [], "reviews": []})
        by_chapter[uid]["reviews"].append(review)

    # 建立 bookmarkId -> review 的快速查找
    review_by_bookmark = {}
    for r in new_reviews:
        ref_id = r.get("refMpInfo", {}).get("bookmarkId") or r.get("abstract", "")
        if ref_id:
            review_by_bookmark[ref_id] = r.get("content", "")

    lines = [f"\n### {today_str} 同步\n"]
    for uid in sorted(by_chapter.keys()):
        chapter_title = chapter_map.get(uid, f"章节 {uid}")
        lines.append(f"#### {chapter_title}\n")
        for mark in sorted(by_chapter[uid]["marks"], key=lambda x: x.get("createTime", 0)):
            text = mark.get("markText", "").strip()
            if text:
                lines.append(f"> {text}\n")
                bid = mark.get("bookmarkId", "")
                if bid in review_by_bookmark:
                    lines.append(f"💭 {review_by_bookmark[bid]}\n")
                lines.append("")
        for review in by_chapter[uid]["reviews"]:
            content = review.get("content", "").strip()
            abstract = review.get("abstract", "").strip()
            if abstract and abstract not in [m.get("markText", "") for m in by_chapter[uid]["marks"]]:
                lines.append(f"> {abstract}\n")
            if content:
                lines.append(f"💭 {content}\n")
                lines.append("")

    lines.append("**本周思考**：\n")
    return "\n".join(lines)


def main():
    if not API_KEY:
        raise SystemExit("请设置环境变量 WEREAD_API_KEY")
    print("读取笔记文件...")
    with open(NOTES_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    last_ts = get_last_sync_ts(content)
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    print(f"上次同步时间戳：{last_ts}（{'从未同步' if last_ts == 0 else datetime.datetime.fromtimestamp(last_ts).strftime('%Y-%m-%d')}）")

    print("获取划线...")
    bookmarks_resp = call_api("/book/bookmarklist")
    bookmarks = bookmarks_resp.get("updated", [])
    print(f"  共 {len(bookmarks)} 条划线")

    print("获取想法...")
    reviews_resp = call_api("/review/list/mine", {"bookid": BOOK_ID, "synckey": 0, "count": 100}, include_book_id=False)
    reviews = [r.get("review", r) for r in reviews_resp.get("reviews", [])]
    print(f"  共 {len(reviews)} 条想法")

    print("获取章节信息...")
    chapters_resp = call_api("/book/chapterinfo")
    chapter_map = get_chapter_map(chapters_resp)

    new_section = format_notes(bookmarks, reviews, chapter_map, last_ts, today_str)

    # 更新最近同步日期
    content = update_sync_date(content, today_str)

    # 追加笔记
    if "## 阅读笔记" in content:
        anchor = "<!-- 每次同步后，笔记将按日期追加在此 -->"
        if anchor in content:
            content = content.replace(anchor, anchor + new_section)
        else:
            content = content + new_section
    else:
        content = content + "\n## 阅读笔记\n" + new_section

    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"笔记文件已更新：{NOTES_FILE}")

    # Git 提交推送
    repo_dir = os.path.dirname(NOTES_FILE)
    subprocess.run(["git", "config", "user.email", "weread@auto.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "WeRead-Sync"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "酒吧长谈.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", f"周同步 {today_str}"], cwd=repo_dir, check=True)
    if GITHUB_TOKEN:
        remote_url = f"https://{GITHUB_USER}:{GITHUB_TOKEN}@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    print("已推送到 GitHub。")


if __name__ == "__main__":
    main()
