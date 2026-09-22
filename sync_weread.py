#!/usr/bin/env python3
"""
微信读书笔记自动同步脚本

用法：
    python3 sync_weread.py            # 正常运行：更新笔记并提交推送
    python3 sync_weread.py --dry-run  # 只打印会做什么，不写文件、不碰 git

工作方式：
    不再写死某一本书。每次运行会拉取整个书架，挑出"最近 CANDIDATE_WINDOW_DAYS
    天内有阅读更新"的书（不论是正在读还是刚读完），逐本同步笔记：
    - 本地没有对应的 .md 笔记文件：新建，写入完整书籍信息 + 目前所有划线/想法
    - 已经有笔记文件：只追加上次同步之后新增的划线/想法，并更新进度信息

    需要本地网络能访问 i.weread.qq.com，以及 ~/.weread_credentials 里的
    WEREAD_API_KEY / GITHUB_TOKEN（见该文件本身的注释）。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.expanduser("~/.weread_credentials")
API_URL = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.4"

# 只处理最近 N 天内在书架上有阅读更新（readUpdateTime）的书，避免每次
# 都把 300+ 本书全部拉一遍接口。7 天的定时任务周期 + 1 天缓冲。
CANDIDATE_WINDOW_DAYS = 8


def load_credentials_file(path):
    """把形如 `export KEY=VALUE` 的隐藏凭据文件加载进 os.environ。

    launchd 启动脚本时不会执行 shell 的 rc 文件（如 .zshrc），所以这里
    独立加载一次，保证无论是交互式 shell 手动运行，还是被 launchd 定时
    任务调用，都能拿到凭据。已存在的环境变量优先，不会被文件内容覆盖。
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_credentials_file(CREDENTIALS_FILE)

API_KEY = os.environ.get("WEREAD_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER = os.environ.get("GITHUB_USER", "lqwzai555")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "weread")


def call_api(api_name, params):
    payload = {"api_name": api_name, "skill_version": SKILL_VERSION}
    payload.update(params)
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
    if result.get("errcode"):
        raise RuntimeError(f"{api_name} 调用失败：{result.get('errmsg', result)}")
    return result


# ---------------------------------------------------------------------------
# 格式化辅助
# ---------------------------------------------------------------------------

def fmt_date(ts):
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def fmt_duration(seconds):
    seconds = seconds or 0
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}小时{m}分钟"


def sanitize_filename(title):
    """替换文件系统里有问题的字符（主要是 "/"），其余原样保留。"""
    return title.replace("/", "／").strip()


# ---------------------------------------------------------------------------
# 章节标题：把 "第一部" 这类一级标题拼到二级章节前面，
# 生成类似 "第一部 · 第七章" 这样的复合标题。
# ---------------------------------------------------------------------------

def build_chapter_meta(book_id):
    resp = call_api("/book/chapterinfo", {"bookId": book_id})
    chapters = resp.get("chapters", [])
    meta = {}
    current_part = None
    for ch in chapters:
        uid = ch.get("chapterUid")
        idx = ch.get("chapterIdx", uid)
        title = ch.get("title", f"章节{uid}")
        level = ch.get("level", 1)
        if level == 1:
            current_part = title
            meta[uid] = {"title": title, "idx": idx}
        else:
            composite = f"{current_part} · {title}" if current_part else title
            meta[uid] = {"title": composite, "idx": idx}
    return meta


# ---------------------------------------------------------------------------
# 划线 / 想法分组与格式化
# ---------------------------------------------------------------------------

