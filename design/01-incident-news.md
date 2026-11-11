# Incident news: getting and storing

Goal: one Korean card per real-world security incident (who attacked whom, when, how much damage), even when
several sources report it, built from RSS feeds listed in `config.toml`.

## Sources and configuration

```toml
[collector]
interval_minutes = 30        # one cycle every 30 minutes
max_articles_per_run = 30    # per feed, newest first
max_age_days = 30            # feed items older than this are ignored
database = "data/news.db"

[[sources]]                  # incident news feeds (Korean and English)
name = "보안뉴스"
url = "https://..."
enabled = true
```

`config.toml` is re-read at the start of every cycle, so feeds can be added, removed or disabled without a restart.
Current incident feeds: 보안뉴스, 데일리시큐 (all articles), Bleeping Computer, The Hacker News, Security Week.
Dark Reading and DataBreaches.net are listed but disabled: their sites answer automated requests with a Cloudflare
403, and the project does not try to get around bot checks.

## Pipeline (one cycle, `collector.collect_once`)

```
feeds.fetch_feed ──► INSERT OR IGNORE INTO articles (status=new, section=incident)
                                │  url UNIQUE: an item already seen is never reprocessed
                                ▼
pending = articles with status new|failed and attempts < 3
          sorted: incident before intel, Korean before foreign
                                │
for each article:               ▼
  feeds.fetch_body ── page text (≤ 6,000 chars), else the RSS snippet
  extractor.extract ─► Extraction(is_incident, title, occurred_at, attacker, victim, damage,
                                  category, summary, hashtags, method)
  if incident: dedupe.upsert_incident ─► incidents (+ incident_articles link) ; status=done
  else: status=skipped
  on error: status=failed, attempts+1 (retried next cycle, at most 3 attempts)
  LLM unreachable: stop the cycle without touching status or attempts
```

### 1. Fetching feeds (`feeds.fetch_feed`)

- httpx with a project User-Agent (`newscollect/1.0`), feedparser for RSS/Atom.
- Publication date from the entry (`published`/`updated`); a loose parser handles formats such as "Nov 05, 2026"
  and "5 November 2026". Missing date: today.
- Snippet: the longer of `content:encoded` and `description`, HTML stripped, WordPress "The post ... appeared first
  on ..." footers removed.

### 2. Article text (`feeds.fetch_body`, `extract_article_text`)

- Known body selectors first (`#news_body_area`, `#article-view-content-div`, `.articleBody`, `[itemprop=articleBody]`,
  ...), else the element holding the most `<p>` text.
- Navigation, scripts, figures, headers/footers and "Related articles"/"Read more" blocks are dropped; text is
  joined per block.
- Fewer than 200 characters, an HTTP error or a block page: the RSS snippet is used instead.

### 3. Incident extraction (`extractor.extract`)

1. **Prefilter** (`looks_like_incident`), no LLM cost:
   - titles of talks, interviews, columns, contributed articles and roundups (`[AIS 2026]`, `[인터뷰]`, `[기고]`,
     "Week in Review", "In Other News") are never incidents;
   - the text must contain incident vocabulary (Korean `INCIDENT_WORDS`, English `EN_INCIDENT`);
   - vulnerability or patch advisories are not incidents unless the title reports actual damage or exploitation.
2. **LLM** (`extract_llm`, JSON mode, temperature 0, then 0.3 and 0.6 on retries). Fields: `is_incident`, `title`,
   `occurred_at`, `attacker`, `victim`, `damage`, `category` (랜섬웨어 / 데이터 유출 / 해킹 / 악성코드 / DDoS /
   피싱 / 기타), `summary`, `tags`. All values in Korean, except names (companies, groups, products), which keep the
   article's spelling. Vulnerability-only advisories, policy, products, opinion, talks and roundups are
   `is_incident=false`.
3. **Checks** before a result is accepted (a failed check retries with a hint naming the problem):
   - summary is Korean, 2-4 plain sentences in 다체 (`~했다`, `~이다`; 존댓말 is converted by `plain_form` or
     rejected), no transliterated English names;
   - attacker and victim must appear in the article text (otherwise blanked for English sources);
   - `occurred_at` within 180 days before the publication date, and years in the summary must occur in the article.
4. **Model choice**: `[llm].model` for Korean articles, `[llm].foreign_model` for others (`llm.model_for`).
5. **Fallback**: if the LLM fails for a Korean article, a keyword heuristic stores the incident with the first
   sentences as summary (`method="heuristic"`). English articles have no fallback (it would store English); they are
   retried instead.

### 4. Tags

The same LLM call returns up to five tags, one per role, in this order: attacker, victim, vulnerable system,
technique, incident type (e.g. `#LockBit #A사 #VPN #크리덴셜스터핑 #랜섬웨어`). Roles the article doesn't support are
left out. `extractor.clean_hashtags` drops placeholders (미상, unknown), keeps one name per role, removes `#` and
spaces, and limits each tag to 2-25 characters. See [03-tag-graph.md](03-tag-graph.md).

### 5. Merging duplicates (`dedupe.upsert_incident`)

Candidates are incidents with `occurred_at` within ±7 days. An article joins an existing incident when:

1. the victim is the same (names normalised: case, corporate suffixes, spaces; equal or one contains the other)
   and the attackers don't conflict; or
2. the titles are similar (character-bigram Jaccard ≥ 0.5); or
3. the victims are written in different scripts (바이낸스 / Binance) within 7 days and an LLM judge
   (`dedupe.llm_judge`) says they are the same incident. Any doubt keeps them separate.

On a merge the article is linked through `incident_articles`; empty fields are filled, a longer summary replaces a
shorter one, and the first non-empty tag set is kept. Otherwise a new incident is inserted.

## Storage

| Table | Role |
|---|---|
| `articles` | Every feed item: URL (unique), title, publication date, body, status, attempts, section |
| `incidents` | One row per incident card: Korean title, `occurred_at`, attacker, victim, damage, category, summary, tags (JSON) |
| `incident_articles` | Which articles each incident came from (many-to-many) |

Full column list: [../docs/database.md](../docs/database.md).

## Dates

- `occurred_at`: when the incident happened, as extracted by the LLM. Used for merging.
- Display date (`web.POSTED`): the earliest publication date among the incident's articles. Cards are grouped by
  this date, so a newly reported old incident still appears at the top; the card shows "발생" when `occurred_at`
  differs.

## Maintenance commands

- `POST /incidents/{id}/regenerate` (the card's "요약 다시 생성" button): re-reads up to 3 source pages and asks the LLM
  again at temperature 0.4; one at a time, 20 s cooldown per incident; keeps `occurred_at`.
- `python -m newscollect retag [id ...]`: re-tags stored incidents with a tags-only prompt.
