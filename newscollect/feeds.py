"""RSS fetching and article body extraction."""
import re
from datetime import date, datetime, timedelta

import feedparser
import httpx
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (compatible; newscollect/1.0)"}
# most specific first; "article" last because on some sites it is only a small wrapper
BODY_SELECTORS = ["#news_body_area", "#article-view-content-div", ".articleBody", "#articlebody", "[itemprop=articleBody]",
                  ".post-body", ".entry-content", ".article_txt", "#articleBody", "article"]
NOISE_TAGS = ["script", "style", "noscript", "nav", "aside", "form", "iframe", "button", "svg", "figure", "header", "footer"]
BLOCKS = ["p", "li", "h2", "h3", "h4", "blockquote", "pre"]
MIN_BODY_CHARS = 200
RELATED = re.compile(r"(related\s+(articles?|stories|posts?|reading)|read\s+more|also\s+read|you\s+may\s+also\s+like|recommended)\b", re.I)


# WordPress feeds end every item with "The post <title> appeared first on <site>." (SecurityWeek and others)
_WP_FOOTER = re.compile(r"\s*The post .{1,400}? appeared first on [^.]{1,80}\.?\s*$", re.S)


def _clean(html: str) -> str:
    text = re.sub(r"\s+", " ", BeautifulSoup(html or "", "lxml").get_text(" ")).strip()
    return _WP_FOOTER.sub("", text).strip()


def _entry_date(e) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return date(t.tm_year, t.tm_mon, t.tm_mday).isoformat()
    raw = e.get("published") or e.get("updated") or ""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return m.group(0)
    return _loose_date(raw)


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _loose_date(raw: str) -> str | None:
    """Dates feedparser can't read: "Nov 05, 2026 00:00:00-0600" (CrowdStrike), "5 November 2026"."""
    m = (re.search(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", raw)
         or re.search(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})\b", raw))
    if not m:
        return None
    a, b, year = m.groups()
    mon, day = (a, b) if a[0].isalpha() else (b, a)
    try:
        return date(int(year), _MONTHS[mon.lower()], int(day)).isoformat()
    except (KeyError, ValueError):
        return None


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
        snippet = _clean(e.get("description", ""))
        full = _clean(e["content"][0].get("value", "")) if e.get("content") else ""
        items.append({"url": link, "title": _clean(e.get("title", "")), "published_at": pub,
                      "snippet": full if len(full) > len(snippet) else snippet})
    return items


def _node_text(node) -> str:
    """Article text with paragraph breaks. Inline tags are joined without a space (so "<a>Acme</a>'s" stays
    "Acme's"); falls back to plain text when the page has no <p>-style blocks."""
    for t in node.find_all(NOISE_TAGS):
        t.decompose()
    blocks = [b for b in node.find_all(BLOCKS) if not b.find_parent(BLOCKS)]
    parts = [p for p in (re.sub(r"\s+", " ", b.get_text("")).strip() for b in blocks) if p]
    for i, part in enumerate(parts):  # "Related Articles:" lists other stories: not part of this one
        if i >= 2 and RELATED.match(part):
            parts = parts[:i]
            break
    text = "\n".join(parts)
    if len(text) < MIN_BODY_CHARS:
        text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
    return text


def extract_article_text(html: bytes) -> str:
    soup = BeautifulSoup(html, "lxml")
    for sel in BODY_SELECTORS:
        node = soup.select_one(sel)
        if node:
            text = _node_text(node)
            if len(text) >= MIN_BODY_CHARS:
                return text
    # unknown layout: the element that directly holds the most paragraph text
    best, best_len = None, 0
    for tag in soup.find_all(True):
        n = sum(len(p.get_text(" ", strip=True)) for p in tag.find_all("p", recursive=False))
        if n > best_len:
            best, best_len = tag, n
    return _node_text(best) if best is not None and best_len >= MIN_BODY_CHARS else ""


def fetch_body(client: httpx.Client, url: str, fallback: str = "") -> str:
    """Full article text; falls back to the RSS snippet on any failure."""
    try:
        r = client.get(url, headers=UA, follow_redirects=True)
        r.raise_for_status()
        text = extract_article_text(r.content)
        if len(text) > MIN_BODY_CHARS:
            return text[:6000]
    except Exception:
        pass
    return fallback[:6000]
