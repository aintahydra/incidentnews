# KakaoTalk chat import (HUMINT)

Goal: members of a KakaoTalk group (e.g. 공급망보안연구회) share and discuss security news. The links they share,
with the headline and preview around them, are turned into incident or intel cards on the site, using the same
LLM pipeline as RSS articles.

Code: `scripts/KakaotalkParser/` (`kakao_log.py` parser, `kakao_import.py` importer, `test_kakao.py`, README).
Input: PC KakaoTalk exports `KakaoTalk_{DATE}_{TIME}_*.txt` in `int/humint/` (git-ignored: they contain members'
names and private talk). The importer is run by hand; it is not part of the server.

```bash
venv/bin/python scripts/KakaotalkParser/kakao_import.py [FILE ...] [--since 2026-01-01] [--limit N]
                                                        [--dry-run] [--import-only] [--config config.toml] [-v]
```

## Export format

```
공급망보안연구회 님과 카카오톡 대화                     ← chat title
저장한 날짜 : 2026-11-10 18:41:20                      ← when the export was saved

--------------- 2026년 1월 5일 월요일 ---------------   ← day separator
홍길동님이 김철수님을 초대했습니다.                        ← system line
[홍길동] [오후 12:51] 국정원에서 ... 결과를 공개했습니다. ← message start: [name] [오전|오후 H:MM]
                                                      ← continuation lines belong to the same message
국정원, 152개 기관 ‘2025 사이버보안 실태평가’ 공개…     ← headline line
https://www.dailysecu.com/news/articleView.html?idxno=203973
‘우수’ 공공기관 32개로 증가…                            ← preview text
[홍길동] [오후 12:52] 사진                              ← next message
```

## Parsing (`kakao_log.py`)

- `parse(text)` → `Log(chat, saved_at, messages)`. CRLF and BOM are handled. A message starts at
  `[name] [오전|오후 H:MM]` (오전 12 = 00 h, 오후 12 = 12 h), takes its date from the last day separator, and runs
  until the next message, day separator or system line (invitations, joins, leaves, removals, admin notices).
- `links(message)` → one `Link` per URL in the message:
  - `url`, trailing punctuation removed;
  - `title`: the text before the URL on the same line (≥ 8 characters), else the non-empty line just above it;
  - `preview`: the lines after the URL up to the next link's headline (usually the article lead or a summary the
    member wrote);
  - `comment`: the member's own words before the first link of the message;
  - `when`: the message time.
- Messages without links (greetings, thanks, photos, emoticons, discussion) produce nothing.

## Importing (`kakao_import.py`)

```
for each export, oldest 저장한 날짜 first:
  parse ─► messages on/after --since (default 2026-01-01) and after the chat's bookmark
  for each link in those messages:
     skip if not an article  (YouTube, forms, Zoom, KakaoTalk invites, job posts, file links)
     skip if URL already known
     fetch page ─► final URL (after redirects), text, og:title, publication date
     skip if final URL already known
     INSERT articles(status='humint', source='카카오톡 · <chat>', ...)
  advance the bookmark, commit (per message)
LLM pass over articles with status='humint' and attempts < 3
```

### Only new conversation

Table `humint_imports` keeps one bookmark per chat: the `(minute, n-th message in that minute)` of the latest
message imported. A newer export of the same chat repeats all earlier talk; only messages after the bookmark are
read. The bookmark only moves forward, so a later run with an earlier `--since` does not backfill.

### Known URLs

A link is skipped when it is already in `articles` (from RSS, or an earlier import). URLs are compared by a key
(`norm_url`) that ignores http/https, a `www.` or `m.` host prefix, fragments, tracking and paging parameters
(`utm_*`, `fbclid`, `page`, `mkind`, `kind`, ...) and a trailing slash. The key is checked before fetching and again
on the final URL, so an old Boannews link (`media/view.asp?idx=141289`) that redirects to the stored
`news/articleView.html?idxno=141289` is recognised.

### Article fields

| Field | Value |
|---|---|
| `url` | final URL after redirects (https kept when the shared link was https) |
| `title` | the chat headline without share tags such as `[공유]`, `[일본]`; else the page title without " - 사이트명"; else the URL |
| `body` | the page text (≥ 200 characters); else the chat headline + preview, plus the member's comment when those are short |
| `published_at` | the page's `article:published_time` (or similar) when it lies within 90 days before the share; else the share date |
| `source` | `카카오톡 · 공급망보안연구회` — member names are not stored |
| `status` | `humint` (the server's collector ignores it) |

Pages that block automated requests (Cloudflare on Dark Reading and SecurityWeek, paywalls) are not worked around;
the chat's own headline and preview are used instead.

### LLM pass

Same steps as the collector ([01](01-incident-news.md), [02](02-intel-advisories.md)), with the LLM in
`config.toml`:

1. `extractor.extract`: if it is an incident, `dedupe.upsert_incident` creates a card or merges into an existing one
   (the shared link becomes one of its sources); `section='incident'`, `status='done'`.
2. Otherwise `intel.extract`: if relevant, `intel.save`; `section='intel'`, `status='done'`.
3. Otherwise `status='skipped'`.
4. Errors: `attempts+1`, error stored; after 3 attempts `status='failed'`. An unreachable LLM stops the pass and leaves
   the rest queued.

Each article is committed on its own, so the script can be stopped and re-run at any time. `--import-only` queues
links without the LLM pass; `--limit N` processes at most N; `--dry-run` only lists what would be imported.

## Tests

`venv/bin/python -m pytest scripts/KakaotalkParser -q` runs offline (page fetches stubbed, no LLM): header, days,
message times, link splitting, URL normalisation, redirect-based skipping, headline choice, and incremental import
across two exports.
