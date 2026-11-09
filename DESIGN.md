# newscollect — 보안 사고 타임라인 서비스

Collects cybersecurity news from RSS feeds, extracts incidents with an LLM
(rule-based fallback), de-duplicates them, and renders a hacklog-style page:
one entry per incident with date, who attacked whom, damage and a summary.

## 1. Requirements

- Sources are a list in `config.toml`; add / delete / modify by editing the file
  (re-read every collection cycle, no restart needed).
- LLM endpoint + model are in `config.toml` (`127.0.0.1:11434` / `solar:10.7b`
  by default; `OLLAMA_HOST` env overrides the endpoint, e.g. `10.0.2.2:11434`).
- UI and summaries are Korean.
- One entry per real-world incident, even when several articles/sources cover it.
- Each entry: occurrence date, attacker, victim, damage scale, summary, source links.

## 2. Architecture

```
config.toml ─┐
             ▼
 [Collector loop]  every N minutes (background asyncio task in the web process)
   1. fetch RSS (httpx + feedparser)           -> articles (status=new)
   2. fetch article body (bs4), fall back to RSS description
   3. Extractor: LLM JSON  -> fallback heuristic   (status=done|skipped)
   4. Deduper: match to an existing incident or create a new one
             ▼
        SQLite (data/news.db)
             ▼
 [FastAPI + Jinja2]   /  /incidents/{id}  /api/incidents  /healthz
```

Single process, single SQLite file: simple to run (`python -m newscollect`).

## 3. Data model

- `articles(id, source, url UNIQUE, title, published_at, body, status, error, fetched_at, section)` — section is `incident` or `intel`
- `intel(id, article_id UNIQUE, title, kind, summary, action, hashtags, published_at, created_at, updated_at)`
  status: `new` → `done` | `skipped` (not an incident) | `failed` (retried, max attempts)
- `incidents(id, title, occurred_at, attacker, victim, damage, summary, category, hashtags, created_at, updated_at)`
- `incident_articles(incident_id, article_id)` — many articles per incident

## 4. Extraction

Prompt asks the LLM (Ollama `/api/chat`, `format: json`, temperature 0) for:
`is_incident, title, occurred_at (YYYY-MM-DD or null), attacker, victim, damage,
category, summary`. Summary must state when / who attacked whom / how large the
damage was, in Korean, 2–4 sentences. `occurred_at` is resolved against the
article's publish date. Vulnerability-only advisories, policy, product and
opinion pieces are `is_incident=false`.

Fallback (LLM down / bad JSON): keyword heuristic decides incident-ness
(해킹, 랜섬웨어, 유출, 침해, 공격 …), occurrence date = publish date, summary =
first sentences of the body, attacker/victim left empty.

## 5. De-duplication

A new extraction is compared with incidents whose `occurred_at` is within ±7 days:

1. Normalize victim / attacker (lowercase, strip corp suffixes and spaces).
2. Same victim (equal or containment) → duplicate.
3. Otherwise title similarity (character-bigram Jaccard ≥ 0.5) → duplicate.
4. Same URL is never reprocessed (`articles.url UNIQUE`).

On a match: link the article, fill empty fields, keep the longer summary.
(An LLM tie-break for borderline scores is a possible later extension.)

## 6. Configuration (`config.toml`)

```toml
[server]    host, port
[llm]       endpoint, model, timeout_seconds, enabled
[collector] interval_minutes, max_articles_per_run, max_age_days
[[sources]] name, url, enabled
```

## 7. Phased plan

| Phase | Scope | Verification |
|---|---|---|
| 1 | Project skeleton, `config.toml` loader, SQLite schema | unit tests: config parse, add/remove source |
| 2 | RSS fetch + article body fetch + storage | test with fixture feed; live fetch of both URLs |
| 3 | LLM extractor + heuristic fallback | tests with mocked Ollama; live call to solar |
| 4 | Deduper | tests: same incident from two sources merges, different ones don't |
| 5 | Web UI (list grouped by date, detail, JSON API) | TestClient tests; screenshot/curl |
| 6 | Scheduler wiring, README, end-to-end run | full live run, check no duplicates |

