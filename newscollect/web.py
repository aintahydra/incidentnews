import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import collector, db, orgs, regen, stats, wordcloud
from .config import load_config

BASE = Path(__file__).parent
WEEKDAYS = "월화수목금토일"
REGEN_COOLDOWN = 20  # seconds, per incident
SEARCH_FIELDS = ("title", "summary", "attacker", "victim", "damage", "category", "hashtags")


class NoCacheStatic(StaticFiles):
    """Always revalidate (ETag -> 304) so an edited style.css/app.js is never served stale."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def create_app(config_path=None, run_collector: bool = True) -> FastAPI:
    cfg = load_config(config_path)
    conn = db.connect(cfg.database)
    lock = threading.Lock()
    regen_busy = threading.Lock()  # one LLM regeneration at a time
    last_regen: dict[int, float] = {}
    classifier = orgs.Classifier(cfg.gov_list, cfg.public_list)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(collector.run_forever(conn, config_path)) if run_collector else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title=cfg.site_title, lifespan=lifespan)
    app.mount("/static", NoCacheStatic(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=BASE / "templates")

    def static_url(name: str) -> str:  # ?v=<mtime> busts browser caches when a file changes
        return f"/static/{name}?v={int((BASE / 'static' / name).stat().st_mtime)}"

    templates.env.globals["static_url"] = static_url

    def q(sql, args=()):
        with lock:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def sources_of(inc_id):
        return q("SELECT a.title,a.url,a.source FROM articles a JOIN incident_articles ia ON ia.article_id=a.id "
                 "WHERE ia.incident_id=? ORDER BY a.published_at", (inc_id,))

    def tagged(r):
        r["org_tags"] = classifier.tags(r["victim"])
        try:
            r["hashtags"] = [t for t in json.loads(r.get("hashtags") or "[]") if isinstance(t, str)]
        except ValueError:
            r["hashtags"] = []
        return r

    def listing(limit=200, offset=0, query="", tab="all"):
        """(page of incidents for the tab, {tab: count}); tags come from the victim name, so they follow list edits."""
        # every whitespace-separated term must match some field or a linked article title
        clauses, args = [], []
        for term in query.split()[:5]:
            like = _like(term)
            fields = " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in SEARCH_FIELDS)
            clauses.append(f"({fields} OR EXISTS (SELECT 1 FROM incident_articles ia JOIN articles a ON a.id=ia.article_id "
                           "WHERE ia.incident_id=incidents.id AND a.title LIKE ? ESCAPE '\\'))")
            args += [like] * (len(SEARCH_FIELDS) + 1)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = [tagged(r) for r in q(f"SELECT * FROM incidents {where} ORDER BY occurred_at DESC, id DESC", args)]
        by_tab = {"all": rows}
        for name, tags in orgs.TAB_TAGS.items():
            by_tab[name] = [r for r in rows if set(r["org_tags"]) & set(tags)]
        page = by_tab.get(tab, rows)[offset:offset + limit]
        for r in page:
            r["sources"] = sources_of(r["id"])
        return page, {k: len(v) for k, v in by_tab.items()}

    def one(inc_id):
        rows = q("SELECT * FROM incidents WHERE id=?", (inc_id,))
        if not rows:
            raise HTTPException(404, "사고를 찾을 수 없습니다")
        rows[0]["sources"] = sources_of(inc_id)
        return tagged(rows[0])

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", tab: str = "all"):
        q = q.strip()[:100]
        tab = tab if tab in orgs.TAB_TAGS else "all"
        groups: dict[str, list] = {}
        rows, counts = listing(query=q, tab=tab)
        for r in rows:
            groups.setdefault(r["occurred_at"], []).append(r)
        days = [{"date": d, "weekday": WEEKDAYS[date.fromisoformat(d).weekday()], "items": items}
                for d, items in groups.items()]
        return templates.TemplateResponse(request, "index.html",
                                          {"title": cfg.site_title, "days": days, "q": q, "total": counts[tab],
                                           "tab": tab, "counts": counts})

    @app.get("/incidents/{inc_id}", response_class=HTMLResponse)
    def detail(request: Request, inc_id: int):
        return templates.TemplateResponse(request, "detail.html", {"title": cfg.site_title, "inc": one(inc_id), "q": ""})

    @app.get("/incidents/{inc_id}/card", response_class=HTMLResponse)
    def card(request: Request, inc_id: int, view: str = "list"):
        if view == "detail":
            return templates.TemplateResponse(request, "_detail_card.html", {"inc": one(inc_id)})
        return templates.TemplateResponse(request, "_card.html", {"i": one(inc_id)})

    @app.post("/incidents/{inc_id}/regenerate")
    def regenerate(inc_id: int):
        one(inc_id)  # 404 if missing
        wait = REGEN_COOLDOWN - (time.monotonic() - last_regen.get(inc_id, -1e9))
        if wait > 0:
            raise HTTPException(429, f"{int(wait) + 1}초 후에 다시 시도하세요")
        if not regen_busy.acquire(blocking=False):
            raise HTTPException(429, "다른 요약을 생성 중입니다. 잠시 후 다시 시도하세요")
        try:
            result = regen.regenerate(conn, lock, load_config(config_path), inc_id)
            last_regen[inc_id] = time.monotonic()
            return result
        except HTTPException:
            raise
        except Exception as e:
            last_regen[inc_id] = time.monotonic()
            raise HTTPException(502, f"요약 생성 실패: {e}")
        finally:
            regen_busy.release()

    @app.get("/api/incidents")
    def api_incidents(limit: int = 100, offset: int = 0, q: str = "", tab: str = "all"):
        if tab != "all" and tab not in orgs.TAB_TAGS:
            raise HTTPException(400, "tab은 all 또는 gov여야 합니다")
        return listing(max(1, min(limit, 500)), max(0, offset), q.strip()[:100], tab)[0]

    @app.get("/api/wordcloud")
    def api_wordcloud(period: str = "week", limit: int = 60):
        if period not in wordcloud.PERIODS:
            raise HTTPException(400, "period는 day, week, month, year 중 하나여야 합니다")
        try:  # re-read so [wordcloud].trivial edits apply on the next page load, like the other live settings
            trivial = load_config(config_path).wordcloud_trivial
        except Exception:  # file mid-edit / invalid: keep the list from startup
            trivial = cfg.wordcloud_trivial
        start = wordcloud.since(period)
        rows = q("SELECT title, victim, attacker FROM incidents WHERE occurred_at >= ?", (start,))
        return {"period": period, "since": start, "incidents": len(rows),
                "words": wordcloud.cloud(rows, max(1, min(limit, 150)), trivial)}

    @app.get("/api/article-stats")
    def api_article_stats(unit: str = "day"):
        if unit not in stats.UNITS:
            raise HTTPException(400, "unit은 day, month, year 중 하나여야 합니다")
        with lock:
            series = stats.article_counts(conn, unit)
        return {"unit": unit, "total": sum(x["count"] for x in series), "series": series}

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "incidents": q("SELECT COUNT(*) c FROM incidents")[0]["c"]}

    return app


def app_factory():
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))  # also when started as `uvicorn newscollect.web:app_factory --factory`
    return create_app(os.environ.get("NEWSCOLLECT_CONFIG"))
