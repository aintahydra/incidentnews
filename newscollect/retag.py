"""Re-tag stored incidents with the current tag rules (attacker, victim, system, technique, type).

    python -m newscollect retag          # every incident
    python -m newscollect retag 12 40    # just these ids

Only the hashtags column changes. Uses the LLM from config.toml [llm]; the article text stored at collection time."""
import json
import logging

import httpx

from . import extractor, llm as llmmod

log = logging.getLogger("newscollect")


def retag(conn, cfg, ids=None) -> dict:
    stats = {"updated": 0, "unchanged": 0, "failed": 0}
    where, args = ("WHERE id IN (%s)" % ",".join("?" * len(ids)), list(ids)) if ids else ("", [])
    incidents = conn.execute(f"SELECT * FROM incidents {where} ORDER BY id", args).fetchall()
    with httpx.Client(timeout=cfg.llm_timeout) as http:
        llm = llmmod.from_config(cfg, http)
        for inc in incidents:
            arts = conn.execute("SELECT a.title, a.body FROM articles a JOIN incident_articles ia ON ia.article_id=a.id "
                                "WHERE ia.incident_id=? ORDER BY a.published_at", (inc["id"],)).fetchall()
            article = {"title": arts[0]["title"] if arts else inc["title"],
                       "body": "\n\n".join(a["body"] or "" for a in arts)[:4000] or inc["summary"]}
            fields = {"사고 제목": inc["title"], "공격자": inc["attacker"], "피해 대상": inc["victim"],
                      "피해 규모": inc["damage"], "요약": inc["summary"]}
            model = extractor.model_for(cfg, extractor.is_korean(article["title"] + " " + article["body"][:600]))
            try:
                tags = extractor.extract_tags(llm, model, article, fields)
            except llmmod.LLMUnavailable:
                raise
            except Exception as e:  # bad JSON / refusal: keep the old tags
                log.warning("#%s %s: tags failed (%s)", inc["id"], inc["title"], e)
                stats["failed"] += 1
                continue
            new = json.dumps(tags, ensure_ascii=False)
            if not tags or new == inc["hashtags"]:
                stats["unchanged"] += 1
                continue
            conn.execute("UPDATE incidents SET hashtags=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (new, inc["id"]))
            conn.commit()
            stats["updated"] += 1
            log.info("#%s %s: %s", inc["id"], inc["title"], " ".join("#" + t for t in tags))
    return stats
