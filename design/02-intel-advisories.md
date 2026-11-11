# Intelligence and advisories: getting and storing

Goal: a second stream of Korean summaries for threat intelligence, vendor research, government advisories and
security guidance. Unlike incident news, every relevant article becomes its own card: no incident test and no
merging.

## Sources

A separate list in `config.toml`, same shape as `[[sources]]`:

```toml
[[intel_sources]]
name = "Cisco Talos"
url = "https://blog.talosintelligence.com/rss/"
enabled = true
```

Current feeds: NCSC UK, Recorded Future, Kaspersky Tips, Cisco Talos, Google Security Blog, Google Threat
Intelligence, Unit 42, SANS ISC, Securelist, Trend Micro, CERT-EU, Rapid7, CrowdStrike. CISA's feeds are listed
but disabled (Cloudflare 403 on every CISA feed).

## Pipeline

The collector reads both lists in the same cycle with the same `max_age_days` and `max_articles_per_run`. Intel
items are stored in the shared `articles` table with `section='intel'`, so URL uniqueness, statuses and retries
work exactly as for incident news ([01-incident-news.md](01-incident-news.md)). Incident articles are processed
first in each cycle, then intel.

```
feeds.fetch_feed ──► articles (status=new, section=intel)
                                │
feeds.fetch_body ──► page text, else snippet
                                │
intel.extract(llm, model, article)
  ├─ title matches SKIP_TITLE (e.g. SANS "ISC Stormcast" podcast stubs) ─► None ─► status=skipped
  ├─ LLM says relevant=false (webinar promos, hiring, awards, company news, ads) ─► None ─► status=skipped
  └─ IntelEntry(title, kind, summary, action, hashtags) ─► intel.save ─► status=done
  failure after retries ─► ValueError ─► status=failed (retried next cycle, max 3 attempts)
```

Intel needs the LLM: there is no heuristic fallback, because the output must be a Korean summary.

## What the LLM produces (`intel.SYSTEM`)

One JSON object per article:

| Field | Content |
|---|---|
| `relevant` | true for threats, vulnerabilities, advisories, threat analysis, security practice and policy |
| `kind` | `권고` (official advisory, patch or mitigation notice), `위협 분석` (groups, campaigns, malware, vulnerability trends), `가이드` (practices, tips), `정책` (law, government strategy, regulation). Unknown values become 위협 분석 |
| `title` | Korean title, ≤ 50 characters |
| `summary` | 3-4 Korean sentences in 다체: what the article is about, who is affected, the threat and its evidence. Nothing that isn't in the article |
| `action` | One sentence telling the reader what to do (e.g. "FortiOS를 7.4.5 이상으로 업데이트한다."), or empty |
| `tags` | Four slots: `actor` (threat actor), `product` (affected product or CVE), `technique`, `topic` (abstract theme such as 랜섬웨어, AI보안, 패치관리) |

Names of companies, groups, products and malware and CVE numbers keep the article's spelling.

Checks reuse the incident ones: Korean summary, 다체 enforced (`plain_form` converts 존댓말, anything left is
rejected, also in `action`), transliterated names removed. A failed check retries at a higher temperature with the
problems as a hint.

## Storage

Table `intel`, one row per article (`article_id UNIQUE`; `intel.save` upserts, so re-processing overwrites):

| Column | Content |
|---|---|
| `article_id` | the source article (link, source name and original title come from it) |
| `title`, `kind`, `summary`, `action` | as above |
| `hashtags` | JSON array, up to 4 tags in slot order |
| `published_at` | the article's publication date (cards are grouped and sorted by it) |

## Display and APIs

- `/?section=intel`: cards grouped by publication date, with kind sub-tabs `전체 / 권고 / 위협 분석 / 가이드 / 정책`
  (`?tab=advisory|threat|guide|policy`). Card title and source link go to the original article.
- Search covers title, summary, action, kind, tags, original title and source name.
- The side pane's tag cloud and article chart count only intel when this tab is open (`section=intel` on
  `/api/tagcloud` and `/api/article-stats`).
- `/api/intel?tab=&q=` returns the same list as JSON. `/api/version` includes intel, so live updates cover the tab.
