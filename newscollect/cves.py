"""주요 CVE: CVEs from the [[cve_sources]] feeds, stored as the sources give them (no LLM).

Two feed formats are understood, chosen by the feed's host:
  - cvefeed.io  (e.g. /rssfeed/severity/high.xml): title "CVE-… - <title>", an HTML description with the English
    description and "Severity: 9.2 | CRITICAL". The CVE's detail page on cvefeed.io is read once afterwards for the
    affected products, the "Solution" section (-> remediation) and the reference links.
  - knvd.krcert.or.kr (KISA 보안취약점 정보포털): title "CVE-… | <Korean title>", a description with □ 개요 / □ 설명,
    a markdown table "CVE | CVSS | 심각도 | …" (one item may name several CVEs), a table of affected products with
    fixed versions (-> remediation) and □ 참고사이트 links.
Anything else is read generically: every CVE id in an item's title becomes a row with the item's text.

One row per CVE id; a CVE seen in several feeds is merged (empty fields filled, Korean and English descriptions both
kept, sources and references combined).

After the feeds, the official CVE Record of new CVEs is fetched in batches from the CVE Program's Search API
([cve_api], POST https://search.cve.org/search with cveIds; BETA, no key, ~1 request per cycle): vendor/product and
version ranges, CVSS vector, CWE, the CNA's solutions and workarounds, references, and CISA's enrichment
(SSVC exploitation, KEV). Recent records are refreshed once a day, since CISA adds its data later. The cvefeed.io
detail page is then read only for CVEs whose record left products or remediation empty.

CVEs mentioned on 보안 사고 / 인텔·권고 cards (tags, title, summary; intel also the action and the original
article title) are added too (`from_cards`, origin 'card'); their record fills in the details.
"""
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup

from . import feeds

