import asyncio
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

from . import collector, db, regen
from .config import load_config

BASE = Path(__file__).parent
WEEKDAYS = "월화수목금토일"
REGEN_COOLDOWN = 20  # seconds, per incident
SEARCH_FIELDS = ("title", "summary", "attacker", "victim", "damage", "category")


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

    def listing(limit=200, offset=0, query=""):
        # every whitespace-separated term must match some field or a linked article title
        clauses, args = [], []
        for term in query.split()[:5]:
            like = _like(term)
            fields = " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in SEARCH_FIELDS)
            clauses.append(f"({fields} OR EXISTS (SELECT 1 FROM incident_articles ia JOIN articles a ON a.id=ia.article_id "
                           "WHERE ia.incident_id=incidents.id AND a.title LIKE ? ESCAPE '\\'))")
            args += [like] * (len(SEARCH_FIELDS) + 1)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = q(f"SELECT * FROM incidents {where} ORDER BY occurred_at DESC, id DESC LIMIT ? OFFSET ?",
                 (*args, limit, offset))
        for r in rows:
            r["sources"] = sources_of(r["id"])
        return rows

    def one(inc_id):
        rows = q("SELECT * FROM incidents WHERE id=?", (inc_id,))
        if not rows:
            raise HTTPException(404, "사고를 찾을 수 없습니다")
        rows[0]["sources"] = sources_of(inc_id)
        return rows[0]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = ""):
        q = q.strip()[:100]
        groups: dict[str, list] = {}
        rows = listing(query=q)
        for r in rows:
            groups.setdefault(r["occurred_at"], []).append(r)
        days = [{"date": d, "weekday": WEEKDAYS[date.fromisoformat(d).weekday()], "items": items}
                for d, items in groups.items()]
        return templates.TemplateResponse(request, "index.html",
                                          {"title": cfg.site_title, "days": days, "q": q, "total": len(rows)})

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
    def api_incidents(limit: int = 100, offset: int = 0, q: str = ""):
        return listing(max(1, min(limit, 500)), max(0, offset), q.strip()[:100])

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "incidents": q("SELECT COUNT(*) c FROM incidents")[0]["c"]}

    return app


def app_factory():
    return create_app(os.environ.get("NEWSCOLLECT_CONFIG"))
