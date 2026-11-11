# Design documents

How newscollect gets, stores and shows cybersecurity information. Each document describes the current
implementation; file and function names point at the code that does it.

| Document | Covers |
|---|---|
| [01-incident-news.md](01-incident-news.md) | Collecting incident news from RSS, extracting incidents with the LLM, merging duplicates, storing them |
| [02-intel-advisories.md](02-intel-advisories.md) | Collecting threat intelligence and advisories, summarising each article, storing it |
| [03-tag-graph.md](03-tag-graph.md) | Tags, the tag cloud, and the 태그간 관계 그래프 (tag co-occurrence network and its statistics) |
| [04-kakaotalk-parser.md](04-kakaotalk-parser.md) | Importing links shared in a KakaoTalk group chat (HUMINT) |
| [05-screen-layout.md](05-screen-layout.md) | Page structure, header, side pane, cards, the graph page, responsive and live behaviour |

The database schema itself is in [../docs/database.md](../docs/database.md). The older single-file design notes
are in [../DESIGN.md](../DESIGN.md).

## System overview

```
                 config.toml  ([[sources]], [[intel_sources]], [llm], [collector], [server], ...)
                      │  re-read every cycle / request
                      ▼
  RSS feeds ──► Collector loop (asyncio task in the web process, every interval_minutes)
                 │ 1 fetch feeds ─────────────────► articles (status=new, section=incident|intel)
                 │ 2 fetch article page text
                 │ 3 section=incident: extractor (LLM) ─► dedupe ─► incidents + incident_articles
                 │   section=intel:    intel (LLM) ───────────────► intel
                 ▼
  KakaoTalk ──► scripts/KakaotalkParser/kakao_import.py (run by hand)
  export         parse chat ─► fetch linked page ─► articles (status=humint)
                 ─► same extractors (incident first, else intel)
                      │
                      ▼
               SQLite  data/news.db
                      │
                      ▼
  FastAPI + Jinja2 (newscollect/web.py)
     /                     보안 사고 tab (incident cards by day)
     /?section=intel       인텔·권고 tab (intel cards by day)
     /?section=graph       태그간 관계 그래프 tab
     /incidents/{id}       incident detail
     /api/...              JSON for cards, tag cloud, article chart, tag graph, change polling
```

One process runs both the collector and the web server (`python -m newscollect`); the KakaoTalk importer is a
separate script that writes to the same database. The LLM is either a local Ollama model or Claude
(`[llm].provider`); every LLM step goes through `newscollect/llm.py`, so all features use whichever is configured.