def group_and_format(bookmarks, reviews, chapter_meta):
    by_uid = {}
    for m in bookmarks:
        uid = m.get("chapterUid")
        by_uid.setdefault(uid, {"marks": [], "reviews": []})["marks"].append(m)
    for r in reviews:
        uid = r.get("chapterUid")
        if uid is None:
            continue
        by_uid.setdefault(uid, {"marks": [], "reviews": []})["reviews"].append(r)

    def sort_key(uid):
        return chapter_meta.get(uid, {}).get("idx", uid)

    lines = []
    for uid in sorted(by_uid.keys(), key=sort_key):
        title = chapter_meta.get(uid, {}).get("title", f"章节 {uid}")
        lines.append(f"### {title}\n")
        marks = sorted(by_uid[uid]["marks"], key=lambda x: x.get("createTime", 0))
        mark_texts = [m.get("markText", "").strip() for m in marks]
        for m in marks:
            text = m.get("markText", "").strip()
            if text:
                lines.append(f"> {text}\n")
        for r in by_uid[uid]["reviews"]:
            abstract = (r.get("abstract") or "").strip()
            content = (r.get("content") or "").strip()
            if abstract and abstract not in mark_texts:
                lines.append(f"> {abstract}\n")
            if content:
                lines.append(f"**想法**：{content}\n")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 进度表：通用的"查找/更新某一行"逻辑，兼容不同笔记文件里
# 略有差异的字段命名（如"阅读时长" vs "累计阅读时长"）。
# ---------------------------------------------------------------------------

def find_table_row(content, label_variants):
    for line in content.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[1] in label_variants:
            return line
    return None


def update_table_row(content, label_variants, new_value):
    lines = content.splitlines()
    for i, line in enumerate(lines):
        parts = line.split("|")
        if len(parts) >= 3 and parts[1].strip() in label_variants:
            parts[2] = f" {new_value} "
            lines[i] = "|".join(parts)
            return "\n".join(lines) + ("\n" if content.endswith("\n") else "")
    return content


def get_last_sync_ts(content):
    """从进度表里的"最近同步"行解析出时间戳；没有则返回 0（视为从未同步过）。"""
    row = find_table_row(content, ["最近同步"])
    if not row:
        return 0
    for p in row.split("|"):
        p = p.strip()
        if len(p) == 10 and p[4] == "-" and p[7] == "-":
            try:
                return int(datetime.datetime.strptime(p, "%Y-%m-%d").timestamp())
            except ValueError:
                continue
    return 0


# ---------------------------------------------------------------------------
# 单本书同步
# ---------------------------------------------------------------------------

def build_new_note(shelf_book, book_id, today_str):
    info = call_api("/book/info", {"bookId": book_id})
    progress_resp = call_api("/book/getprogress", {"bookId": book_id})
    progress = progress_resp.get("book", {})
    chapter_meta = build_chapter_meta(book_id)
    bookmarks = call_api("/book/bookmarklist", {"bookId": book_id}).get("updated", [])
    reviews_resp = call_api("/review/list/mine", {"bookid": book_id, "synckey": 0, "count": 100})
    reviews = [r.get("review", r) for r in reviews_resp.get("reviews", [])]

    finished = bool(shelf_book.get("finishReading"))

    lines = [f"# {info.get('title', shelf_book.get('title', '未知书名'))}", ""]
    if info.get("author"):
        lines.append(f"**作者**：{info['author']}")
    if info.get("translator"):
        lines.append(f"**译者**：{info['translator']}")
    pub = info.get("publisher", "")
    pub_time = info.get("publishTime", "")
    if pub:
        pub_line = pub + (f"（{pub_time[:7]}）" if pub_time else "")
        lines.append(f"**出版**：{pub_line}")
    rating = info.get("newRating") or 0
    rating_count = info.get("newRatingCount") or 0
    if rating and rating_count:
        rating_title = (info.get("newRatingDetail") or {}).get("title", "")
        lines.append(f"**评分**：{rating / 100:.2f} / 10" + (f"（{rating_title}）" if rating_title else ""))
    if info.get("category"):
        lines.append(f"**分类**：{info['category']}")
    lines.append(f"**状态**：{'读完' if finished else '在读'}")
    lines.append("")
    if info.get("intro"):
        lines.append(f"> {info['intro'].strip()}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 进度")
    lines.append("")
    lines.append("| 项目 | 状态 |")
    lines.append("|------|------|")
    if finished:
        finish_ts = progress.get("finishTime") or shelf_book.get("readUpdateTime")
        lines.append(f"| 读完时间 | {fmt_date(finish_ts)} |")
    else:
        start_ts = progress.get("startReadingTime") or shelf_book.get("readUpdateTime")
        cur_uid = progress.get("chapterUid")
        cur_title = chapter_meta.get(cur_uid, {}).get("title", "—") if cur_uid else "—"
        pct = progress.get("progress")
        if pct is not None and cur_title != "—":
            cur_title = f"{cur_title}（进度 {pct}%）"
        lines.append(f"| 开始时间 | {fmt_date(start_ts)} |")
        lines.append(f"| 当前章节 | {cur_title} |")
    lines.append(f"| 累计阅读时长 | {fmt_duration(progress.get('readingTime'))} |")
    lines.append(f"| 最近同步 | {today_str} |")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 划线笔记")
    lines.append("")
    body = group_and_format(bookmarks, reviews, chapter_meta)
    lines.append(body if body.strip() else "（目前还没有划线或想法）\n")
    lines.append("<!-- 每次同步后，新增划线/想法可按日期追加在此 -->")
    return "\n".join(lines) + "\n"


