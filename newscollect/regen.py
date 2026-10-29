"""Re-generate an incident's summary from its linked articles (LLM only, no heuristic fallback)."""
import time

import httpx

from . import extractor, feeds

MAX_BODY_CHARS = 4000
MAX_ARTICLES = 3
TEMPERATURE = 0.4  # temperature 0 would just reproduce the same (bad) summary
BUDGET_SECONDS = 240  # the browser is waiting; give up rather than hold the request for many minutes
FIELDS = ("title", "attacker", "victim", "damage", "category")


class NotAnIncident(ValueError):
    pass


def regenerate(conn, lock, cfg, inc_id: int) -> dict:
    if not cfg.llm_enabled:
        raise RuntimeError("config.toml에서 LLM이 비활성화되어 있습니다")
    with lock:
        inc = conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        if inc is None:
            raise LookupError(inc_id)
        arts = [dict(r) for r in conn.execute(
            "SELECT a.* FROM articles a JOIN incident_articles ia ON ia.article_id=a.id "
            "WHERE ia.incident_id=? ORDER BY a.published_at", (inc_id,))][:MAX_ARTICLES]
    per_article = MAX_BODY_CHARS // max(len(arts), 1)
    deadline = time.monotonic() + BUDGET_SECONDS
    with httpx.Client(timeout=20) as web, httpx.Client(timeout=cfg.llm_timeout) as llm:
        for a in arts:  # the stored body may be only the RSS snippet; try for the full text
            a["body"] = feeds.fetch_body(web, a["url"], a["body"])
        headline = arts[0]["title"] if arts else inc["title"]
        combined = {
            "published_at": inc["occurred_at"],
            "title": headline,
            "body": "\n\n".join(f"[{a['source']}] {a['title']}\n{a['body'][:per_article]}" for a in arts),
        }
        source_text = combined["title"] + " " + combined["body"][:MAX_BODY_CHARS]
        fields = {k: inc[k] for k in FIELDS}
        kw = dict(num_ctx=cfg.llm_num_ctx, deadline=deadline)

        # 1) fresh structured fields, name-checked against the article
        try:
            ex = extractor.extract_llm(llm, cfg.llm_base_url, cfg.llm_model, combined, temperature=TEMPERATURE, **kw)
        except ValueError:
            ex = None  # validation failed (e.g. garbled company name): repair the stored fields below
        if ex is not None:
            if not ex.is_incident:
                raise NotAnIncident("LLM이 이 기사를 사고로 판단하지 않아 기존 요약을 유지합니다")
            fields.update({k: getattr(ex, k) for k in FIELDS if getattr(ex, k)})
        bad = extractor.ungrounded_names(
            extractor.Extraction(True, victim=fields["victim"], attacker=fields["attacker"]), source_text)
        fields = extractor.repair_fields(fields, source_text, headline)

        # 2) the summary itself, written as prose
        method = "llm"
        try:
            summary = extractor.write_summary(llm, cfg.llm_base_url, cfg.llm_model, combined, fields,
                                              temperature=TEMPERATURE, forbidden=bad, **kw)
        except ValueError:
            # The model could not write an acceptable summary. Keep the stored one unless it is itself
            # broken (wrong name / memo style); then the article's own lead sentences are the better text.
            lead = extractor.extractive_summary(arts[0]["body"]) if arts else ""
            old_broken = bool(bad) or bool(extractor.summary_problems(inc["summary"], bad, source_text))
            if not (lead and not extractor.summary_problems(lead) and old_broken):
                raise
            summary, method = lead, "extract"

    # occurred_at is kept (dedupe depends on it)
    upd = {**fields, "summary": summary}
    with lock:
        for a in arts:
            conn.execute("UPDATE articles SET body=? WHERE id=?", (a["body"], a["id"]))
        sets = ",".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE incidents SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*upd.values(), inc_id))
        conn.commit()
        return {**dict(conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()), "method": method}
