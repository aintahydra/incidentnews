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

- `articles(id, source, url UNIQUE, title, published_at, body, status, error, fetched_at)`
  status: `new` → `done` | `skipped` (not an incident) | `failed` (retried, max attempts)
- `incidents(id, title, occurred_at, attacker, victim, damage, summary, category, created_at, updated_at)`
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

- Word cloud (`wordcloud.py`, `GET /api/wordcloud?period=`): incidents with `occurred_at` in the last 1/7/30/365 days
  (today included). Terms = title words (bracket tags, numbers, trailing particles, predicates and a stopword list removed)
  plus victim/attacker as whole names; each counted once per incident; top 60. Laid out client-side (spiral placement,
  log-scaled font size, no external library); words link to `/?q=`.
- `[wordcloud].trivial` (config, re-read per request): each keyword is removed as a substring from every title word
  after particle stripping (longest first, case-insensitive), so compounds keep their informative part
  (금융권해킹 -> 금융권) and words left empty are dropped. Victim/attacker names are never cut, only dropped when they
  consist solely of trivial keywords.

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
