"""Reading one web page into an article, and the LLM pass over queued articles. Shared by
`python -m newscollect add URL` (addurl.py) and the KakaoTalk importer (scripts/KakaotalkParser)."""
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from . import dedupe, extractor, feeds, intel, llm as llmmod

log = logging.getLogger("newscollect.pages")
MAX_ATTEMPTS = 3

DROP_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|page|mkind|kind|tab_type|ref|from)$", re.I)


# outlet names for sites that are not (or not under this host) in config.toml's feed lists
OUTLETS = {
    "boannews.com": "보안뉴스", "dailysecu.com": "데일리시큐", "securityweek.com": "Security Week",
    "thehackernews.com": "The Hacker News", "darkreading.com": "Dark Reading", "bleepingcomputer.com": "Bleeping Computer",
    "zdnet.co.kr": "지디넷코리아", "yna.co.kr": "연합뉴스", "etnews.com": "전자신문", "edaily.co.kr": "이데일리",
    "newsis.com": "뉴시스", "chosun.com": "조선일보", "donga.com": "동아일보", "joongang.co.kr": "중앙일보",
    "hani.co.kr": "한겨레", "mk.co.kr": "매일경제", "hankyung.com": "한국경제", "digitaltoday.co.kr": "디지털투데이",
    "itdaily.kr": "아이티데일리", "datanet.co.kr": "데이터넷", "ddaily.co.kr": "디지털데일리", "inews24.com": "아이뉴스24",
    "dt.co.kr": "디지털타임스", "boho.or.kr": "KISA 보호나라", "kisa.or.kr": "한국인터넷진흥원", "kiisc.or.kr": "한국정보보호학회",
    "v.daum.net": "Daum 뉴스", "n.news.naver.com": "네이버 뉴스", "reuters.com": "Reuters", "theregister.com": "The Register",
    "securityonline.info": "SecurityOnline", "cisa.gov": "CISA", "microsoft.com": "Microsoft", "cloud.google.com": "Google Cloud",
    "github.com": "GitHub", "nikkei.com": "日本経済新聞", "wired.com": "WIRED", "techcrunch.com": "TechCrunch",
    "therecord.media": "The Record", "cyberscoop.com": "CyberScoop", "infosecurity-magazine.com": "Infosecurity Magazine",
}
FEED_HOSTS = re.compile(r"^(feeds\.feedburner\.com|feeds\.|rss\.)")  # feed hosts that say nothing about the outlet


def bare_host(url: str) -> str:
    return re.sub(r"^(www\.|m\.|mobile\.)", "", (urlsplit(url).hostname or "").lower())


def outlet_names(cfg) -> dict[str, str]:
    """host -> outlet name: the built-in list, overridden by the names config.toml gives its feeds' sites."""
    names = dict(OUTLETS)
    for src in list(getattr(cfg, "sources", [])) + list(getattr(cfg, "intel_sources", [])):
        host = bare_host(src.url)
        if host and not FEED_HOSTS.match(host):
            names[host] = src.name
    return names


def outlet(url: str, site_name: str = "", names: dict | None = None) -> str:
    """The publisher of `url`: a known name for its host or a parent domain, else the page's og:site_name, else the host."""
    host, names = bare_host(url), names or OUTLETS
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in names:
            return names[".".join(parts[i:])]
    site = re.sub(r"\s+", " ", site_name or "").strip()
    return site[:40] if 2 <= len(site) <= 40 else host or url


def norm_url(u: str) -> str:
    """Comparison key: scheme, www./m. host prefix, fragment, tracking/paging params and trailing slash ignored."""
    p = urlsplit(u.strip())
    host = re.sub(r"^(www\.|m\.)", "", (p.hostname or "").lower())
    q = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not DROP_PARAMS.match(k)))
    return urlunsplit(("", host, p.path.rstrip("/") or "/", q, ""))


