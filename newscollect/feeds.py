"""RSS fetching and article body extraction."""
import re
from datetime import date, datetime, timedelta

import feedparser
import httpx
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; newscollect/1.0)"}
BODY_SELECTORS = ["#news_body_area", "#article-view-content-div", "article", ".article_txt", "#articleBody"]


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "lxml").get_text(" ")).strip()


def _entry_date(e) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
    raw = e.get("published") or e.get("updated") or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return m.group(0) if m else None


def fetch_feed(client: httpx.Client, url: str, limit: int, max_age_days: int) -> list[dict]:
    r = client.get(url, headers=UA, follow_redirects=True)
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    items = []
    for e in parsed.entries[:limit]:
        link = e.get("link")
        if not link:
            continue
        pub = _entry_date(e) or date.today().isoformat()
        if pub < cutoff:
            continue
        items.append({"url": link, "title": _clean(e.get("title", "")),
                      "published_at": pub, "snippet": _clean(e.get("description", ""))})
    return items


def fetch_body(client: httpx.Client, url: str, fallback: str = "") -> str:
    """Full article text; falls back to the RSS snippet on any failure."""
    try:
        r = client.get(url, headers=UA, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "lxml")
        for sel in BODY_SELECTORS:
            node = soup.select_one(sel)
            if node:
                text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
                if len(text) > 200:
                    return text[:6000]
    except Exception:
        pass
    return fallback[:6000]