log = logging.getLogger("newscollect.cves")
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.I)
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
SEVERITY_KO = {"CRITICAL": "치명적", "HIGH": "높음", "MEDIUM": "보통", "LOW": "낮음"}
DETAIL_PER_CYCLE = 40      # cvefeed.io detail pages read per collection cycle (politely, one at a time)
DETAIL_DELAY = 1.0         # seconds between detail pages
DETAIL_ATTEMPTS = 3
RECORD_BATCH = 100         # CVE ids per Search API request (cveIds is limited to 4096 characters)
RECORD_ATTEMPTS = 5        # cycles a CVE may be missing from the Search API before we stop asking
RECORD_REFRESH_DAYS = 30   # records published this recently are re-read once a day (CISA SSVC / KEV arrive later)
VERSION = "1.0"
EXPLOITATION_KO = {"active": "실제 악용 확인", "poc": "공개 PoC 있음", "none": "알려진 악용 없음"}
FIELDS = ("title", "description", "description_ko", "affected", "cvss", "cvss_version", "severity", "cwe",
          "remediation", "detection", "published_at")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cves (
  cve_id TEXT PRIMARY KEY,          -- CVE-YYYY-NNNN…, upper case
  title TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',     -- as the source gives it (cvefeed.io: English)
  description_ko TEXT NOT NULL DEFAULT '',  -- Korean description (KNVD 개요/설명)
  affected TEXT NOT NULL DEFAULT '',        -- short affected-system name for the list
  products TEXT NOT NULL DEFAULT '[]',      -- JSON [{product, affected, fixed}] (affected/fixed versions when known)
  cvss REAL,                                -- base score
  cvss_version TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT '',        -- CRITICAL | HIGH | MEDIUM | LOW
  cwe TEXT NOT NULL DEFAULT '',
  remediation TEXT NOT NULL DEFAULT '',
  detection TEXT NOT NULL DEFAULT '',
  refs TEXT NOT NULL DEFAULT '[]',          -- JSON [url]
  sources TEXT NOT NULL DEFAULT '[]',       -- JSON [{name, url}]: the feeds' pages for this CVE
  published_at TEXT NOT NULL DEFAULT '',    -- YYYY-MM-DD, the earliest feed date
  detail_url TEXT NOT NULL DEFAULT '',      -- cvefeed.io detail page still to read ('' = none / done)
  detail_attempts INTEGER NOT NULL DEFAULT 0,
  products_from TEXT NOT NULL DEFAULT '',   -- feed (KNVD table) | record (CVE.org) | detail (cvefeed.io page) | ''
  cvss_vector TEXT NOT NULL DEFAULT '',
  workarounds TEXT NOT NULL DEFAULT '',     -- the CNA's workarounds (CVE record)
  exploitation TEXT NOT NULL DEFAULT '',    -- CISA SSVC Exploitation: none | poc | active
  kev_added TEXT NOT NULL DEFAULT '',       -- YYYY-MM-DD the CVE entered CISA's KEV catalog
  record_status TEXT NOT NULL DEFAULT '',   -- '' to fetch | done | missing (CVE Search API)
  record_attempts INTEGER NOT NULL DEFAULT 0,
  record_fetched_at TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'feed',      -- feed ([[cve_sources]]) | card (only mentioned on 보안 사고 / 인텔·권고 cards)
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cves_published ON cves(published_at DESC);
"""


COLUMNS_ADDED = {  # columns newer than the first version of the table (migrate() adds them to older databases)
    "products_from": "TEXT NOT NULL DEFAULT ''", "cvss_vector": "TEXT NOT NULL DEFAULT ''",
    "workarounds": "TEXT NOT NULL DEFAULT ''", "exploitation": "TEXT NOT NULL DEFAULT ''",
    "kev_added": "TEXT NOT NULL DEFAULT ''", "record_status": "TEXT NOT NULL DEFAULT ''",
    "record_attempts": "INTEGER NOT NULL DEFAULT 0", "record_fetched_at": "TEXT NOT NULL DEFAULT ''",
    "origin": "TEXT NOT NULL DEFAULT 'feed'"}


def migrate(conn) -> None:
    have = {r[1] for r in conn.execute("PRAGMA table_info(cves)")}
    for col, decl in COLUMNS_ADDED.items():
        if col not in have:
            conn.execute(f"ALTER TABLE cves ADD COLUMN {col} {decl}")
    conn.commit()


# ---------------------------------------------------------------- parsing helpers

def _text(fragment: str) -> str:
    """Feed HTML -> plain text with line breaks kept."""
    s = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"\\([~*_\-\[\]()#>.!])", r"\1", s)  # markdown escapes (KNVD: "1.0 이상 \~ 2.0 미만")
    return "\n".join(line.rstrip() for line in s.replace("\r", "").split("\n"))


def _squeeze(s: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _date(entry) -> str:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return time.strftime("%Y-%m-%d", t) if t else datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _severity(word: str, score) -> str:
    w = (word or "").strip().upper()
    w = {"심각": "CRITICAL", "긴급": "CRITICAL", "높음": "HIGH", "중간": "MEDIUM", "보통": "MEDIUM", "낮음": "LOW"}.get(w, w)
    if w in SEVERITIES:
        return w
    if score is None:
        return ""
    return "CRITICAL" if score >= 9 else "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW" if score > 0 else ""


def _score(s) -> float | None:
    m = re.search(r"\d+(\.\d+)?", str(s or ""))
    return float(m.group()) if m and 0 <= float(m.group()) <= 10 else None


def _md_tables(text: str) -> list[list[list[str]]]:
    """Markdown tables in `text` as lists of rows of cells (separator rows dropped)."""
    tables, cur = [], []
    for line in text.split("\n") + [""]:
        line = line.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                cur.append(cells)
        elif cur:
            tables.append(cur)
            cur = []
    return tables


def _sections(text: str) -> dict[str, str]:
    """KNVD '□ heading' sections -> text."""
    out, name = {}, None
    for line in text.split("\n"):
        m = re.match(r"[\s#]*□\s*(.+)", line)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip()
            out[name] = ""
        elif name:
            out[name] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def short_affected(products: list[dict]) -> str:
    names = list(dict.fromkeys(p["product"] for p in products if p.get("product")))
    if not names:
        return ""
    return names[0] if len(names) == 1 else f"{names[0]} 외 {len(names) - 1}개"


VERSION_TAIL = re.compile(r"\s*((<=?|>=?|≤|before|prior to|through|versions?)\s*v?[\d.]|v?\d+\.\d+[\d.]*\s*-\s*\d).*$", re.I)


def affected_from_title(title: str, weak: bool = True) -> str:
    """A product name from a cvefeed.io title. Reliable forms: 'WordPress WP Rocket plugin < 3.24.2 - RCE' ->
    'WordPress WP Rocket plugin'; 'Apache JSPWiki: Session fixation…' -> 'Apache JSPWiki'. With `weak`, also
    guesses: '… in Opentext APM' -> 'Opentext APM'; 'Joomla Extension - virtuemart.net - …' -> 'Joomla Extension'."""
    t = title.strip()
    m = re.match(r"(.{3,80}?)\s+-\s+", t)
    if m and VERSION_TAIL.search(m.group(1)):          # "<product> < 1.2 - <what>"
        return VERSION_TAIL.sub("", m.group(1)).strip()
    m = re.match(r"([^:]{3,60}):\s", t)
    if m:
        return m.group(1).strip()
    if not weak:
        return ""
    if m := re.search(r".*\sin\s+(?:the\s+)?([^,;]{3,80})$", t):
        return VERSION_TAIL.sub("", m.group(1)).strip()
    m = re.match(r"(.{3,60}?)\s+-\s+", t)               # "Joomla Extension - virtuemart.net - …"
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- feed formats

def parse_cvefeed(entry) -> list[dict]:
    title = entry.get("title", "")
    m = re.match(r"\s*(CVE-\d{4}-\d{4,})\s*-\s*(.*)", title, re.I)
    if not m:
        return []
    text = _text(entry.get("summary", ""))
    desc = re.search(r"Description\s*:\s*(.*?)\n\s*Severity\s*:", text, re.S)
    sev = re.search(r"Severity\s*:\s*([\d.]+)\s*\|\s*([A-Z]+)", text)
    score = _score(sev.group(1)) if sev else None
    link = entry.get("link", "")
    return [{"cve_id": m.group(1).upper(), "title": m.group(2).strip(),
             "description": _squeeze(desc.group(1)) if desc else "",
             # a guess from the title waits for the detail page's product table (apply_detail)
             "affected": affected_from_title(m.group(2), weak="/vuln/detail/" not in link), "cvss": score,
             "severity": _severity(sev.group(2) if sev else "", score), "published_at": _date(entry),
             "source_url": link, "detail_url": link if "/vuln/detail/" in link else ""}]


def parse_knvd(entry) -> list[dict]:
    title = entry.get("title", "")
    text = _text(entry.get("summary", ""))
    sec = _sections(text)
    pick = lambda *names: next((sec[k] for k in sec for n in names if k.startswith(n)), "")
    body = lambda s: _squeeze("\n".join(re.sub(r"^\s*o\s+", "", ln) for ln in s.split("\n")
                                        if ln.strip() and not ln.strip().startswith("|")))
    desc_ko = _squeeze("\n".join(x for x in (body(pick("개요")), body(pick("설명"))) if x))
    products, remediation = [], []
    scores = {}
    for table in _md_tables(text):
        head = [c.replace(" ", "") for c in table[0]]
        if "CVE" in head:                                  # CVE | CVSS | 심각도 | CWE | 영향
            for row in table[1:]:
                cell = dict(zip(head, row))
                for cid in CVE_RE.findall(cell.get("CVE", "")):
                    scores[cid.upper()] = (_score(cell.get("CVSS")), cell.get("심각도", ""), cell.get("CWE", ""))
        elif any("제품" in h for h in head):                # 제품 | 영향받는 버전 | 해결 버전
            ip = next(i for i, h in enumerate(head) if "제품" in h)
            ia = next((i for i, h in enumerate(head) if "영향" in h and i != ip), None)
            ifx = next((i for i, h in enumerate(head) if "해결" in h), None)
            for row in table[1:]:
                get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
                p = {"product": get(ip), "affected": get(ia), "fixed": get(ifx)}
                if p["product"]:
                    products.append(p)
                    if p["fixed"]:
                        remediation.append(f"{p['product']}: {p['fixed']}" + (f" (영향: {p['affected']})" if p["affected"] else ""))
    notes = [re.sub(r"^\s*※\s*", "", ln).strip() for ln in pick("영향받는", "영향을").split("\n") if ln.strip().startswith("※")]
    if remediation:
        remediation = ["다음 버전 이상으로 업데이트:"] + ["- " + r for r in remediation]
    remediation += notes
    refs = re.findall(r"https?://[^\s<>\"')\]]+", pick("참고"))
    ko_title = re.sub(r"^\s*(CVE-\d{4}-\d{4,}[\s,]*)+\|?\s*", "", title, flags=re.I).strip()
    ids = list(dict.fromkeys(c.upper() for c in CVE_RE.findall(title))) or list(scores)
    out = []
    for cid in ids:
        score, sev, cwe = scores.get(cid, (None, "", ""))
        out.append({"cve_id": cid, "title": ko_title, "description_ko": desc_ko, "products": products,
                    "affected": short_affected(products) or ko_title, "cvss": score, "severity": _severity(sev, score),
                    "cwe": cwe, "remediation": "\n".join(remediation), "detection": pick("탐지", "점검"),
                    "refs": refs, "published_at": _date(entry), "source_url": entry.get("link", "")})
    return out


def parse_generic(entry) -> list[dict]:
    title = entry.get("title", "")
    text = _squeeze(_text(entry.get("summary", "")))
    return [{"cve_id": cid.upper(), "title": re.sub(r"^\s*CVE-\d{4}-\d{4,}\s*[-|:]?\s*", "", title, flags=re.I),
             "description": text, "published_at": _date(entry), "source_url": entry.get("link", "")}
            for cid in dict.fromkeys(CVE_RE.findall(title))]


def parser_for(url: str):
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("cvefeed.io"):
        return parse_cvefeed
    if host.endswith("knvd.krcert.or.kr"):
        return parse_knvd
    return parse_generic


def parse_feed(url: str, content: bytes) -> list[dict]:
    parse = parser_for(url)
    items = []
    for e in feedparser.parse(content).entries:
        try:
            items += parse(e)
        except Exception as ex:  # one odd item must not lose the rest
            log.warning("cve feed %s: item %r skipped: %s", url, e.get("title", "")[:60], ex)
    return items


def parse_cvefeed_detail(content: bytes) -> dict:
    """Affected products, Solution and references from a cvefeed.io /vuln/detail/ page."""
    soup = BeautifulSoup(content, "lxml")
    heading = lambda name: next((h for h in soup.find_all(["h4", "h5", "h6"])
                                 if h.get_text(" ", strip=True).startswith(name)), None)
    out = {"products": [], "remediation": "", "refs": [], "cvss_version": ""}
    h = heading("Affected Products")
    if h and (table := h.find_parent("div", class_="card-body").find("table")):
        for tr in table.select("tbody tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) >= 3 and cells[2]:
                vendor, product = cells[1], cells[2].replace("_", " ")
                if product.islower():
                    product = product.title()
                if vendor and vendor.lower() not in product.lower():
                    product = f"{vendor} {product}"
                out["products"].append({"product": product, "affected": "", "fixed": ""})
    h = heading("Solution")
    if h and (card := h.find_parent("div", class_="card")):
        body = card.find("div", class_="card-body")
        if body:
            lead = body.find("div", class_="card-text")
            items = [li.get_text(" ", strip=True) for li in body.find_all("li")]
            out["remediation"] = "\n".join(([lead.get_text(" ", strip=True)] if lead else []) + ["- " + i for i in items if i])
    h = heading("References to Advisories")
    if h and (card := h.find_parent("div", class_="card")):
        out["refs"] = list(dict.fromkeys(a["href"] for a in card.find_all("a", href=True) if a["href"].startswith("http")))
    m = re.search(r"CVSS\s+(\d\.\d)", soup.get_text(" ", strip=True))
    out["cvss_version"] = m.group(1) if m else ""
    return out


# ---------------------------------------------------------------- storage

def _loads(s, default):
    try:
        v = json.loads(s or "")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def upsert(conn, item: dict, source_name: str) -> bool:
    """Insert or merge one parsed CVE. True when the CVE is new."""
    cid = item["cve_id"]
    row = conn.execute("SELECT * FROM cves WHERE cve_id=?", (cid,)).fetchone()
    src = {"name": source_name, "url": item.get("source_url", "")}
    if row is None:
        vals = {f: item.get(f) if f == "cvss" else (item.get(f) or "") for f in FIELDS}
        conn.execute(f"INSERT INTO cves(cve_id,{','.join(FIELDS)},products,refs,sources,detail_url,products_from) "
                     f"VALUES(?,{','.join('?' * len(FIELDS))},?,?,?,?,?)",
                     (cid, *vals.values(), json.dumps(item.get("products") or [], ensure_ascii=False),
                      json.dumps(item.get("refs") or [], ensure_ascii=False),
                      json.dumps([src], ensure_ascii=False), item.get("detail_url", ""),
                      "feed" if item.get("products") else ""))
        return True
    cur = dict(row)
    upd = {}
    for f in FIELDS:
        new = item.get(f)
        if f == "published_at":
            if new and (not cur[f] or new < cur[f]):
                upd[f] = new
        elif new not in (None, "") and cur[f] in (None, ""):
            upd[f] = new
    products = _loads(cur["products"], [])
    if item.get("products") and cur["products_from"] != "feed":     # KNVD's table (Korean, fixed versions) wins
        upd["products"] = json.dumps(item["products"], ensure_ascii=False)
        upd["products_from"] = "feed"
        upd["affected"] = item.get("affected") or cur["affected"]   # a product table beats a name from a title
    refs = _loads(cur["refs"], [])
    more = [r for r in item.get("refs") or [] if r not in refs]
    if more:
        upd["refs"] = json.dumps(refs + more, ensure_ascii=False)
    sources = _loads(cur["sources"], [])
    if src["url"] and all(s.get("url") != src["url"] for s in sources):
        upd["sources"] = json.dumps(sources + [src], ensure_ascii=False)
    if cur["origin"] == "card":   # first seen on a card, now also in a CVE feed
        upd["origin"] = "feed"
    if item.get("detail_url") and not cur["detail_url"] and cur["detail_attempts"] == 0 and not products:
        upd["detail_url"] = item["detail_url"]
    if upd:
        conn.execute(f"UPDATE cves SET {', '.join(k + '=?' for k in upd)}, updated_at=CURRENT_TIMESTAMP WHERE cve_id=?",
                     (*upd.values(), cid))
    return False


def apply_detail(conn, cid: str, d: dict) -> None:
    cur = dict(conn.execute("SELECT * FROM cves WHERE cve_id=?", (cid,)).fetchone())
    upd = {"detail_url": ""}
    if d["products"] and not _loads(cur["products"], []):
        upd["products"] = json.dumps(d["products"], ensure_ascii=False)
        upd["products_from"] = "detail"
    if not cur["affected"]:  # no reliable name from the title: the product table, else a guess from the title
        upd["affected"] = short_affected(d["products"]) or affected_from_title(cur["title"])
    if d["remediation"] and not cur["remediation"]:
        upd["remediation"] = d["remediation"]
    if d["cvss_version"] and not cur["cvss_version"]:
        upd["cvss_version"] = d["cvss_version"]
    refs = _loads(cur["refs"], [])
    more = [r for r in d["refs"] if r not in refs]
    if more:
        upd["refs"] = json.dumps(refs + more, ensure_ascii=False)
    conn.execute(f"UPDATE cves SET {', '.join(k + '=?' for k in upd)}, updated_at=CURRENT_TIMESTAMP WHERE cve_id=?",
                 (*upd.values(), cid))


# ---------------------------------------------------------------- CVE.org records (CVE Search API)

def _versions(v: dict) -> tuple[str, str]:
    """(affected range, fixed version) of one CVE JSON 5 `versions` entry, in Korean."""
    start = str(v.get("version") or "").strip()
    start = "" if start in ("0", "*", "-", "n/a", "unspecified") else start
    lt, le = str(v.get("lessThan") or "").strip(), str(v.get("lessThanOrEqual") or "").strip()
    fixed = next((c.get("at", "") for c in v.get("changes") or [] if c.get("status") == "unaffected"), "")
    if lt and lt not in ("*",):
        rng = (f"{start} 이상 " if start else "") + f"{lt} 미만"
        fixed = fixed or (lt if re.fullmatch(r"v?[\d.]+[\w.-]*", lt) else "")
    elif le and le not in ("*",):
        rng = (f"{start} 이상 " if start else "") + f"{le} 이하"
    else:
        rng = start
    return rng, fixed


def parse_record(rec: dict) -> dict:
    """The parts of a CVE JSON 5 record the page shows."""
    cont = rec.get("containers") or {}
    cna, adps = cont.get("cna") or {}, cont.get("adp") or []
    en = lambda items: next((d.get("value", "") for d in items or [] if str(d.get("lang", "")).startswith("en")),
                            next((d.get("value", "") for d in items or []), ""))
    products = []
    for a in cna.get("affected") or []:
        vendor, product = (a.get("vendor") or "").strip(), (a.get("product") or "").strip()
        if vendor.lower() in ("n/a", "unknown"):
            vendor = ""
        if product.lower() in ("n/a", "unknown", ""):
            continue
        name = product if not vendor or vendor.lower() in product.lower() else f"{vendor} {product}"
        affected = [v for v in a.get("versions") or [] if v.get("status") == "affected"] or [{}]
        for v in affected[:5]:
            rng, fixed = _versions(v)
            products.append({"product": name, "affected": rng, "fixed": fixed})
    best = None  # CNA's own score, newest CVSS version first; else an ADP's
    for metrics in [cna.get("metrics") or []] + [a.get("metrics") or [] for a in adps]:
        for key, ver in (("cvssV4_0", "4.0"), ("cvssV3_1", "3.1"), ("cvssV3_0", "3.0"), ("cvssV2_0", "2.0")):
            m = next((x[key] for x in metrics if key in x), None)
            if m and m.get("baseScore") is not None:
                best = (m, ver)
                break
        if best:
            break
    cwe = []
    for pt in [cna.get("problemTypes") or []] + [a.get("problemTypes") or [] for a in adps]:
        for d in (x for p in pt for x in p.get("descriptions") or []):
            if d.get("cweId") and d["cweId"] not in cwe:
                cwe.append(d["cweId"])
    exploitation, kev = "", ""
    for a in adps:
        for m in a.get("metrics") or []:
            other = m.get("other") or {}
            content = other.get("content") or {}
            if other.get("type") == "ssvc":
                for opt in content.get("options") or []:
                    if "Exploitation" in opt:
                        exploitation = str(opt["Exploitation"]).lower()
            elif other.get("type") == "kev":
                kev = str(content.get("dateAdded") or "")[:10]
    refs = [r.get("url") for r in cna.get("references") or [] if _web(r.get("url"))]
    for a in adps:
        refs += [r.get("url") for r in a.get("references") or [] if _web(r.get("url"))]
    score = best[0].get("baseScore") if best else None
    return {"title": cna.get("title") or "", "description": en(cna.get("descriptions")), "products": products,
            "cvss": float(score) if score is not None else None, "cvss_version": best[1] if best else "",
            "cvss_vector": best[0].get("vectorString", "") if best else "",
            "severity": _severity(best[0].get("baseSeverity", "") if best else "", score),
            "cwe": ", ".join(cwe), "remediation": en(cna.get("solutions")), "workarounds": en(cna.get("workarounds")),
            "refs": list(dict.fromkeys(refs)), "exploitation": exploitation if exploitation in EXPLOITATION_KO else "",
            "kev_added": kev}


def apply_record(conn, cid: str, r: dict) -> None:
    """Merge a parsed CVE record. The record is authoritative for score, vector, CWE and (unless KNVD gave a
    product table) products; text fields only fill gaps; exploitation / KEV always follow the latest record."""
    cur = dict(conn.execute("SELECT * FROM cves WHERE cve_id=?", (cid,)).fetchone())
    upd = {"record_status": "done", "record_fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
           "exploitation": r["exploitation"], "kev_added": r["kev_added"]}
    if r["cvss"] is not None:
        upd.update(cvss=r["cvss"], cvss_version=r["cvss_version"], cvss_vector=r["cvss_vector"], severity=r["severity"])
    for f in ("title", "description", "cwe", "remediation", "workarounds"):
        if r[f] and not cur[f]:
            upd[f] = r[f]
    if r["products"] and cur["products_from"] != "feed":
        upd["products"] = json.dumps(r["products"], ensure_ascii=False)
        upd["products_from"] = "record"
        # the list keeps a readable name from a vendor-style title; a name built from cvefeed.io's CPE ids
        # ("Wpmedia Wp Rocket") or none at all gives way to the record's vendor + product
        if not cur["affected"] or cur["products_from"] == "detail":
            upd["affected"] = short_affected(r["products"])
    refs = _loads(cur["refs"], [])
    more = [u for u in r["refs"] if u not in refs]
    if more:
        upd["refs"] = json.dumps(refs + more, ensure_ascii=False)
    sources = _loads(cur["sources"], [])
    page = f"https://www.cve.org/CVERecord?id={cid}"
    if all(s.get("url") != page for s in sources):
        upd["sources"] = json.dumps(sources + [{"name": "CVE.org", "url": page}], ensure_ascii=False)
    # the record covered what the cvefeed.io page would add: don't read it
    if cur["detail_url"] and (upd.get("products") or cur["products"] != "[]") and (upd.get("remediation") or cur["remediation"]):
        upd["detail_url"] = ""
    conn.execute(f"UPDATE cves SET {', '.join(k + '=?' for k in upd)}, updated_at=CURRENT_TIMESTAMP WHERE cve_id=?",
                 (*upd.values(), cid))


def user_agent(cfg) -> dict:
    """The Search API asks distributed apps for name, version and contact in the User-Agent ([cve_api].contact)."""
    contact = (getattr(cfg, "cve_api_contact", "") or "").strip()
    return {"User-Agent": f"newscollect/{VERSION}" + (f" (+{contact})" if contact else ""),
            "Content-Type": "application/json"}


def fetch_records(conn, cfg, client: httpx.Client) -> int:
    """Fetch CVE records for new CVEs (and refresh recent ones once a day) in batches. Returns records applied."""
    if not getattr(cfg, "cve_api_enabled", False) or not getattr(cfg, "cve_api_url", ""):
        return 0
    ids = [r[0] for r in conn.execute(
        "SELECT cve_id FROM cves WHERE (record_status='' AND record_attempts < ?) "
        "   OR (record_status='done' AND record_fetched_at < datetime('now','-1 day') AND published_at >= date('now', ?)) "
        "ORDER BY published_at DESC", (RECORD_ATTEMPTS, f"-{RECORD_REFRESH_DAYS} day"))]
    done = 0
    for i in range(0, len(ids), RECORD_BATCH):
        batch = ids[i:i + RECORD_BATCH]
        try:
            r = client.post(cfg.cve_api_url, headers=user_agent(cfg),
                            json={"cveIds": ",".join(batch), "resultsPerPage": len(batch), "noRejected": True})
        except Exception as e:
            log.warning("CVE Search API unreachable: %s", e)
            return done
        if r.status_code == 429 or r.status_code >= 500:   # back off until the next cycle (Responsible Use Guide)
            log.warning("CVE Search API answered %s; trying again next cycle", r.status_code)
            return done
        try:
            r.raise_for_status()
            records = r.json().get("records") or []
        except Exception as e:
            log.warning("CVE Search API: %s", e)
            return done
        got = set()
        for rec in records:
            cid = str((rec.get("cveMetadata") or {}).get("cveId", "")).upper()
            if cid in batch:
                try:
                    apply_record(conn, cid, parse_record(rec))
                    got.add(cid)
                    done += 1
                except Exception as e:
                    log.warning("CVE record %s not applied: %s", cid, e)
        for cid in batch:  # not (yet) in the index: ask again next cycle, a few times
            if cid not in got:
                conn.execute("UPDATE cves SET record_attempts=record_attempts+1, record_status=CASE WHEN "
                             "record_status='' AND record_attempts+1 >= ? THEN 'missing' ELSE record_status END "
                             "WHERE cve_id=?", (RECORD_ATTEMPTS, cid))
        conn.commit()
    return done


CARD_TEXT = (  # (date, text) of every 보안 사고 / 인텔·권고 card: where a mentioned CVE id is looked for
    "SELECT COALESCE((SELECT MIN(NULLIF(a.published_at,'')) FROM incident_articles ia JOIN articles a "
    "ON a.id=ia.article_id WHERE ia.incident_id=incidents.id), occurred_at) AS d, "
    "hashtags || ' ' || title || ' ' || summary AS t FROM incidents "
    "UNION ALL SELECT i.published_at, i.hashtags || ' ' || i.title || ' ' || i.summary || ' ' || "
    "COALESCE(i.action,'') || ' ' || a.title FROM intel i JOIN articles a ON a.id=i.article_id")


def from_cards(conn) -> int:
    """Add CVE ids mentioned on cards but not yet in the list (origin 'card'), dated by the earliest such card.
    Their CVE record (fetch_records) supplies everything else. Returns how many were added."""
    have = {r[0] for r in conn.execute("SELECT cve_id FROM cves")}
    found: dict[str, str] = {}
    for row in conn.execute(CARD_TEXT):
        for cid in CVE_RE.findall(row["t"] or ""):
            cid = cid.upper()
            if cid not in have and (cid not in found or (row["d"] or "9") < found[cid]):
                found[cid] = row["d"] or ""
    for cid, d in found.items():
        conn.execute("INSERT OR IGNORE INTO cves(cve_id, published_at, origin) VALUES(?,?, 'card')",
                     (cid, d or datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    conn.commit()
    return len(found)


def collect(conn, cfg, client: httpx.Client) -> dict:
    """Read every enabled [[cve_sources]] feed, then CVE records (CVE Search API), then the cvefeed.io detail pages
    still needed. Returns counts."""
    stats = {"cves": 0, "cve_mentions": 0, "cve_records": 0, "cve_details": 0}
    for src in getattr(cfg, "cve_sources", []):
        if not src.enabled:
            continue
        try:
            r = client.get(src.url, headers=feeds.UA, follow_redirects=True)
            r.raise_for_status()
            items = parse_feed(src.url, r.content)
        except Exception as e:
            log.warning("cve feed %s failed: %s", src.url, e)
            continue
        for it in items:
            stats["cves"] += upsert(conn, it, src.name)
        conn.commit()
    stats["cve_mentions"] = from_cards(conn)
    stats["cve_records"] = fetch_records(conn, cfg, client)
    # cvefeed.io pages: only where the record (if any yet) left products or remediation empty; a CVE whose record
    # is still being looked for waits, unless the API is off
    api = getattr(cfg, "cve_api_enabled", False)
    pending = conn.execute("SELECT cve_id, detail_url FROM cves WHERE detail_url != '' AND detail_attempts < ? "
                           "AND (products='[]' OR remediation='') AND (? = 0 OR record_status != '') "
                           "ORDER BY published_at DESC LIMIT ?", (DETAIL_ATTEMPTS, int(api), DETAIL_PER_CYCLE)).fetchall()
    for n, row in enumerate(pending):
        if n:
            time.sleep(DETAIL_DELAY)
        try:
            r = client.get(row["detail_url"], headers=feeds.UA, follow_redirects=True)
            r.raise_for_status()
            apply_detail(conn, row["cve_id"], parse_cvefeed_detail(r.content))
            stats["cve_details"] += 1
        except Exception as e:
            log.info("cve detail %s: %s", row["detail_url"], e)
            conn.execute("UPDATE cves SET detail_attempts=detail_attempts+1, "
                         "detail_url=CASE WHEN detail_attempts+1 >= ? THEN '' ELSE detail_url END WHERE cve_id=?",
                         (DETAIL_ATTEMPTS, row["cve_id"]))
            title = conn.execute("SELECT title, affected FROM cves WHERE cve_id=?", (row["cve_id"],)).fetchone()
            if not title["affected"]:  # until (or unless) the page is read: a guess from the title
                conn.execute("UPDATE cves SET affected=? WHERE cve_id=?", (affected_from_title(title["title"]), row["cve_id"]))
        conn.commit()
    if stats["cves"] or stats["cve_mentions"] or stats["cve_records"]:
        log.info("CVE: %d new from feeds, %d from cards, %d records, %d detail pages read", stats["cves"],
                 stats["cve_mentions"], stats["cve_records"], stats["cve_details"])
    return stats


# ---------------------------------------------------------------- reading

def _web(u) -> bool:
    return isinstance(u, str) and u.lower().startswith(("http://", "https://"))


def decorate(r: dict) -> dict:
    """JSON columns -> lists; only http(s) links survive (they come from feeds and end up in href)."""
    r["products"] = [p for p in _loads(r.get("products"), []) if isinstance(p, dict)]
    r["refs"] = [u for u in _loads(r.get("refs"), []) if _web(u)]
    r["sources"] = [x for x in _loads(r.get("sources"), []) if isinstance(x, dict) and _web(x.get("url"))]
    r["severity_ko"] = SEVERITY_KO.get(r.get("severity") or "", "")
    r["exploitation_ko"] = EXPLOITATION_KO.get(r.get("exploitation") or "", "")
    return r


def mentions(text: str, cid: str) -> bool:
    """`cid` appears in `text` as a whole id (CVE-2026-7614 doesn't match CVE-2026-76146)."""
    return bool(re.search(re.escape(cid) + r"(?!\d)", text or "", re.I))


def related(conn, cid: str) -> list[dict]:
    """보안 사고 / 인텔·권고 글타래 tagged with the CVE, or naming it in their title or summary; newest first."""
    like = f"%{cid}%"
    out = []
    for r in conn.execute("SELECT id, title, summary, hashtags, occurred_at AS d FROM incidents "
                          "WHERE hashtags LIKE ? OR title LIKE ? OR summary LIKE ?", (like, like, like)):
        if mentions(r["hashtags"] + " " + r["title"] + " " + r["summary"], cid):
            out.append({"section": "incident", "id": r["id"], "title": r["title"], "date": r["d"],
                        "url": f"/incidents/{r['id']}", "tagged": mentions(r["hashtags"], cid)})
    for r in conn.execute("SELECT i.id, i.title, i.summary, i.hashtags, i.published_at AS d, a.url, a.title AS at "
                          "FROM intel i JOIN articles a ON a.id=i.article_id "
                          "WHERE i.hashtags LIKE ? OR i.title LIKE ? OR i.summary LIKE ? OR a.title LIKE ?",
                          (like, like, like, like)):
        if mentions(" ".join((r["hashtags"], r["title"], r["summary"], r["at"])), cid):
            out.append({"section": "intel", "id": r["id"], "title": r["title"], "date": r["d"], "url": r["url"],
                        "tagged": mentions(r["hashtags"], cid)})
    return sorted(out, key=lambda x: (x["date"] or "", x["id"]), reverse=True)
