import asyncio
import logging

import httpx

from . import db, dedupe, extractor, feeds, intel, llm as llmmod
from .config import load_config

log = logging.getLogger("newscollect")
MAX_ATTEMPTS = 3


def collect_once(conn, cfg) -> dict:
    stats = {"new": 0, "incidents": 0, "intel": 0, "skipped": 0, "failed": 0}
    with httpx.Client(timeout=20) as web, httpx.Client(timeout=cfg.llm_timeout) as http:
        feeds_to_read = [(s, "incident") for s in cfg.sources if s.enabled]
        feeds_to_read += [(s, "intel") for s in getattr(cfg, "intel_sources", []) if s.enabled]
        for src, section in feeds_to_read:
            try:
                items = feeds.fetch_feed(web, src.url, cfg.max_articles_per_run, cfg.max_age_days)
            except Exception as e:
                log.warning("feed %s failed: %s", src.url, e)
                continue
            for it in items:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO articles(source,url,title,published_at,body,section) VALUES(?,?,?,?,?,?)",
                    (src.name, it["url"], it["title"], it["published_at"], it["snippet"], section))
                if cur.rowcount:
                    stats["new"] += 1
            conn.commit()
        llm = None
        if cfg.llm_enabled:
            try:
                llm = llmmod.from_config(cfg, http)
            except llmmod.LLMUnavailable as e:  # e.g. provider "claude" without ANTHROPIC_API_KEY
                log.error("%s; articles wait for the next cycle", e)
                return stats
        # the judge compares Korean summaries: the main model does this well, the foreign model was too conservative
        judge = dedupe.llm_judge(llm, llmmod.main_model(cfg)) if llm else None
        pending = conn.execute(
            "SELECT * FROM articles WHERE status IN ('new','failed') AND attempts < ? ORDER BY published_at",
            (MAX_ATTEMPTS,)).fetchall()
        # incidents before intel; Korean first within each (with a separate foreign_model the LLM server swaps
        # models less often)
        pending = sorted(pending, key=lambda a: (a["section"] == "intel",
                                                 not extractor.is_korean(a["title"] + " " + a["body"][:600])))
        for a in pending:
            art = dict(a)
            try:
                art["body"] = feeds.fetch_body(web, art["url"], art["body"])
                if art["section"] == "intel":
                    if not cfg.llm_enabled:  # no heuristic fallback: intel needs the LLM to be summarized in Korean
                        break
                    korean = extractor.is_korean(art["title"] + " " + art["body"][:600])
                    entry = intel.extract(llm, llmmod.model_for(cfg, korean), art)
                    if entry:
                        intel.save(conn, art["id"], entry, art["published_at"])
                        stats["intel"] += 1
                        log.info("intel [%s] <- %s", entry.kind, art["title"])
                    else:
                        stats["skipped"] += 1
                    conn.execute("UPDATE articles SET body=?, status=?, attempts=attempts+1, error=NULL WHERE id=?",
                                 (art["body"], "done" if entry else "skipped", art["id"]))
                    conn.commit()
                    continue
                ex = extractor.extract(llm, cfg, art)
                if ex.is_incident:
                    inc = dedupe.upsert_incident(conn, ex, art["id"], judge)
                    stats["incidents"] += 1
                    log.info("incident %s <- %s (%s)", inc, art["title"], ex.method)
                else:
                    stats["skipped"] += 1
                conn.execute("UPDATE articles SET body=?, status=?, attempts=attempts+1, error=NULL WHERE id=?",
                             (art["body"], "done" if ex.is_incident else "skipped", art["id"]))
            except llmmod.LLMUnavailable as e:
                # LLM unreachable: don't burn this article's retry attempts, and don't wait on every other one
                log.warning("LLM unreachable (%s); remaining articles wait for the next cycle", e)
                break
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
