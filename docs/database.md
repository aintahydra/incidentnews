# Database schema (`data/news.db`)

The collector and the web app share one SQLite file. Its path comes from `[collector].database` in
`config.toml` (default `data/news.db`). The schema is defined in `newscollect/db.py`. `db.connect()` creates
any missing tables and adds columns that are missing from older databases (see [Migrations](#migrations)).
Every connection turns on `PRAGMA foreign_keys=ON`.

## Overview

```
                     ┌──────────────────────┐
                     │       articles       │  every collected feed item (both sections)
                     │  section = incident  │
                     │        | intel       │
                     └──────────┬───────────┘
          section='incident'    │    section='intel'
            ┌───────────────────┴───────────────────┐
            │ N                                      │ 1
┌───────────▼───────────┐                 ┌──────────▼──────────┐
│   incident_articles   │                 │        intel        │  one card per article
│ (incident_id,         │                 │ article_id UNIQUE   │
│  article_id)          │                 └─────────────────────┘
└───────────┬───────────┘
            │ N
┌───────────▼───────────┐
│       incidents       │  one card per real-world incident (several articles can merge into one)
└───────────────────────┘
```

- **articles** is the raw input. Every RSS item from `[[sources]]` and `[[intel_sources]]` lands here.
- **incidents** and **intel** are what the web page shows. The LLM produces them in Korean.
- An incident can cite several articles, for example the same breach reported by 보안뉴스 and Bleeping
  Computer. That is a many-to-many link through **incident_articles**. An intel card always comes from
  exactly one article.

## Tables

### `articles`

Each row is one feed item, keyed by URL.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `source` | TEXT NOT NULL | The source `name` from `config.toml`, such as `보안뉴스` or `Cisco Talos`. |
| `url` | TEXT NOT NULL **UNIQUE** | Deduplication key. The collector inserts with `INSERT OR IGNORE`, so a re-fetched item is skipped. |
| `title` | TEXT NOT NULL | The original title, in the source language. |
| `published_at` | TEXT | Publication date as `YYYY-MM-DD`, taken from the feed or the page. It can be empty. |
| `body` | TEXT | Article text. It starts as the feed snippet and is replaced by the fetched page body when processed. It stays snippet-only for sites that block fetches, such as Cloudflare 403s. |
| `status` | TEXT NOT NULL | `new` → `done` / `skipped` / `failed` (see below). |
| `attempts` | INTEGER NOT NULL | How many times processing was tried. Rows with `attempts >= 3` (`MAX_ATTEMPTS` in `collector.py`) are not retried. |
| `error` | TEXT | Last error message (up to 300 characters) when `status='failed'`. |
| `fetched_at` | TEXT NOT NULL | When the collector first saw the item, as a UTC `CURRENT_TIMESTAMP`. |
| `section` | TEXT NOT NULL | `incident` or `intel`, depending on which source list the item came from. The default is `incident`. |

**Status lifecycle**

```
new ──► done      incident section: an incident was created or merged · intel section: an intel card was saved
    ├─► skipped   not an incident (talks, interviews, roundups, product news) / intel item judged irrelevant
    └─► failed    an exception occurred; retried on later cycles until attempts reaches 3
```

If the LLM server is unreachable, the cycle stops without changing the status or `attempts` of any article, so
nothing loses a retry. The collector processes `new` and `failed` rows with incident articles first, then
intel articles, and Korean articles before foreign-language ones within each group.

### `incidents`

Each row is one incident card in the 보안사고 tab.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `title` | TEXT NOT NULL | Korean title. |
| `occurred_at` | TEXT NOT NULL | When the incident happened (`YYYY-MM-DD`), as estimated by the LLM. It is used for deduplication. |
| `attacker` | TEXT | Attacker or group; empty if unknown. |
| `victim` | TEXT | Victim organization; empty if unknown. |
| `damage` | TEXT | Damage description. |
| `category` | TEXT | One of `랜섬웨어`, `데이터 유출`, `해킹`, `악성코드`, `DDoS`, `피싱`, `기타`. |
| `summary` | TEXT | Korean summary in plain 다체 (no 존댓말). |
| `hashtags` | TEXT NOT NULL | A JSON array of up to 5 tags (without `#`), one per role in the order attacker, victim, vulnerable system, technique, incident type. Roles the article doesn't support are left out. Default `'[]'`. |
| `created_at`, `updated_at` | TEXT NOT NULL | UTC timestamps. `updated_at` changes when a later article fills in empty fields. |

**Merging.** `dedupe.upsert_incident()` looks for an existing incident within ±7 days of `occurred_at`. It
matches on the same victim (and a compatible attacker), on title similarity ≥ 0.5, or, for Korean and English
victim names, on an LLM judgement. On a match the article is linked to the existing card. Empty fields are
filled in, and a longer summary replaces a shorter one. Otherwise a new card is inserted.

**Displayed date.** The page groups and charts incidents by the publication date of the earliest linked
article (`POSTED` in `web.py`). It falls back to `fetched_at` and then to `occurred_at`. When `occurred_at`
differs from that date, the card also shows a "발생" date.

### `incident_articles`

Links incidents to their source articles (many-to-many).

| Column | Type | Notes |
|---|---|---|
| `incident_id` | INTEGER NOT NULL | → `incidents.id`, `ON DELETE CASCADE` |
| `article_id` | INTEGER NOT NULL | → `articles.id`, `ON DELETE CASCADE` |
| | | PRIMARY KEY (`incident_id`, `article_id`) |

Deleting an incident or an article removes its links. Deleting an incident does **not** delete its articles.

### `intel`

Each row is one card in the 인텔리전스·권고 tab.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `article_id` | INTEGER NOT NULL **UNIQUE** | → `articles.id`, `ON DELETE CASCADE`. One card per article. `intel.save()` upserts on it, so a re-run overwrites the card. |
| `title` | TEXT NOT NULL | Korean title. |
| `kind` | TEXT NOT NULL | `권고` (advisory), `위협 분석` (threat analysis), `가이드` (guide) or `정책` (policy). This drives the sub-tabs. |
| `summary` | TEXT NOT NULL | Korean summary, 다체. |
| `action` | TEXT | One-line recommended action, mostly for advisories; may be empty. |
| `hashtags` | TEXT NOT NULL | A JSON array of up to 4 tags by role: actor, product/CVE, technique, topic. Default `'[]'`. |
| `published_at` | TEXT NOT NULL | The article's publication date. Cards are sorted and charted by it. |
| `created_at`, `updated_at` | TEXT NOT NULL | UTC timestamps. |

## Indexes

| Index | On | Used for |
|---|---|---|
| `sqlite_autoindex_articles_1` | `articles(url)` | the UNIQUE constraint and URL deduplication |
| `sqlite_autoindex_incident_articles_1` | `incident_articles(incident_id, article_id)` | the primary key and duplicate-link prevention |
| `idx_incidents_date` | `incidents(occurred_at DESC)` | the ±7-day merge window lookup |
| `sqlite_autoindex_intel_1` | `intel(article_id)` | the UNIQUE constraint and upserts |
| `idx_intel_date` | `intel(published_at DESC)` | listing intel cards newest first |

## Migrations

`CREATE TABLE IF NOT EXISTS` does not change tables that already exist, so `_migrate()` in `db.py` adds
columns that older databases lack:

- `incidents.hashtags`, for databases created before tags existed.
- `articles.section`. Existing rows become `incident`.

`ALTER TABLE ... ADD COLUMN` appends the new column at the end, which is why `section` is the last column of
`articles`. SQLite also keeps the original `CREATE TABLE` text, comments included. In databases created before
role-based tags, `.schema` shows the old comment "up to 3 tags" on `incidents.hashtags`. The authoritative
definition is the one in `db.py`.

## Useful queries

```sql
-- processing status per section
SELECT section, status, COUNT(*) FROM articles GROUP BY 1, 2;

-- incidents with their source articles
SELECT i.id, i.title, a.source, a.published_at, a.url
FROM incidents i
JOIN incident_articles ia ON ia.incident_id = i.id
JOIN articles a ON a.id = ia.article_id
ORDER BY i.id DESC;

-- intel cards per kind
SELECT kind, COUNT(*) FROM intel GROUP BY kind;

-- articles that failed and will be retried
SELECT id, source, title, attempts, error FROM articles WHERE status = 'failed' AND attempts < 3;

-- tag frequency (incidents)
SELECT t.value AS tag, COUNT(*) FROM incidents, json_each(incidents.hashtags) t GROUP BY tag ORDER BY 2 DESC;
```

To reprocess an article, set it back to `new` and reset its retry counter (and delete its intel card or
incident link first if needed):

```sql
UPDATE articles SET status = 'new', attempts = 0, error = NULL WHERE id = ?;
```
