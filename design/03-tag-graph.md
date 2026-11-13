# Tags, tag cloud and the tag relation graph

## Tags

Every card carries a small set of tags produced by the same LLM call that writes its summary:

| Card | Slots (in order) | Max |
|---|---|---|
| Incident | attacker, victim, vulnerable system, technique, incident type | 5 |
| Intel / advisory | actor, product or CVE, technique, topic | 4 |

A slot the article doesn't support is left empty and dropped. `extractor.clean_hashtags(raw, roles)` normalises
them (placeholders such as 미상/unknown removed, one name per slot, no `#` or spaces, 2-25 characters, no duplicates).
They are stored as a JSON array in `incidents.hashtags` / `intel.hashtags`, rendered as links that search for the tag
in the card's section.

## Tag cloud (side pane)

`newscollect/tagcloud.py`, `GET /api/tagcloud?period=day|week|month|year&section=incident|intel`.

- Cards published in the last 1 / 7 / 30 / 365 days (by publication date, today included).
- Each card counts each tag once; spelling variants that differ only in case are merged under the most common one.
- Whole tags listed in `[tagcloud].exclude` (generic words such as 공격, 해킹, 보안) are left out; cards and search
  are unaffected.
- Top 60, laid out in the browser on an Archimedean spiral (log-scaled font size, the largest in the accent colour).

## Tag relation graph (태그간 관계 그래프)

Question it answers: which tags appear together on the same 글타래 (card) more often than chance, and are those
links backed by the source articles?

It is the analysis of `scripts/tag_link_test.py`, moved into the app as `newscollect/taggraph.py` and served by
`GET /api/tag-graph?scope=all|incident|intel` (the page uses `all`).

### Documents

`load_cards(conn, scope)`:

- each incident card is one document: its tags, plus the titles and bodies of all its linked articles (for the text
  check), and a link to `/incidents/{id}`;
- each intel card is one document: its tags, plus its article's title and body, and a link to the original article.

Two tags are linked when they are on the same document.

### Statistics (`analyze`)

With N documents, `n(A)` documents tagged A, and `k` documents tagged with both A and B:

1. **Candidate pairs.** Only pairs with `k ≥ 2` are tested; a single shared document can't separate a link from
   coincidence.
2. **One-sided Fisher exact test** per pair on the 2×2 table `[[k, n(A)-k], [n(B)-k, N-n(A)-n(B)+k]]`:
   p = P(X ≥ k) for X ~ Hypergeometric(N, n(A), n(B)), computed exactly with `math.comb` (`fisher_greater`; no
   scipy). This matches `scipy.stats.fisher_exact(..., alternative="greater")`.
3. **Benjamini–Hochberg correction** over all tested pairs gives q-values (`bh`). A link is significant when
   `q < 0.05`.
4. **Lift** = `k·N / (n(A)·n(B))`: how many times more often the pair occurs than independence predicts.
5. **Text check.** For every document, each tag (or an alias from `ALIAS`, e.g. 원격코드실행 → "remote code
   execution", 패치관리 → "patch") is searched in the normalised source text. A link counts as grounded on a document
   when both tags are found; `g` = grounded documents out of `k`.
6. **Permutation test.** Tags are shuffled across documents 1,000 times (seed 7), keeping each document's tag
   count, and the number of pairs with `k ≥ 2` is counted each time. The observed count is compared with that
   distribution (`obs`, `nullmean`, `nullmax`, `permp`, `hist`). The API returns it; the page currently doesn't show it.

### API result

```
N, ninc, nint          documents (total, incident, intel)
ntags, npairs, single  distinct tags, distinct pairs, pairs on one document only
tt, tg, reused         tag uses, tag uses found in the text, tags used ≥ 2 times
minsup                 2
nodes[]  {id, n, ni}   tags that are in a tested pair; n documents, ni of them incidents
edges[]  {a, b, k, fa, fb, lift, p, q, g, cards[{sec, id, title, link}]}  sorted by p
obs, nullmean, nullmax, permp, nperm, hist   permutation test
```

### Caching

The result is cached per scope together with the card version from `/api/version`; it is recomputed only when a
card is added, merged or rewritten (about 0.2 s for ~170 documents).

### Drawing (`static/app.js`, 태그간 관계 그래프 section)

- **Layout**: a small deterministic force simulation, no external library. Nodes start on a circle; for 450
  iterations nodes repel (charge plus a hard push when labels would overlap), links pull towards a rest length that
  shrinks with `k`, and a centring force pulls harder along the box's short side. The result is scaled to fit the
  box. The same data always gives the same picture.
- **Size**: drawn at the panel's real width (≤ 900 px) so labels keep their size; on phones (< 600 px) the box is
  1.35 × taller than wide. Redrawn on window resize.
- **Tag filter**: a four-stop slider above the network, "글타래 N개 이상인 태그만". Each stop keeps about a share of
  the linked tags: 전체, ½, ¼, ⅛. For each share the page picks the 글타래 threshold N whose remaining tag count is
  closest to that share (only tags on at least N documents, the links whose two tags both remain, and no tag left
  without a link), skips thresholds that would leave nothing, and drops a stop that repeats the previous threshold.
  Shares rather than evenly spaced thresholds, because tag counts are lopsided: with counts from 2 to 83, even spacing
  gave 79 → 5 → 3 → 2 tags, while the shares give 2+ (79), 3+ (42), 9+ (19), 16+ (10). The stop labels under the
  slider show each threshold and can be tapped. Default ("자동"): ⅛ on a phone (< 600 px), ¼ on wider screens. A
  chosen stop is saved in the browser (`localStorage.graphStop`) as a stop, not a number, so it keeps its meaning as
  data grows; 자동 clears it. A counter shows "태그 shown/total · 연결 shown/total". The table below still lists every
  tested pair.
- **Encoding**: node radius grows with √(documents); line width grows with `k`; blue solid = significant
  (`q < 0.05`), grey = not significant; dashed = at least one document where the tags weren't found in the text.
- **Most-used tags**: the five tags on the most documents (ranked over all tags, not the visible ones, so a tag keeps
  its colour at every filter stop) each get a categorical colour (orange, aqua, yellow, magenta, violet: slots 2-5 and
  7 of the dataviz reference palette, light and dark steps; blue slot 1 already means "significant"). Their circles
  and their links take that colour; a link between two of them takes the more-used one's colour; a coloured link is
  opaque when significant and faded when not, and hub links are drawn on top. A legend row lists the five tags with
  their colours and counts. The palette passes the colour-blind separation checks in both themes; three colours are
  below 3:1 contrast on the light surface, so every coloured tag is labelled in the graph and named in the legend.
- **Interaction**: hovering a line shows the pair, `k`, lift, q and up to three document titles, and highlights its
  table row; clicking a line scrolls to that row; clicking a tag searches for it (in the intel tab when the tag occurs
  only on intel cards).
- **Table**: every tested pair with k, n(A), n(B), lift, p, q, verdict (✓ 연결 / – 불충분) and text support, and a
  collapsible list of its documents linking to the incident detail or the original article.
- **Updates**: the page polls `/api/version` every `[server].refresh_seconds` and re-fetches when cards change.

### Limits (also stated on the page)

Most pairs occur on one document only and are not tested; multiple reports of one event can inflate a link; the text
check is literal (generous for abstract tags reached through aliases); with a few hundred documents the test has
low power, and results change as data accumulates.
