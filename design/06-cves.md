# 주요 CVE

A list of notable vulnerabilities from CVE feeds, with a detail view in the side pane that links each CVE to the
보안 사고 and 인텔·권고 글타래 that carry it. Code: `newscollect/cves.py`, the `section=cve` branch of `web.py`,
`templates/cve.html` and `_cve_detail.html`, the 주요 CVE block of `static/app.js`.

No LLM is involved: CVEs are stored the way the sources give them (cvefeed.io in English, KNVD in Korean).

## Sources (`config.toml`)

```toml
[[cve_sources]]
name = "cvefeed.io"
url = "https://cvefeed.io/rssfeed/severity/high.xml"

[[cve_sources]]
name = "KNVD 보안취약점 정보포털"
url = "https://knvd.krcert.or.kr/rss/security/info"
```

Read on every collection cycle (`collector.collect_once` calls `cves.collect` right after the news feeds, before
the LLM step), so adding or disabling a feed applies without a restart. The parser is chosen by the feed's host.

Order within a cycle: (1) the feeds, (2) CVEs mentioned on 보안 사고 / 인텔·권고 cards, (3) CVE records from the
CVE Search API for new CVEs, (4) cvefeed.io detail pages, only for CVEs whose record left products or remediation
empty.

### CVEs mentioned on cards (`from_cards`)

Every cycle, all cards are scanned for CVE ids that aren't in the list yet: an incident's tags, title and summary;
an intel card's tags, title, summary, recommended action and original article title. Full article bodies are not
scanned (round-ups such as Patch Tuesday articles list dozens of CVEs the card isn't about). Each new id becomes a
row with `origin = 'card'`, dated by the earliest card that mentions it (an incident's earliest article date, an
intel card's publication date), and nothing else: its CVE record fills in title, description, products, CVSS,
CWE, remediation, exploitation and KEV. Cards made during a cycle (by the collector, `addnews` or the KakaoTalk
importer) are picked up on the next cycle.

- In the list such CVEs carry a "뉴스" mark; the pane says the CVE was found on 글타래, not in a CVE feed.
- If a CVE feed later carries the id, `origin` becomes `feed`.
- An id cve.org never returns (5 attempts, `record_status = 'missing'`), e.g. a mistyped tag, is left out of the
  list and the counts. Until then it is listed with whatever is known.

| Source | What an item gives | Parsed into |
|---|---|---|
| cvefeed.io RSS (`parse_cvefeed`) | title `CVE-… - <title>`; HTML description with `Description :` and `Severity: 9.2 \| CRITICAL`; pubDate; link to `/vuln/detail/CVE-…` | id, title, English description, CVSS, severity, date; the detail page is queued |
| cvefeed.io detail page (`parse_cvefeed_detail`) | "Affected Products" table (vendor, product), "Solution" card, "References to Advisories…" table, CVSS version | products, remediation, references |
| KNVD RSS (`parse_knvd`) | title `CVE-… \| <Korean title>`; text in `□ 개요`, `□ 설명`, `□ 영향받는 제품 및 해결 방안`, `□ 참고사이트` sections (plain or Markdown `#####`); a table `CVE \| CVSS \| 심각도 \| CWE \| 영향` (one item may list several CVEs) | Korean description, per-CVE CVSS/severity/CWE, products with affected and fixed versions, remediation ("다음 버전 이상으로 업데이트" + the ※ notes), references, detection when a section named 탐지/점검 exists |
| any other feed (`parse_generic`) | CVE ids in item titles | id, title, item text as description |

The KNVD `□ 기여자` section (names of people who reported the bug) is not stored.

### CVE records (CVE Program Search API)

```toml
[cve_api]
enabled = true
url = "https://search.cve.org/search"
contact = ""     # appended to the User-Agent; set it if you distribute the app
```

The CVE Program's Search API (BETA since 2026-10-13; docs at https://search.cve.org/api-docs/, rules at
https://cveproject.github.io/search-api-responsible-use) returns complete CVE JSON 5 records. `fetch_records` sends
`POST /search` with `{"cveIds": "CVE-…,CVE-…", "resultsPerPage": n, "noRejected": true}`, up to 100 ids per request,
so a cycle usually costs one request.

- Which CVEs: new ones (`record_status = ''`), and records published in the last 30 days that were fetched more
  than a day ago (CISA adds SSVC and KEV later). Older records are not re-read.
- An id the API doesn't return is asked again on later cycles (the index can lag), up to 5 times, then marked
  `missing`.
- HTTP 429 or 5xx, or a network error: stop and try again next cycle (the guide asks for backing off; no retries
  inside a cycle). No API key; the guide's ceiling is 10,000 requests / 5 minutes / IP, far above this use.
- The guide asks publicly distributed apps to send app name, version and contact in the User-Agent:
  `newscollect/1.0 (+<contact>)` with `[cve_api].contact`. It is empty by default.

`parse_record` takes from the record: the CNA's English description and title; `affected` (vendor + product, and
version ranges as "A 이상 B 미만" / "B 이하" with the fixed version from `changes` or `lessThan`); the CNA's CVSS (v4.0,
else v3.1, v3.0, v2.0; else an ADP's) with vector; CWE ids (CNA and ADP); `solutions` -> remediation; `workarounds`;
references; and from CISA's ADP container the SSVC `Exploitation` (none / poc / active) and the KEV `dateAdded`.

`apply_record` merge rules: score, version, vector and severity follow the record; exploitation and KEV always follow
the latest record; title, descriptions, CWE, remediation and workarounds only fill empty fields (KNVD's Korean text
and fixed-version table stay); products replace cvefeed.io's or none, but not KNVD's table (`products_from`); the
list's short name changes only if it was empty or built from cvefeed.io's CPE ids. A "CVE.org" link to
`https://www.cve.org/CVERecord?id=…` is added to the sources. When the record supplies products and remediation,
the cvefeed.io page is not read.

### Remediation and detection

The request was to take remediation and detection from `https://cve.socradar.io/cve/CVE-…`. That site answers
automated clients with a Cloudflare JavaScript challenge (HTTP 403 "Just a moment..."), so it isn't used; the app
does not work around bot checks. Instead:

- remediation comes from KNVD's fixed-version table, else the CNA's `solutions` in the CVE record, else
  cvefeed.io's "Solution" section (allowed by its robots.txt: `Allow: /vuln/detail/`); the record's `workarounds`
  are shown as 임시 조치;
- exploitation: CISA's SSVC assessment and KEV listing from the record are shown as 악용 여부, and KEV (or active
  exploitation) as a badge in the list;
