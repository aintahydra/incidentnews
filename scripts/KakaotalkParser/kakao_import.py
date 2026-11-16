"""Import incident news and security intel shared in a KakaoTalk group chat into data/news.db.

    python scripts/KakaotalkParser/kakao_import.py                 # every int/humint/KakaoTalk_*.txt
    python scripts/KakaotalkParser/kakao_import.py FILE ... [--since 2026-01-01] [--limit N]
                                                   [--dry-run] [--import-only] [--config config.toml]

Steps
  1. Parse each export (kakao_log.py) and keep messages on or after --since that come after the last message
     imported from the same chat (table humint_imports), so a newer export of the same chat only adds new talk.
     Greetings, thanks, photos and system lines carry no link and are ignored.
  2. For each shared link not already in `articles` (compared after redirects and URL normalisation), fetch the
     page for its text, title and publication date, and insert it with status 'humint'. If the page can't be
     read (paywall, Cloudflare), the headline and preview text from the chat are used instead.
  3. Run the LLM from config.toml over the 'humint' articles, the same way the collector does: the incident
     extractor first (merging with existing incidents); if it is not an incident, the intel extractor; if neither,
     the article is skipped. Articles become status done / skipped, or failed after 3 attempts.

The article's `source` is the outlet that published it (보안뉴스, Security Week, ...), the same name its RSS cards
use, so cards link to and credit the original provider. Which chat shared it is kept in table humint_articles.

The server's collector ignores status 'humint', so the two never process the same article. Re-running resumes
where it stopped. Chat participants' names are not stored.

`--fix-sources` only rewrites articles stored by older versions with source "카카오톡 · <chat>" to the outlet name
(recording the chat in humint_articles) and exits.
"""
import argparse
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT)]

import kakao_log  # noqa: E402
from newscollect import db, feeds, pages  # noqa: E402
from newscollect.pages import (OUTLETS, bare_host, fetch, norm_url, outlet,  # noqa: E402,F401
                               outlet_names, page_date)
from newscollect.config import load_config  # noqa: E402