## 8. Additions (themes, search, regenerate)

- Theme: `data-theme` on `<html>` (set by an inline script before first paint from
  `localStorage`, else `prefers-color-scheme`); CSS variables per theme; two header buttons.
- Search: `LIKE` (wildcards escaped) over incident fields and linked article titles, terms ANDed.
- Regenerate: `POST /incidents/{id}/regenerate` re-fetches article bodies (≤3), calls the LLM
  at temperature 0.4 (0 would repeat the same answer), never falls back to the heuristic, keeps
  `occurred_at` (dedupe depends on it). One at a time; 20 s per-incident cooldown.
- LLM validation (`extractor.extract_llm`): Korean summary + victim/attacker names must occur in the
  article text; up to 3 attempts at temperatures (t, 0.3, 0.6) with a hint naming the bad tokens;
  transient Ollama 5xx counts as a failed attempt. `num_ctx` is sent explicitly.

- Tag cloud (`tagcloud.py`, `GET /api/tagcloud?period=`): incidents *published* in the last 1/7/30/365 days (today
  included; see Timeline dates); each incident's tags counted once, case variants merged under the most common spelling;
  top 60. Laid out client-side (spiral placement, log-scaled font size, no external library); tags link to `/?q=`.
- `[tagcloud].exclude` (config, re-read per request; the old `[wordcloud].trivial` is read when `[tagcloud]` is absent):
  whole tags left out of the cloud, case-insensitive. Cards and search are unaffected.

- Collected-article chart (`stats.py`, `GET /api/article-stats?unit=`): `articles` counted by
  published_at (feed date, YYYY-MM-DD; falls back to the local date of fetched_at, which is UTC), zero-filled: last max_age_days days / last 12 months / every
  year since the first. Single-series SVG column chart drawn client-side (blue series color, hairline grid, peak
  value labelled, per-column hover/focus tooltip, table view).

- Sticky header (`position: sticky`); its height is published as `--header-h` so the pane sticks right below it.
- Side pane (list page, also on search results): `<aside id="side">` left of `<main>` holding the cloud and the chart,
  sticky, toggled by the header's icon button (inline SVG, `aria-expanded`). Grid columns pane | list (≤760px) | spacer
  keep the list centred while there is room, with the pane's cards right-aligned against the list. State is `data-pane` on `<html>`, set before first paint from
  `localStorage` (default open; always closed below 900px, where the pane is a fixed overlay drawer). Both widgets skip
  layout while their box is zero-sized and re-lay out when the pane opens.

- Org tags (`orgs.py`): victim -> "정부기관" if on `[orgs].government_list` (KOSIS, column 2), "공공기관" if on
  `public_list` (column 1); both possible. Names normalized (drop `(...)`, 재단법인/사단법인/주식회사, spaces, punctuation);
  match = equal, or a listed name of >= 3 chars contained in the victim; local governments also get short forms
  (서울시, 종로구청). Computed at read time (cached per victim, lists re-read when the file mtime changes), so tags follow
  list updates without a migration. Tabs `?tab=all|gov` filter the search result in Python before paging.

- LLM backends (`llm.py`): `[llm].provider = "local"` (Ollama `/api/chat`) or `"claude"` (Anthropic SDK,
  `client.beta.messages.create` with `output_config.effort`, adaptive thinking, server-side fallback `fallbacks: "default"`
  for cyber refusals; no `temperature` - the model rejects non-default sampling). One interface,
  `chat(model, system, user, json=, temperature=, max_tokens=)`; errors normalized to `LLMError` (retry/give up on the
  article: bad status, refusal, empty answer) and `LLMUnavailable` (transport, auth, rate limit, missing key: the collector
  stops for the cycle without burning attempts). `.env` loaded at startup with python-dotenv.

