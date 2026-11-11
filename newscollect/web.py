import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import collector, db, intel, orgs, regen, stats, tagcloud, taggraph
from .config import load_config

BASE = Path(__file__).parent
WEEKDAYS = "월화수목금토일"
REGEN_COOLDOWN = 20  # seconds, per incident
PAGE = 100  # cards per "더 보기" step on the 보안 사고 / 인텔·권고 tabs (?n= shows the first n)
SEARCH_FIELDS = ("title", "summary", "attacker", "victim", "damage", "category", "hashtags")
INTEL_SEARCH_FIELDS = ("i.title", "i.summary", "i.action", "i.kind", "i.hashtags", "a.title", "a.source")
SECTIONS = ("incident", "intel")  # card sections; the header also has the "graph" page (태그간 관계 그래프)
INTEL_TABS = {"advisory": "권고", "threat": "위협 분석", "guide": "가이드", "policy": "정책"}  # tab key -> intel.kind
# when the news came out: the earliest linked article's publication date (feed date; collection date if missing).
# The timeline and the tag cloud use this; occurred_at (the incident date the LLM extracted) is shown on the card.
POSTED = ("COALESCE((SELECT MIN(COALESCE(NULLIF(a.published_at,''), date(a.fetched_at,'localtime'))) "
          "FROM incident_articles ia JOIN articles a ON a.id=ia.article_id WHERE ia.incident_id=incidents.id), occurred_at)")


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
    graph_cache: dict[str, tuple] = {}  # scope -> (version, result); recomputed when cards change
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
        rows = [tagged(r) for r in q(f"SELECT *, {POSTED} AS posted_at FROM incidents {where} "
                                     "ORDER BY posted_at DESC, occurred_at DESC, id DESC", args)]
        by_tab = {"all": rows}
        for name, tags in orgs.TAB_TAGS.items():
            by_tab[name] = [r for r in rows if set(r["org_tags"]) & set(tags)]
        page = by_tab.get(tab, rows)[offset:offset + limit]
        for r in page:
            r["sources"] = sources_of(r["id"])
        return page, {k: len(v) for k, v in by_tab.items()}

    def intel_listing(query="", tab="all", limit=300, offset=0):
        """(page of intel entries for the kind tab, {tab: count}), newest publication first."""
        clauses, args = [], []
        for term in query.split()[:5]:
            clauses.append("(" + " OR ".join(f"{f} LIKE ? ESCAPE '\\'" for f in INTEL_SEARCH_FIELDS) + ")")
            args += [_like(term)] * len(INTEL_SEARCH_FIELDS)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        rows = q("SELECT i.*, a.source, a.url, a.title AS article_title FROM intel i JOIN articles a ON a.id=i.article_id "
                 f"{where} ORDER BY i.published_at DESC, i.id DESC", args)
        for r in rows:
            try:
                r["hashtags"] = [t for t in json.loads(r.get("hashtags") or "[]") if isinstance(t, str)]
            except ValueError:
                r["hashtags"] = []
        by_tab = {"all": rows, **{k: [r for r in rows if r["kind"] == kind] for k, kind in INTEL_TABS.items()}}
        return by_tab.get(tab, rows)[offset:offset + limit], {k: len(v) for k, v in by_tab.items()}

    def version():
        """Changes whenever an incident or intel entry is added, merged or rewritten (and articles: the charts)."""
        r = q("SELECT COUNT(*) n, MAX(id) m, MAX(updated_at) u, (SELECT COUNT(*) FROM articles) a FROM incidents")[0]
        i = q("SELECT COUNT(*) n, MAX(id) m, MAX(updated_at) u FROM intel")[0]
        return {"incidents": f"{r['n']}-{r['m'] or 0}-{r['u'] or ''}|{i['n']}-{i['m'] or 0}-{i['u'] or ''}",
                "articles": r["a"]}

    def one(inc_id):
        rows = q(f"SELECT *, {POSTED} AS posted_at FROM incidents WHERE id=?", (inc_id,))
        if not rows:
            raise HTTPException(404, "사고를 찾을 수 없습니다")
        rows[0]["sources"] = sources_of(inc_id)
        return tagged(rows[0])

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, q: str = "", tab: str = "all", section: str = "incident", n: int = PAGE):
        q = q.strip()[:100]
        n = max(PAGE, min(n, 10000))
        try:  # re-read so a [server].refresh_seconds edit applies on the next page load
            refresh = load_config(config_path).refresh_seconds
        except Exception:
            refresh = cfg.refresh_seconds
        section = section if section in SECTIONS + ("graph",) else "incident"
        inc_rows, inc_counts = listing(limit=n, query=q, tab=tab if tab in orgs.TAB_TAGS else "all")
        intel_rows, intel_counts = intel_listing(limit=n, query=q, tab=tab if tab in INTEL_TABS else "all")
        if section == "graph":  # one graph over all 글타래 for now (the API also takes scope=incident|intel)
            tab = "all"
            rows, counts, date_key = [], {k: 0 for k in taggraph.SCOPES}, "posted_at"
        elif section == "intel":
            tab = tab if tab in INTEL_TABS else "all"
            rows, counts, date_key = intel_rows, intel_counts, "published_at"
        else:
            tab = tab if tab in orgs.TAB_TAGS else "all"
            rows, counts, date_key = inc_rows, inc_counts, "posted_at"
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r[date_key], []).append(r)
        days = [{"date": d, "weekday": WEEKDAYS[date.fromisoformat(d).weekday()], "items": items}
                for d, items in groups.items()]
        more = None  # link to the same list with the next PAGE cards (None when everything is shown)
        if section != "graph" and len(rows) < counts[tab]:
            params = {k: v for k, v in (("q", q), ("section", section if section == "intel" else ""),
                                        ("tab", tab if tab != "all" else "")) if v}
            more = "/?" + urlencode({**params, "n": n + PAGE})
        return templates.TemplateResponse(request, "graph.html" if section == "graph" else "index.html",
                                          {"title": cfg.site_title, "days": days, "q": q, "total": counts[tab],
                                           "tab": tab, "counts": counts, "version": version(), "refresh": refresh,
                                           "section": section, "intel_tabs": INTEL_TABS,
                                           "shown": len(rows), "more": more, "step": PAGE,
                                           "section_counts": {"incident": inc_counts["all"],
                                                              "intel": intel_counts["all"]}})

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

    @app.get("/api/intel")
    def api_intel(limit: int = 100, offset: int = 0, q: str = "", tab: str = "all"):
        if tab != "all" and tab not in INTEL_TABS:
            raise HTTPException(400, "tab은 all, " + ", ".join(INTEL_TABS) + " 중 하나여야 합니다")
        return intel_listing(q.strip()[:100], tab, max(1, min(limit, 500)), max(0, offset))[0]

    @app.get("/api/tagcloud")
    def api_tagcloud(period: str = "week", limit: int = 60, section: str = "incident"):
        if period not in tagcloud.PERIODS:
            raise HTTPException(400, "period는 day, week, month, year 중 하나여야 합니다")
        if section not in SECTIONS:
            raise HTTPException(400, "section은 incident 또는 intel이어야 합니다")
        try:  # re-read so [tagcloud].exclude edits apply on the next page load, like the other live settings
            exclude = load_config(config_path).tagcloud_exclude
        except Exception:  # file mid-edit / invalid: keep the list from startup
            exclude = cfg.tagcloud_exclude
        start = tagcloud.since(period)
        rows = (q("SELECT hashtags FROM intel WHERE published_at >= ?", (start,)) if section == "intel"
                else q(f"SELECT hashtags FROM incidents WHERE {POSTED} >= ?", (start,)))
        return {"period": period, "since": start, "incidents": len(rows),
                "tags": tagcloud.cloud(rows, max(1, min(limit, 150)), exclude)}

    @app.get("/api/article-stats")
    def api_article_stats(unit: str = "day", section: str = ""):
        if unit not in stats.UNITS:
            raise HTTPException(400, "unit은 day, month, year 중 하나여야 합니다")
        if section and section not in SECTIONS:
            raise HTTPException(400, "section은 incident 또는 intel이어야 합니다")
        try:  # daily span = [collector].max_age_days (older feed items are never collected); re-read per request
            days = load_config(config_path).max_age_days
        except Exception:
            days = cfg.max_age_days
        days = max(1, min(int(days), 3660))
        with lock:
            series = stats.article_counts(conn, unit, days=days, section=section or None)
        return {"unit": unit, "total": sum(x["count"] for x in series), "series": series,
                "average": stats.average(series)}

    @app.get("/api/tag-graph")
    def api_tag_graph(scope: str = "all"):
        """Tag co-occurrence network with its significance tests (the 태그간 관계 그래프 page)."""
        if scope not in taggraph.SCOPES:
            raise HTTPException(400, "scope는 all, incident, intel 중 하나여야 합니다")
        ver = version()["incidents"]
        hit = graph_cache.get(scope)
        if hit and hit[0] == ver:
            return hit[1]
        with lock:
            cards = taggraph.load_cards(conn, scope)
        result = taggraph.analyze(cards)
        graph_cache[scope] = (ver, result)
        return result

    @app.get("/api/version")
    def api_version():
        """Polled by the list page to show new incidents without a manual refresh."""
        return version()

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "incidents": q("SELECT COUNT(*) c FROM incidents")[0]["c"]}

    return app


def app_factory():
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))  # also when started as `uvicorn newscollect.web:app_factory --factory`
    return create_app(os.environ.get("NEWSCOLLECT_CONFIG"))