log = logging.getLogger("kakao_import")
STATUS = "humint"
# links that are never articles: video, forms, meetings, chat invites, job posts, file shares
SKIP_HOSTS = re.compile(r"(^|\.)(youtube\.com|youtu\.be|forms\.gle|zoom\.us|open\.kakao\.com|pf\.kakao\.com|"
                        r"instagram\.com|facebook\.com|drive\.google\.com|docs\.google\.com|tally\.so)$|^recruit\.")
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|mp4|zip|hwp|hwpx|docx?|xlsx?|pptx?)$", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS humint_articles (
  article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  chat TEXT NOT NULL,              -- chat the link was shared in
  shared_at TEXT                   -- 'YYYY-MM-DD HH:MM' of the message (NULL for rows fixed by --fix-sources)
);
CREATE TABLE IF NOT EXISTS humint_imports (
  chat TEXT PRIMARY KEY,           -- chat title from the export header
  last_message_at TEXT NOT NULL,   -- 'YYYY-MM-DD HH:MM' of the last message imported
  last_seq INTEGER NOT NULL,       -- how many messages of that minute were imported
  file TEXT,                       -- export file it came from
  saved_at TEXT,                   -- the export's 저장한 날짜
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


TAG_PREFIX = re.compile(r"^(\s*\[(공유|펌|일본|중국|미국|해외|국내|속보|단독)\]\s*)+")
SITE_SUFFIX = re.compile(r"\s+[-|–—:]\s+[^-|–—:]{2,25}$")


def headline(link: kakao_log.Link, page_title: str, url: str) -> str:
    """The chat's headline line (usually the exact headline, often already in Korean) without share tags like
    [공유]; else the page title without a trailing " - 사이트명"; else the URL."""
    chat = TAG_PREFIX.sub("", link.title).strip()
    if len(chat) >= 8:
        return chat
    return SITE_SUFFIX.sub("", page_title).strip() or chat or url


def snippet(link: kakao_log.Link) -> str:
    """What the chat itself says about the link: headline + preview, plus the sharer's note when that is thin."""
    text = "\n".join(x for x in (link.title, link.preview) if x).strip()
    if len(text) < feeds.MIN_BODY_CHARS and link.comment:
        text = (text + "\n\n" + link.comment).strip()
    return text


def new_links(conn, files: list[Path], since: date):
    """Yield (chat, file, saved_at, message key, link) for links in messages newer than each chat's bookmark."""
    logs = []
    for f in files:
        lg = kakao_log.parse(f.read_text(encoding="utf-8", errors="replace"))
        if not lg.chat:
            log.warning("%s: not a KakaoTalk export (no '... 님과 카카오톡 대화' header), skipped", f.name)
            continue
        logs.append((lg.saved_at or datetime.min, f, lg))
    for _, f, lg in sorted(logs, key=lambda t: t[0]):            # older exports first
        row = conn.execute("SELECT last_message_at, last_seq FROM humint_imports WHERE chat=?", (lg.chat,)).fetchone()
        mark = (row["last_message_at"], row["last_seq"]) if row else ("", 0)
        seq, prev, count = {}, None, 0
        for i, m in enumerate(lg.messages):                         # (minute, n-th message in that minute)
            key = m.when.strftime("%Y-%m-%d %H:%M")
            count = count + 1 if key == prev else 1
            seq[i], prev = (key, count), key
        fresh = [i for i, m in enumerate(lg.messages) if m.when.date() >= since and seq[i] > mark]
        log.info("%s: chat '%s' saved %s, %d messages since %s, %d new", f.name, lg.chat, lg.saved_at,
                 sum(m.when.date() >= since for m in lg.messages), since, len(fresh))
        for i in fresh:
            lks = kakao_log.links(lg.messages[i])
            for ln in lks:
                yield lg.chat, f, lg.saved_at, seq[i], ln
            if not lks:
                yield lg.chat, f, lg.saved_at, seq[i], None           # still advances the bookmark


def fix_sources(conn, names=None) -> int:
    """Articles stored as "카카오톡 · <chat>" by earlier versions: source -> the outlet, chat -> humint_articles."""
    rows = conn.execute("SELECT id, url, source FROM articles WHERE source LIKE '카카오톡 · %'").fetchall()
    for r in rows:
        conn.execute("INSERT OR IGNORE INTO humint_articles(article_id, chat) VALUES(?,?)",
                     (r["id"], r["source"].split(" · ", 1)[1]))
        conn.execute("UPDATE articles SET source=? WHERE id=?", (outlet(r["url"], "", names), r["id"]))
    conn.commit()
    return len(rows)


def import_links(conn, files, since, dry_run=False, names=None) -> int:
    known = {norm_url(r["url"]) for r in conn.execute("SELECT url FROM articles")}
    added, marks = 0, {r["chat"]: (r["last_message_at"], r["last_seq"])
                       for r in conn.execute("SELECT * FROM humint_imports")}
    with httpx.Client(timeout=20) as web:
        for chat, f, saved, key, ln in new_links(conn, files, since):
            if ln is not None:
                host = (urlsplit(ln.url).hostname or "").lower()
                if SKIP_HOSTS.search(host) or SKIP_EXT.search(urlsplit(ln.url).path):
                    log.debug("not an article: %s", ln.url)
                elif norm_url(ln.url) in known:
                    log.debug("already in DB: %s", ln.url)
                elif dry_run:
                    known.add(norm_url(ln.url))
                    added += 1
                    print(f"{ln.when:%Y-%m-%d %H:%M}  {ln.title[:70] or '(제목 없음)'}\n                  {ln.url}")
                else:
                    page = fetch(web, ln.url)
                    if norm_url(page["url"]) in known:              # an old link that redirects to a known article
                        known.add(norm_url(ln.url))
                        log.debug("already in DB after redirect: %s -> %s", ln.url, page["url"])
                    else:
                        shared = ln.when.date()
                        d = page["date"]
                        if not d or not (shared - timedelta(days=90)).isoformat() <= d <= shared.isoformat():
                            d = shared.isoformat()                   # no usable page date: the day it was shared
                        body = page["text"] if len(page["text"]) > feeds.MIN_BODY_CHARS else snippet(ln)
                        title = headline(ln, page["title"], page["url"])
                        cur = conn.execute("INSERT OR IGNORE INTO articles(source,url,title,published_at,body,section,status) "
                                           "VALUES(?,?,?,?,?,'incident',?)",
                                           (outlet(page["url"], page["site"], names), page["url"], title[:300], d,
                                            body[:6000], STATUS))
                        if cur.rowcount:
                            conn.execute("INSERT OR IGNORE INTO humint_articles(article_id, chat, shared_at) VALUES(?,?,?)",
                                         (cur.lastrowid, chat, f"{ln.when:%Y-%m-%d %H:%M}"))
                        known.update({norm_url(ln.url), norm_url(page["url"])})
                        added += 1
                        log.info("queued %s  %s", d, title[:80])
            if not dry_run and key > marks.get(chat, ("", 0)):  # the bookmark only moves forward
                marks[chat] = key
                conn.execute("INSERT INTO humint_imports(chat,last_message_at,last_seq,file,saved_at) VALUES(?,?,?,?,?) "
                             "ON CONFLICT(chat) DO UPDATE SET last_message_at=excluded.last_message_at, "
                             "last_seq=excluded.last_seq, file=excluded.file, saved_at=excluded.saved_at, "
                             "updated_at=CURRENT_TIMESTAMP",
                             (chat, key[0], key[1], f.name, saved.isoformat(sep=" ") if saved else None))
            if not dry_run:
                conn.commit()                                     # per message: an interrupted run resumes cleanly
    return added


def process(conn, cfg, limit=None) -> dict:
    """LLM pass over the queued 'humint' articles (pages.process_queued)."""
    stats = pages.process_queued(conn, cfg, STATUS, limit=limit)
    stats.pop("results")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="exports (default: int/humint/KakaoTalk_*.txt)")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--since", type=date.fromisoformat, default=date(2026, 1, 1), help="ignore earlier messages")
    ap.add_argument("--limit", type=int, help="LLM-process at most this many queued articles")
    ap.add_argument("--dry-run", action="store_true", help="list the links that would be imported; write nothing")
    ap.add_argument("--import-only", action="store_true", help="queue links but skip the LLM pass")
    ap.add_argument("--fix-sources", action="store_true",
                    help="only rename sources stored as '카카오톡 · <chat>' to the outlet, then exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    files = args.files or sorted((ROOT / "int" / "humint").glob("KakaoTalk_*.txt"))
    if not files and not args.fix_sources:
        sys.exit("no KakaoTalk_*.txt files found")
    cfg = load_config(args.config)
    database = cfg.database if Path(cfg.database).is_absolute() else str(ROOT / cfg.database)
    conn = db.connect(database)
    conn.executescript(SCHEMA)
    names = outlet_names(cfg)
    if args.fix_sources:
        print(f"renamed the source of {fix_sources(conn, names)} articles")
        return
    if not args.dry_run:
        fixed = fix_sources(conn, names)
        if fixed:
            log.info("renamed the source of %d articles from older imports", fixed)
    n = import_links(conn, files, args.since, dry_run=args.dry_run, names=names)
    print(f"{'would queue' if args.dry_run else 'queued'} {n} new links")
    if args.dry_run or args.import_only:
        return
    print("LLM pass:", process(conn, cfg, args.limit))


if __name__ == "__main__":
    main()
