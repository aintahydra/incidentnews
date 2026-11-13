# KakaotalkParser

Imports incident news and security intel that members share in a KakaoTalk group chat into `data/news.db`, so they
show up on the site next to the RSS-collected ones.

## Usage

Export the chat from PC KakaoTalk (채팅방 메뉴 → 대화 내보내기) and put the `KakaoTalk_{DATE}_{TIME}_*.txt` file in
`int/humint/`. Then run from the project root:

```bash
venv/bin/python scripts/KakaotalkParser/kakao_import.py --dry-run      # list the links that would be imported
venv/bin/python scripts/KakaotalkParser/kakao_import.py                # import and summarise everything new
venv/bin/python scripts/KakaotalkParser/kakao_import.py --import-only  # queue links now, summarise later
venv/bin/python scripts/KakaotalkParser/kakao_import.py --limit 20     # summarise at most 20 queued links
```

Options: explicit file paths instead of `int/humint/KakaoTalk_*.txt`, `--since YYYY-MM-DD` (default 2026-01-01;
earlier messages are ignored), `--config PATH` (default `config.toml`), `-v` for per-link details.
Tests: `venv/bin/python -m pytest scripts/KakaotalkParser -q` (offline).

## What it does

1. **Parse** (`kakao_log.py`). The header gives the chat title (`공급망보안연구회 님과 카카오톡 대화`) and the save time
   (`저장한 날짜 : ...`); `--------------- 2026년 1월 5일 월요일 ---------------` lines give the day, and
   `[이름] [오후 12:51] ...` lines start messages that run until the next one. System lines (invitations, leaving)
   end a message. For each URL in a message it keeps the headline line just before it, the preview text after it
   (often the article's lead or a summary the member wrote), and the member's own comment before the first link.
   Greetings, thanks and photos have no link and produce nothing.
2. **Only new talk.** Table `humint_imports` keeps, per chat, the time of the last message imported. A newer export
   of the same chat repeats the old talk; only messages after that bookmark are read. Files are processed in order
   of their save time. The bookmark only moves forward: once a chat is imported, a later run with an earlier
   `--since` does not go back.
3. **Skip what we have.** A link is skipped when its URL is already in `articles` (from RSS or an earlier import),
   compared after following redirects (old Boannews `media/view.asp?idx=` links resolve to `news/articleView.html?idxno=`)
   and ignoring http/https, `www.`/`m.`, fragments and tracking/paging parameters. YouTube, forms, Zoom, KakaoTalk
   invites, job posts and file links are not articles and are skipped.
4. **Fetch the page** for its text and publication date (`article:published_time` and similar; if missing or not
   within 90 days before the share, the share date is used). If the page can't be read (Cloudflare on Dark Reading
   and SecurityWeek, paywalls), the chat's headline and preview, plus the member's comment when those are short,
   are used as the body. The title is the chat's headline (without tags like `[공유]`), else the page title.
   The article's source is the outlet that published it (보안뉴스, Security Week, 데일리시큐, ...), so cards credit and
   link to the original provider: the name config.toml gives that site's feed, else a built-in list of outlets, else
   the page's `og:site_name`, else the domain. Which chat shared it, and when, goes to table `humint_articles`. Status
   is `humint`. Member names are not stored. Articles stored by older versions as "카카오톡 · <chat>" are renamed at the
   start of the next run, or alone with `--fix-sources`.
5. **Summarise with the LLM in `config.toml`**, exactly as the collector does: the incident extractor first (an
   incident already on the site is merged, its card gains the link as a source); if it isn't an incident, the
   intel extractor (권고 / 위협 분석 / 가이드 / 정책); if neither, the article is `skipped`. Errors are retried on the next
   run, up to 3 attempts. The server's collector never touches status `humint`, so both can run at once (they share
   the LLM, so each is slower while the other works).

A run can be stopped at any time (Ctrl-C) and re-run: links are committed per message and summaries per article.
Expect about 10-40 seconds per link with a local gemma4 model.

`int/` is in `.gitignore`: chat exports contain members' names and private talk and must not be committed.
