import asyncio
import logging

import httpx

from . import db, dedupe, extractor, feeds
from .config import load_config

log = logging.getLogger("newscollect")
MAX_ATTEMPTS = 3


def collect_once(conn, cfg) -> dict:
    stats = {"new": 0, "incidents": 0, "skipped": 0, "failed": 0}
    with httpx.Client(timeout=20) as web, httpx.Client(timeout=cfg.llm_timeout) as llm:
        for src in (s for s in cfg.sources if s.enabled):
            try:
                items = feeds.fetch_feed(web, src.url, cfg.max_articles_per_run, cfg.max_age_days)
            except Exception as e:
                log.warning("feed %s failed: %s", src.url, e)
                continue
            for it in items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO articles(source,url,title,published_at,body) VALUES(?,?,?,?,?)",
                    (src.name, it["url"], it["title"], it["published_at"], it["snippet"]))
                if cur.rowcount:
                    stats["new"] += 1
            conn.commit()
        pending = conn.execute(
            "SELECT * FROM articles WHERE status IN ('new','failed') AND attempts < ? ORDER BY published_at",
            (MAX_ATTEMPTS,)).fetchall()
        for a in pending:
            art = dict(a)
            try:
                art["body"] = feeds.fetch_body(web, art["url"], art["body"])
                ex = extractor.extract(llm, cfg, art)
                if ex.is_incident:
                    inc = dedupe.upsert_incident(conn, ex, art["id"])
                    stats["incidents"] += 1
                    log.info("incident %s <- %s (%s)", inc, art["title"], ex.method)
                else:
                    stats["skipped"] += 1
                conn.execute("UPDATE articles SET body=?, status=?, attempts=attempts+1, error=NULL WHERE id=?",
                             (art["body"], "done" if ex.is_incident else "skipped", art["id"]))
            except Exception as e:
                stats["failed"] += 1
                conn.execute("UPDATE articles SET status='failed', attempts=attempts+1, error=? WHERE id=?",
                             (str(e)[:300], art["id"]))
            conn.commit()
    return stats


async def run_forever(conn, config_path=None):
    while True:
        cfg = load_config(config_path)  # re-read: source/LLM edits apply live
        try:
            stats = await asyncio.to_thread(collect_once, conn, cfg)
            log.info("collect done: %s", stats)
        except Exception:
            log.exception("collect failed")
        await asyncio.sleep(cfg.interval_minutes * 60)