def page_date(soup) -> str | None:
    for attrs in ({"property": "article:published_time"}, {"name": "article:published_time"}, {"itemprop": "datePublished"},
                  {"name": "pubdate"}, {"name": "date"}, {"property": "og:published_time"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and (m := re.search(r"(\d{4})-(\d{2})-(\d{2})", tag.get("content", ""))):
            return "-".join(m.groups())
    tag = soup.find("time", attrs={"datetime": True})
    if tag and (m := re.search(r"(\d{4})-(\d{2})-(\d{2})", tag["datetime"])):
        return "-".join(m.groups())
    return None


def fetch(client: httpx.Client, url: str) -> dict:
    """{'url': final URL, 'text', 'title', 'date', 'site', 'description'}; fields are '' (date None) when the page
    can't be read. 'site' is og:site_name; 'description' is og:description / meta description."""
    out = {"url": url, "text": "", "title": "", "date": None, "site": "", "description": ""}
    try:
        r = client.get(url, headers=feeds.UA, follow_redirects=True)
        final = str(r.url)
        if urlsplit(url).scheme == "https" and final.startswith("http://"):
            final = "https://" + final[len("http://"):]
        out["url"] = final
        r.raise_for_status()
        if "html" not in r.headers.get("content-type", "html"):
            return out
        soup = BeautifulSoup(r.content, "html.parser")
        og = soup.find("meta", attrs={"property": "og:title"})
        out["title"] = ((og.get("content") if og else "") or (soup.title.string if soup.title else "") or "").strip()
        out["date"] = page_date(soup)
        site = soup.find("meta", attrs={"property": "og:site_name"})
        out["site"] = (site.get("content") or "").strip() if site else ""
        desc = soup.find("meta", attrs={"property": "og:description"}) or soup.find("meta", attrs={"name": "description"})
        out["description"] = (desc.get("content") or "").strip() if desc else ""
        out["text"] = feeds.extract_article_text(r.content)
    except Exception as e:
        log.debug("fetch %s: %s", url, e)
    return out


def process_queued(conn, cfg, status: str, ids=None, limit=None) -> dict:
    """LLM pass over articles with `status` (only `ids` when given), like collector.collect_once: the incident
    extractor first (merging into existing incidents); if not an incident, the intel extractor; else skipped.
    Articles end as done / skipped, or failed after MAX_ATTEMPTS. Returns counts plus
    results[{id, title, outcome: incident|intel|skipped|failed|queued, ref (incident id / intel kind), error}]."""
    stats = {"incidents": 0, "intel": 0, "skipped": 0, "failed": 0, "results": []}
    sql, args = "SELECT * FROM articles WHERE status=? AND attempts < ?", [status, MAX_ATTEMPTS]
    if ids is not None:
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    pending = conn.execute(sql + " ORDER BY published_at, id", args).fetchall()
    if limit is not None:
        pending = pending[:limit]
    queued = lambda rows: [{"id": a["id"], "title": a["title"], "outcome": "queued", "ref": None, "error": None}
                           for a in rows]
    if not cfg.llm_enabled:
        log.error("[llm] is disabled in config.toml; articles stay queued (status '%s')", status)
        stats["results"] = queued(pending)
        return stats
    log.info("LLM pass: %d queued articles", len(pending))
    with httpx.Client(timeout=cfg.llm_timeout) as http:
        try:
            llm = llmmod.from_config(cfg, http)
        except llmmod.LLMUnavailable as e:
            log.error("%s; articles stay queued", e)
            stats["results"] = queued(pending)
            return stats
        judge = dedupe.llm_judge(llm, llmmod.main_model(cfg))
        for n, a in enumerate(pending, 1):
            art, t0 = dict(a), time.time()
            res = {"id": art["id"], "title": art["title"], "outcome": "skipped", "ref": None, "error": None}
            try:
                ex = extractor.extract(llm, cfg, art)
                if ex.is_incident:
                    inc = dedupe.upsert_incident(conn, ex, art["id"], judge)
                    section, new_status, what = "incident", "done", f"incident #{inc}"
                    res.update(outcome="incident", ref=inc)
                    stats["incidents"] += 1
                else:
                    korean = extractor.is_korean(art["title"] + " " + art["body"][:600])
                    entry = intel.extract(llm, llmmod.model_for(cfg, korean), art)
                    if entry:
                        intel.save(conn, art["id"], entry, art["published_at"])
                        section, new_status, what = "intel", "done", f"intel [{entry.kind}]"
                        res.update(outcome="intel", ref=entry.kind)
                        stats["intel"] += 1
                    else:
                        section, new_status, what = "incident", "skipped", "skipped"
                        stats["skipped"] += 1
                conn.execute("UPDATE articles SET section=?, status=?, attempts=attempts+1, error=NULL WHERE id=?",
                             (section, new_status, art["id"]))
                log.info("[%d/%d] %s (%.0fs) <- %s", n, len(pending), what, time.time() - t0, art["title"][:70])
            except llmmod.LLMUnavailable as e:
                log.error("LLM unreachable (%s); the rest stay queued", e)
                stats["results"] += queued(pending[n - 1:])
                break
            except Exception as e:
                stats["failed"] += 1
                res.update(outcome="failed", error=str(e)[:300])
                conn.execute("UPDATE articles SET attempts=attempts+1, error=?, "
                             "status=CASE WHEN attempts+1 >= ? THEN 'failed' ELSE status END WHERE id=?",
                             (str(e)[:300], MAX_ATTEMPTS, art["id"]))
                log.warning("[%d/%d] failed: %s <- %s", n, len(pending), e, art["title"][:70])
            stats["results"].append(res)
            conn.commit()
    return stats
