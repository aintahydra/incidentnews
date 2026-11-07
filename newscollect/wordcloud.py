"""Keyword counts for the word cloud: incident titles + victim/attacker names, per period."""
import re
from collections import Counter
from datetime import date, timedelta

PERIODS = {"day": 1, "week": 7, "month": 30, "year": 365}  # days, today included

# particles stripped from the end of a Korean word ("해킹으로" -> "해킹"); 이/가 are left alone because many
# names end in them (카카오페이)
_JOSA = ("에서", "으로", "에게", "까지", "부터", "에도", "이며", "이자", "을", "를", "은", "는", "의", "과", "와", "로", "에", "도")
_STOP = {
    # generic incident-news words that would dominate every period
    "사고", "발생", "피해", "확인", "관련", "의혹", "정황", "가능성", "주장", "공지", "안내", "대한", "통해", "위해", "이후",
    "대상", "사건", "사실", "조사", "추정", "최근", "이상", "대규모", "단독", "카드뉴스", "속보", "종합", "공식", "실제", "거쳐", "국내", "해외", "일부", "전체", "등", "및", "약", "또",
    "the", "and", "for", "with", "from", "into", "via", "new", "over", "after",
}
_VERB = re.compile(r"(했|됐|한|된|있|없|였|졌)다$")  # "태업했다", "급증한다": predicates, not keywords
_BRACKET_TAG = re.compile(r"\[[^\]]{1,10}\]")  # [단독], [카드뉴스]
_SPLIT = re.compile(r"[\s,.·…:;!?\"'“”‘’()<>\[\]{}/|~=+]+")


def _strip_josa(w: str) -> str:
    for j in _JOSA:
        if w.endswith(j) and len(w) - len(j) >= 2:
            return w[:-len(j)]
    return w


def trivial_pattern(trivial) -> re.Pattern | None:
    """Regex removing the user's trivial keywords ([wordcloud].trivial) anywhere inside a word, longest first
    so "사이버공격" is removed whole before "공격"; case-insensitive for Latin ("DDoS" = "ddos")."""
    keys = sorted({k.strip() for k in trivial or () if k and k.strip()}, key=len, reverse=True)
    return re.compile("|".join(map(re.escape, keys)), re.I) if keys else None


def _keep(tok: str) -> bool:
    return (len(tok) >= 2 and tok.lower() not in _STOP and not _VERB.search(tok)
            and bool(re.search(r"[가-힣A-Za-z]", tok)))


def words(text: str, trivial: re.Pattern | None = None) -> set[str]:
    out = set()
    for tok in _SPLIT.split(_BRACKET_TAG.sub(" ", text or "")):
        tok = tok.strip("-_")
        if not tok or tok[0].isdigit():  # "3만건", "2026년": numbers are not keywords
            continue
        if re.search(r"[가-힣]", tok):
            tok = _strip_josa(tok)  # "금융권해킹으로" -> "금융권해킹"
        if trivial is not None:  # "금융권해킹" -> "금융권"; "사이버공격" -> "" (dropped)
            tok = trivial.sub("", tok).strip("-_")
        if _keep(tok):
            out.add(tok)
    return out


def since(period: str, today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=PERIODS[period] - 1)).isoformat()


def cloud(rows, limit: int = 60, trivial=()) -> list[dict]:
    """rows: incidents (title, victim, attacker). Each word counts once per incident.
    trivial: keywords removed from inside every title word; a word left empty is dropped."""
    pat = trivial_pattern(trivial)
    counts: Counter[str] = Counter()
    for r in rows:
        terms = words(r["title"], pat)
        for f in ("victim", "attacker"):  # names count as one term even with spaces ("Scattered Spider")
            name = (r[f] or "").strip()
            # names are kept whole ("사이버작전사령부" stays as is); only a name that is nothing but trivial
            # keywords (attacker "해커") is dropped
            if pat is not None and not pat.sub("", name).strip(" -_"):
                continue
            if 2 <= len(name) <= 30:
                terms = {t for t in terms if t not in name.split()} | {name}
        counts.update(terms)
    return [{"word": w, "count": c} for w, c in counts.most_common(limit)]