- detection is filled only when a source has a detection section. Neither current feed does, so the field is
  usually empty and the pane leaves it out.

### Detail pages

New cvefeed.io CVEs get `detail_url`; each cycle reads up to `DETAIL_PER_CYCLE` (40) of them one at a time with a
1 s pause, newest first. A failed read is retried on later cycles, up to 3 attempts. A KNVD product table and
remediation win over cvefeed.io's; references are combined.

### Affected system (the list column)

1. A product table (KNVD, else the cvefeed.io detail page): the first product, plus "외 N개" when there are more.
2. Otherwise a name read from a vendor-style cvefeed.io title: `WordPress WP Rocket plugin < 3.24.2 - RCE` ->
   `WordPress WP Rocket plugin`; `Apache JSPWiki: Session fixation…` -> `Apache JSPWiki`.
3. Only if the detail page has no products (or can't be read): a guess from the title (`… in <product>`), else
   empty ("—" in the list).

### Merging

One row per CVE id (`upsert`). A CVE seen again, or in the other feed, fills empty fields only; the earliest
publication date wins; the English (`description`) and Korean (`description_ko`) descriptions are both kept; the
feeds' pages are collected in `sources` and references in `refs`. A KNVD product table replaces products or an
affected name that came only from a cvefeed.io title.

## Storage

Table `cves` (schema in `cves.SCHEMA`, created by `db.connect`); see [../docs/database.md](../docs/database.md).

## Related 글타래 (`cves.related`)

A 보안 사고 or 인텔·권고 card is related when the CVE id appears in its tags, its title or its summary (intel: also
the original article title), as a whole id (`CVE-2026-7614` doesn't match `CVE-2026-76146`). Tagged cards and
cards that only mention it are both listed; the latter are marked "제목·요약에 언급". Incidents link to their
detail page, intel cards to the original article (as on their cards). Computed at request time, so cards tagged
later show up without any update to the CVE.

## Page

- Header: 「주요 CVE N」 is a separate pill group next to 보안 사고 · 인텔·권고 · 태그간 관계 그래프. The count follows
  the current search, like the other two.
- `/?section=cve`: table CVE (with its date underneath) · 영향받는 시스템 · 심각도, 100 rows plus "더 보기" (`?n=`).
  Sort switch above it (`?sort=`): 최신순 (`date`, default: newest date first, then score) or 심각도순 (`severity`:
  CVSS score, else the severity label CRITICAL 9 / HIGH 7 / MEDIUM 4 / LOW 0.1, unknown last; then newest). Search,
  CVE links, 더 보기 and 검색 해제 keep the sort. The search box searches CVE id, title, affected system, both descriptions, products and CWE.
- Severity is a pill with the CVSS score; its border/tint follows the severity (critical, high, medium, low) and the
  severity name is in the tooltip and in the pane, so colour is never the only cue.
- Clicking a CVE number loads `/cves/{id}` (an HTML fragment) into the side pane, highlights the row and pushes
  `?section=cve&cve=…` to the address; Back/Forward switch between CVEs. The same link without JavaScript renders the
  page with the pane already filled. On phones the pane opens as the drawer.
- Pane: CVE id and title; CVSS (score, severity, version, vector); 악용 여부 (CISA SSVC / KEV, when known);
  affected products, each with its version ranges and fixed versions; CWE; date; description (Korean and/or
  English); 조치 방법; 임시 조치; 탐지 방법 (when present); reference links (the source pages first, including
  CVE.org); 관련 글타래.
- List: a red "KEV" badge (or "악용" for CISA's "active") after the CVE number.

## API

- `GET /api/cves?q=&limit=&sort=date|severity` -> `{total, cves[{cve_id, title, affected, cvss, severity, severity_ko, published_at}]}`
- `GET /api/cves/{id}` -> every column, decoded `products`, `refs`, `sources`, plus `related`
- `GET /cves/{id}` -> the pane fragment (404 if unknown)

Only `http(s)` links from feeds are rendered (`cves.decorate`).
