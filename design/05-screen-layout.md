# Screen layout

Server-rendered pages (FastAPI + Jinja2 templates in `newscollect/templates/`), one stylesheet
(`static/style.css`) and one script (`static/app.js`, no frameworks or external libraries). All UI text is Korean.

## Templates

| Template | Used for |
|---|---|
| `base.html` | Page shell: header, `.layout` (side pane + main), footer; theme and pane state set before first paint |
| `_sections.html` | The header's section switch: 보안 사고 · 인텔·권고 · 태그간 관계 그래프 |
| `index.html` | 보안 사고 and 인텔·권고 tabs: side pane, sub-tabs, cards grouped by day |
| `_card.html`, `_intel_card.html` | One incident card / one intel card |
| `detail.html`, `_detail_card.html` | Incident detail page (`/incidents/{id}`) |
| `graph.html` | 태그간 관계 그래프 tab |

## Header (sticky)

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ [▥] 사이버보안 동향정보 ( 보안 사고 68 | 인텔·권고 98 | 태그간 관계 그래프 )  [검색창 ][검색] [☀ 라이트][🌙 다크] │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

- `[▥]` toggles the side pane (list pages only).
- The site title comes from `[server].site_title` and links to `/`.
- The section switch is a pill group; the current section is filled blue. Counts are cards in each section
  (for the current search). Links: `/`, `/?section=intel`, `/?section=graph`.
- The search box searches the current section (`?q=`; on the intel tab it keeps `section=intel`). Placeholder text
  changes with the section.
- Theme buttons set `data-theme` on `<html>` (saved in `localStorage`; default follows the OS).
- The header stays at the top while scrolling; its height is published as `--header-h` so the side pane can stick
  right below it.

## 보안 사고 / 인텔·권고 tabs (`index.html`)

```
┌─ side pane (376px) ─────┐ ┌─ main (≤ 760px, centred) ──────────────────────┐
│ 태그 클라우드             │ │ 전체 12 | 정부공공 3          ← sub-tabs           │
│ [일간][주간][월간][연간]   │ │ (인텔: 전체 | 권고 | 위협 분석 | 가이드 | 정책)    │
│   #LockBit  #랜섬웨어 ... │ │                                                   │
│                          │ │ ▌2026-11-10 (월)               ← day heading     │
│ 수집 기사 수              │ │ ┌ card ───────────────────────────────────────┐ │
│ [날짜별][월별][연도별]     │ │ │ title  [category] [정부기관]                  │ │
│  ▂▅▃▇▂▁▆  bar chart      │ │ │ 발생 / 공격자 / 피해 / 규모                     │ │
│  평균 … (기간)            │ │ │ summary                                       │ │
│  ▸ 표로 보기              │ │ │ #tag #tag #tag                                │ │
└──────────────────────────┘ │ │ 보안뉴스 · The Hacker News  [요약 다시 생성]     │ │
                             │ └───────────────────────────────────────────────┘ │
                             └───────────────────────────────────────────────────┘
```

- **Grid**: three columns `pane | list | spacer` keep the list centred while there is room; the pane's widgets sit
  against the list. Pane closed: the list stays centred.
- **Sub-tabs**: incidents `전체 / 정부공공` (victim is a government or public body: the KOSIS and public-institution
  lists in `refs/`, plus local public bodies recognised by name, see `orgs.py`); intel `전체 / 권고 / 위협 분석 / 가이드 / 정책`.
- **Day groups**: incidents by the earliest article's publication date, intel by the article's publication date,
  newest first, with the weekday.
- **Incident card**: title (to the detail page), category tag, 정부기관/공공기관 tags, a definition list (발생 only when
  it differs from the display date, 공격자, 피해, 규모 when known), the summary, tag links, source links, and the
  "요약 다시 생성" button with an inline status message.
- **더 보기**: each tab shows the newest 100 cards; a "더 보기 (shown / total)" button under the list adds the next
  100 in place (`?n=200`, `?n=300`, ...; it keeps the search and sub-tab, and also works as a plain link without
  JavaScript). The fetched cards are merged into the existing day groups, and the address is updated so live
  updates keep the longer list.
- **Intel card**: title (to the original), kind tag, summary, a highlighted "권고 조치" line when present, tag links,
  and the source name with the original title.
- **Side pane**: the tag cloud and the "수집 기사 수" bar chart for the current section ([03](03-tag-graph.md)). The
  chart counts collected articles by publication date (daily over `max_age_days`, monthly over 12 months, or yearly),
  shows the average over days or months that have data, a hover/focus tooltip and a table view. The pane's
  open/closed state is remembered.

## 태그간 관계 그래프 tab (`graph.html`)

No side pane; one wider column (≤ 960px).

```
┌─ main (≤ 960px) ─────────────────────────────────────────────────┐
│ ▌태그 연결망                                                      │
│ explanation · legend (유의함 / 유의하지 않음 / 원문 미확인 / 태그)     │
│ 글타래 [9]개 이상인 태그만  ●───●───◉───●  [자동]  태그 19/79 · 연결 28/91 │
│                          전체 2+ ½ 3+ ¼ 9+ ⅛ 16+                     │
│ ┌ network panel ───────────────────────────────────────────────┐ │
│ │     #패치관리 ━━ #PHP ━━ #RCE        #정책 ── #취약점관리        │ │
│ │          ╲ ┄┄ #피싱     #AI보안 ── #TAG-217 ...               │ │
│ └──────────────────────────────────────────────────────────────┘ │
│ ▌검정한 태그 쌍                                                    │
│ table: 태그 쌍 (▸ 글타래 N개) | k | n(A) | n(B) | 향상도 | p | q | 판정 | 원문 │
│ ▌해석할 때 주의할 점                                               │
│ numbered caveats · 방법 note                                       │
└──────────────────────────────────────────────────────────────────┘
```

Cards are called 글타래 on this page. Drawing and interaction are described in [03-tag-graph.md](03-tag-graph.md).

## Incident detail (`/incidents/{id}`)

The same card in a detail layout (all fields and every source article), without the side pane.

## Live updates

- List pages poll `/api/version` every `[server].refresh_seconds` (600 by default; 0 turns it off; paused while the
  browser tab is hidden). When cards change:
  - reader at the top of the page: the list is replaced at once and new cards get a green "새 사고" / "새 글" badge;
  - reader scrolled down: nothing moves; a pill "↑ 새 사고 N건 보기" appears under the header and the tab title shows
    `(N)`; clicking it or scrolling to the top applies the update. Updates wait while a summary is regenerating or
    text is selected.
  - new articles also redraw the tag cloud and the chart.
- The graph page re-fetches the graph when the card version changes.

## Responsive behaviour

| Width | Behaviour |
|---|---|
| ≥ 900px | Side pane beside the list (open by default, state remembered) |
| < 900px | Side pane closed by default; opens as a fixed drawer over the list with a "✕ 닫기" button; tapping outside or Esc closes it |
| < 700px | Header wraps: title and theme buttons on the first row, the section switch on its own full-width row, then the search box |
| < 600px | Graph drawn taller than wide so labels stay readable |

Page width never scrolls horizontally; the pair table scrolls inside its own box.

## Theming and accessibility

- Colours are CSS variables (`--bg`, `--fg`, `--mut`, `--card`, `--acc`, `--line`, `--series-1`) defined for dark and
  light; dark by default unless the OS prefers light.
- Tabs and the section switch use `aria-current="page"`; toggle buttons use `aria-pressed` / `aria-expanded`; the
  chart has keyboard-focusable bars and a table alternative; regenerate status messages use `role="alert"`.
