"""Add news articles by hand:  python -m newscollect add URL [URL ...]   (or ./addnews URL ...)

For each URL:
  1. If the article is already stored (compared after URL normalisation and redirects), say where and stop -
     unless an earlier `add` stored it but could not finish the LLM step; then retry that step.
  2. Read the page: title, text (og:description when the text can't be extracted), publication date (today when
     the page gives none) and the outlet (pages.outlet: config feed name > built-in list > og:site_name > host).
  3. Store it with status 'manual' (the server's collector ignores that status) and run the LLM from
     config.toml right away, the same way the collector does: incident extractor first, merging into an existing
     incident when it is the same event; else the intel extractor; else skipped.

The running server picks the new card up on its next /api/version poll; no restart is needed.
"""
import argparse
import re
from datetime import date

import httpx

from . import db, feeds, pages

STATUS = "manual"


def _card(conn, article_id: int) -> str:
    """Where an article shows up: its incident page, the intel tab, or why it doesn't."""
    row = conn.execute("SELECT incident_id FROM incident_articles WHERE article_id=?", (article_id,)).fetchone()
    if row:
        return f"보안 사고 /incidents/{row[0]}"
    row = conn.execute("SELECT kind FROM intel WHERE article_id=?", (article_id,)).fetchone()
    if row:
        return f"인텔·권고 [{row[0]}]"
    st = conn.execute("SELECT status FROM articles WHERE id=?", (article_id,)).fetchone()[0]
    return {"skipped": "보안 사고·인텔이 아니라고 판단되어 표시하지 않음", "failed": "처리 실패"}.get(st, f"처리 대기 ({st})")


def _title(raw: str, site: str, source: str) -> str:
    """Page title without a trailing " - KBS 뉴스" style site name (only when it names the site)."""
    m = re.search(r"\s+[-|–—:]\s+([^-|–—:]{2,30})$", raw)
    if m and any(n and (n in m.group(1) or m.group(1) in n) for n in (site, source)):
        return raw[:m.start()].strip()
    return raw.strip()


def add(conn, cfg, urls: list[str], client: httpx.Client | None = None) -> list[dict]:
    """Store and process `urls`. Returns [{url, outcome, message}] with outcome one of
    added (then incident|intel|skipped|failed|queued in 'result'), exists, retried, error."""
    names = pages.outlet_names(cfg)
    known = {pages.norm_url(r["url"]): r["id"] for r in conn.execute("SELECT id, url FROM articles")}
    report, todo = [], []
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        for url in urls:
            url = url.strip()
            if not re.match(r"^https?://", url):
                report.append({"url": url, "outcome": "error", "message": "http(s):// 주소가 아닙니다"})
                continue
            aid = known.get(pages.norm_url(url))
            if aid is None:
                page = pages.fetch(client, url)
                aid = known.get(pages.norm_url(page["url"]))
            if aid is not None:
                st = conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()[0]
                if st == STATUS:  # stored by an earlier `add` whose LLM step did not finish
                    report.append({"url": url, "outcome": "retried", "id": aid, "message": "이전에 끝나지 않은 처리를 다시 시도"})
                    todo.append(aid)
                else:
                    report.append({"url": url, "outcome": "exists", "id": aid, "message": "이미 저장된 기사: " + _card(conn, aid)})
                continue
            text = page["text"] if len(page["text"]) >= feeds.MIN_BODY_CHARS else ""
            body = text or page["description"]
            if not page["title"] and not body:
                report.append({"url": url, "outcome": "error",
                               "message": "페이지를 읽지 못했습니다 (접근 차단, 로그인 필요, 또는 기사가 아닌 페이지)"})
                continue
            source = pages.outlet(page["url"], page["site"], names)
            today = date.today().isoformat()
            published = page["date"] if page["date"] and page["date"] <= today else today
            title = _title(page["title"], page["site"], source) or page["url"]
            cur = conn.execute("INSERT OR IGNORE INTO articles(source,url,title,published_at,body,section,status) "
                               "VALUES(?,?,?,?,?,'incident',?)",
                               (source, page["url"], title[:300], published, (body or title)[:6000], STATUS))
            conn.commit()
            aid = cur.lastrowid
            known[pages.norm_url(url)] = known[pages.norm_url(page["url"])] = aid
            note = "" if text else " (본문을 읽지 못해 요약문만 사용)"
            report.append({"url": url, "outcome": "added", "id": aid,
                           "message": f"{source} · {published} · {title[:80]}{note}"})
            todo.append(aid)
    finally:
        if own:
            client.close()
    if todo:
        results = {r["id"]: r for r in pages.process_queued(conn, cfg, STATUS, ids=todo)["results"]}
        for r in report:
            if r.get("id") in results:
                res = results[r["id"]]
                r["result"] = res["outcome"]
                r["card"] = (_card(conn, r["id"]) if res["outcome"] != "queued"
                             else "LLM을 쓸 수 없어 대기 중 - 같은 명령을 다시 실행하면 이어서 처리합니다")
                if res["error"]:
                    r["card"] += f" ({res['error']})"
    return report


def main(argv: list[str], cfg) -> int:
    ap = argparse.ArgumentParser(prog="python -m newscollect add", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="+", metavar="URL", help="article address(es)")
    args = ap.parse_args(argv)
    report = add(db.connect(cfg.database), cfg, args.urls)
    for r in report:
        print(f"\n{r['url']}\n  {r['message']}")
        if "card" in r:
            print(f"  → {r['card']}")
    return 1 if any(r["outcome"] == "error" or r.get("result") in ("failed", "queued") for r in report) else 0