- Tags: the extraction JSON has `tags`, an object with five slots: `attacker`, `victim`, `system` (the vulnerable or
  breached system), `technique`, `type` (abstract incident type), one short tag each, "" when the article doesn't say.
  `extractor.clean_hashtags` turns it into a list in that order, skipping empty / placeholder slots ("미상", "unknown"),
  keeping one name per slot ("A, B" -> "A"), stripping `#` and spaces (2-25 chars), deduping; never fails the
  extraction; also accepts the older plain list. Stored as a JSON array in `incidents.hashtags` (max 5); merge keeps the
  first non-empty set; regenerate replaces it; searchable; rendered as links to `/?q=<tag>`.
  `python -m newscollect retag [id ...]` (`retag.py`) re-tags stored incidents with a tags-only prompt
  (`extractor.extract_tags`, `TAGS_SYSTEM`), from the stored article text plus the incident's fields.
- Timeline dates: the list groups by `posted_at` = earliest linked article's `published_at` (collection date if
  missing), computed in SQL (`web.POSTED`), so new stories land on top even when the incident is older. `occurred_at`
  (LLM-extracted incident date) is shown on the card as "발생" when it differs, and is still what dedupe compares.

## 9. Non-Korean (English) sources

- Language is auto-detected per article (`is_korean`: Hangul share of title + body start).
- Body: selector list incl. `.articleBody`, `#articlebody`, `.post-body`, ...; falls back to the element
  holding the most `<p>` text. Text is joined per block (inline tags don't add spaces); "Related Articles"
  and similar blocks are cut. `content:encoded` is used when longer than `description`.
- Incident gate: English keyword/vulnerability-title regexes mirror the Korean ones.
- LLM: all values in Korean, but victim/attacker keep the article's spelling (so the grounding check works);
  title and damage must contain Hangul. `[llm].foreign_model` selects the model for non-Korean articles.
- No heuristic fallback for non-Korean text (it would store English): failures are retried (max 3), and an
  unreachable LLM (transport error) neither burns attempts nor blocks the run beyond the first failure.
- Pending articles are processed Korean first so the LLM server swaps models once per cycle.
- Cross-language de-duplication: only when victim names differ in script (ko vs en) and dates are within
  7 days, an LLM judge (`dedupe.llm_judge`, run on the main `model`: exaone answered "different" for a true duplicate)
  decides; any doubt or error keeps the incidents separate.
- Name handling for non-Korean text: proper nouns are listed in the prompts (small models transliterate
  Claude -> 클로이디), `클로이디(Claude)` is reduced to `Claude`, headline names missing from the summary trigger one
  `polish_names` edit pass (kept only if the edit is clean). victim/attacker not found in the article are blanked
  rather than failing the article (a Korean word cannot be matched against English source text); for Korean
  articles a bad name that also appears in the title/summary still rejects the result.
- Date sanity: `occurred_at` must lie within 180 days before the publish date; years written in a summary must occur
  in the article or equal the publish year (`known_years`).

## Intel & advisories

- `[[intel_sources]]` in config.toml (same shape as `[[sources]]`); articles stored with `section='intel'` in the shared
  `articles` table (URL uniqueness and statuses as before). The collector reads both lists each cycle and processes
  incident articles first, then intel.
- `intel.py`: one JSON call per article (`intel.SYSTEM`): `relevant`, `kind` (권고 / 위협 분석 / 가이드 / 정책; unknown
  values fall back to 위협 분석), Korean `title`, 3-4 sentence `summary` (다체), one-line `action`, and `tags` with four
  slots (actor, product/CVE, technique, topic) cleaned by `extractor.clean_hashtags(tags, intel.TAG_ROLES)`.
  Not relevant -> article `skipped`. Checks reuse the incident ones (`summary_problems`, `plain_form`, transliteration
  stripping, polite-form rejection also for `action`); retried at hotter temperatures with the problems as a hint;
  failure -> article `failed`, retried next cycle. No heuristic fallback (intel needs the LLM).
- No merging: one `intel` row per article (`article_id UNIQUE`, upsert on conflict).
- Web: `/?section=intel` with kind tabs (`tab=advisory|threat|guide|policy`), grouped by the article's publication
  date; `_intel_card.html` links to the original. `/api/intel`, and `section=` on `/api/tagcloud` and
  `/api/article-stats`. `/api/version` includes intel so live updates cover both tabs. `body[data-section]` tells
  app.js which section the side pane serves.