def update_existing_note(content, shelf_book, book_id, today_str):
    last_ts = get_last_sync_ts(content)
    chapter_meta = build_chapter_meta(book_id)
    bookmarks = call_api("/book/bookmarklist", {"bookId": book_id}).get("updated", [])
    reviews_resp = call_api("/review/list/mine", {"bookid": book_id, "synckey": 0, "count": 100})
    reviews = [r.get("review", r) for r in reviews_resp.get("reviews", [])]

    new_marks = [m for m in bookmarks if m.get("createTime", 0) > last_ts]
    new_reviews = [r for r in reviews if r.get("createTime", 0) > last_ts]

    progress_resp = call_api("/book/getprogress", {"bookId": book_id})
    progress = progress_resp.get("book", {})
    finished = bool(shelf_book.get("finishReading"))

    # 更新进度表里已有的行（找不到对应行就跳过，不强行插入新行）
    content = update_table_row(content, ["最近同步"], today_str)
    content = update_table_row(
        content, ["累计阅读时长", "阅读时长"], fmt_duration(progress.get("readingTime"))
    )
    content = update_table_row(content, ["划线数"], str(len(bookmarks)))
    if finished:
        finish_ts = progress.get("finishTime") or shelf_book.get("readUpdateTime")
        content = update_table_row(content, ["读完时间"], fmt_date(finish_ts))
        content = update_table_row(content, ["状态"], "读完")
    else:
        cur_uid = progress.get("chapterUid")
        if cur_uid:
            cur_title = chapter_meta.get(cur_uid, {}).get("title", f"章节{cur_uid}")
            pct = progress.get("progress")
            if pct is not None:
                cur_title = f"{cur_title}（进度 {pct}%）"
            content = update_table_row(content, ["当前章节"], cur_title)

    if not new_marks and not new_reviews:
        return content, False

    new_section = group_and_format(new_marks, new_reviews, chapter_meta)
    entry = f"\n### {today_str} 同步（{len(new_marks)} 条划线 + {len(new_reviews)} 条想法新增）\n\n{new_section}\n---\n"

    anchor = "<!-- 每次同步后，新增划线/想法可按日期追加在此 -->"
    if anchor in content:
        content = content.replace(anchor, anchor + entry)
    elif "<!-- 每次同步后，笔记将按日期追加在此 -->" in content:
        anchor2 = "<!-- 每次同步后，笔记将按日期追加在此 -->"
        content = content.replace(anchor2, anchor2 + entry)
    else:
        content = content + "\n## 阅读笔记\n" + entry
    return content, True


def sync_book(shelf_book, today_str, repo_dir, dry_run):
    book_id = shelf_book["bookId"]
    title = shelf_book.get("title", book_id)
    filename = sanitize_filename(title) + ".md"
    path = os.path.join(repo_dir, filename)

    print(f"- {title}（{book_id}）")

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old_content = f.read()
        new_content, has_new = update_existing_note(old_content, shelf_book, book_id, today_str)
        if not has_new:
            print("    没有新增划线/想法，只刷新了进度信息")
        else:
            print("    有新增内容")
        if dry_run:
            print(f"    [dry-run] 不写入 {filename}")
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return filename
    else:
        new_content = build_new_note(shelf_book, book_id, today_str)
        print(f"    新建笔记文件 {filename}")
        if dry_run:
            print(f"    [dry-run] 不写入 {filename}")
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return filename


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def find_candidates():
    """挑出"真的读过"的书，而不是"最近点开过一眼"的书。

    最初的实现是看 /shelf/sync 里 readUpdateTime 是否在最近几天内，结果会把
    只是点开翻了几页、根本没读多少的书也当成候选（实测一次拉出 10 本，其中
    大半只是刷到而已）。改用 /readdata/detail 的 readLongest 榜单：接口本身
    已经把单本阅读不到 5 分钟的条目过滤掉了，能更准确反映"真的在读"。

    分别查询"本周"和"上周"两个自然周（各自已按 >5 分钟过滤），取并集，
    覆盖略大于一周的窗口，这样无论脚本是按计划在周一触发，还是被手动
    临时跑一次，都不会因为周期边界漏掉刚读完的书。
    """
    now = time.time()
    merged = {}
    for base_time in (0, int(now - CANDIDATE_WINDOW_DAYS * 86400)):
        resp = call_api("/readdata/detail", {"mode": "weekly", "baseTime": base_time})
        for item in resp.get("readLongest", []):
            book = item.get("book")
            if not book or not book.get("bookId"):
                continue  # 没有 book 字段的是专辑/有声书条目，划线类接口不支持，跳过
            bid = book["bookId"]
            read_time = item.get("readTime", 0)
            if bid not in merged or read_time > merged[bid]["readTime"]:
                merged[bid] = {"book": book, "readTime": read_time}

    if not merged:
        return []

    shelf = call_api("/shelf/sync", {})
    shelf_by_id = {b["bookId"]: b for b in shelf.get("books", []) if b.get("bookId")}

    candidates = []
    for bid, entry in merged.items():
        shelf_book = shelf_by_id.get(bid)
        if shelf_book is None:
            # 书架里找不到对应条目（理论上不太会发生），用 readdata 里的信息拼一个最小可用版本
            shelf_book = {
                "bookId": bid,
                "title": entry["book"].get("title", bid),
                "finishReading": 0,
                "readUpdateTime": int(now),
            }
        candidates.append(shelf_book)

    candidates.sort(key=lambda b: merged[b["bookId"]]["readTime"])
    return candidates


def git_commit_and_push(repo_dir, files, today_str, dry_run):
    if not files:
        print("没有文件变动，跳过 git 提交。")
        return
    if dry_run:
        print(f"[dry-run] 会提交并推送这些文件：{files}")
        return
    subprocess.run(["git", "config", "user.email", "weread@auto.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "WeRead-Sync"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", *files], cwd=repo_dir, check=True)
    summary = "、".join(os.path.splitext(f)[0] for f in files)
    subprocess.run(
        ["git", "commit", "-m", f"自动同步 {today_str}：{summary}"],
        cwd=repo_dir,
        check=True,
    )
    if GITHUB_TOKEN:
        remote_url = f"https://{GITHUB_USER}:{GITHUB_TOKEN}@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"
        subprocess.run(["git", "remote", "set-url", "origin", remote_url], cwd=repo_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo_dir, check=True)
    print("已推送到 GitHub。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印会做什么，不写文件、不碰 git")
    args = parser.parse_args()

    if not API_KEY:
        raise SystemExit("请检查 ~/.weread_credentials 里是否设置了 WEREAD_API_KEY")

    today_str = datetime.date.today().strftime("%Y-%m-%d")

    print(f"查找最近 {CANDIDATE_WINDOW_DAYS} 天内有阅读更新的书...")
    candidates = find_candidates()
    if not candidates:
        print("最近没有阅读更新，本次无需同步。")
        return
    print(f"共 {len(candidates)} 本，逐一同步：")

    touched = []
    for book in candidates:
        try:
            filename = sync_book(book, today_str, REPO_DIR, args.dry_run)
            if filename:
                touched.append(filename)
        except Exception as e:
            print(f"    同步失败：{e}", file=sys.stderr)

    git_commit_and_push(REPO_DIR, touched, today_str, args.dry_run)


if __name__ == "__main__":
    main()
