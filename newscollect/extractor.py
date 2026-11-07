"""Incident extraction: LLM (Ollama) with a keyword-heuristic fallback."""
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import llm as llmmod

INCIDENT_WORDS = ["해킹", "랜섬웨어", "유출", "침해", "공격", "탈취", "감염", "피싱", "악성코드",
                  "디도스", "DDoS", "먹통", "사칭", "해커", "데이터 노출", "침투", "크리덴셜"]

SYSTEM = """너는 사이버 보안 뉴스에서 '사고'를 추출하는 분석가다. 반드시 JSON 객체 하나만 출력한다.
사고란 해킹, 랜섬웨어, 개인정보/데이터 유출, 침해, 디도스, 악성코드 감염 등으로 실제 피해(유출, 서비스 마비, 금전 요구 등)가 발생한 보안 사건이다.
다음은 사고가 아니므로 is_incident=false: 취약점 발견·권고·패치, 잠재적 위험 경고, 연구 결과, 정책·제도, 제품·행사 소개, 통계·설문, 의견 기사.
기사는 한국어이거나 영어일 수 있다. 영어 기사여도 title·damage·summary 등 모든 문자열 값은 반드시 한국어(한글)로 작성한다. (Write all values in Korean.)
단, 기업·기관·공격 그룹·제품·악성코드 이름은 번역하거나 음차하지 말고 기사에 나온 원문 표기 그대로 쓴다(예: Microsoft, LockBit, Binance, 보안뉴스).
금액·건수는 기사의 숫자와 단위를 정확히 옮긴다(예: $6.3 billion → 63억 달러). 확실하지 않으면 원문 표기 그대로 쓴다.
필드:
- is_incident: boolean
- title: 사고를 한 줄로 요약한 제목(40자 이내, 한국어)
- occurred_at: 사고 발생일 YYYY-MM-DD (알 수 없으면 기사 게시일 사용)
- attacker: 공격자/공격 그룹. 기사 본문에 쓰인 표기를 그대로 옮긴다(한국어 기사는 한글 표기, 영어 기사는 영어 표기. 번역·음차 금지). 모르면 ""
- victim: 피해 기업/기관. 기사 본문에 쓰인 표기를 그대로 옮긴다(한국어 기사는 한글 표기, 영어 기사는 영어 표기. 번역·음차 금지). 모르면 ""
- damage: 피해 규모(건수, 금액, 영향 등)를 한국어로 (모르면 "")
- category: 랜섬웨어|데이터 유출|해킹|악성코드|DDoS|피싱|기타 중 하나
- summary: 한국어 2~4문장. 언제, 누가 누구를 공격했는지, 피해 규모가 어느 정도인지 반드시 포함. 기사에 없는 내용은 지어내지 않는다. 모든 문장은 '~했다.', '~이다.'처럼 '다.'로 끝나는 평서문으로 쓰고, 존댓말('~습니다', '~입니다', '~해요')은 절대 쓰지 않는다.
- hashtags: 이 사고를 대표하는 해시태그 3개의 배열. '#' 없이, 띄어쓰기 없이 쓴다. 피해 대상, 공격 유형·수법, 업종·영향처럼 서로 다른 측면을 하나씩 담는다(예: ["SK텔레콤", "유심정보유출", "통신사"], ["LockBit", "랜섬웨어", "제조업"]). 이름은 기사 표기 그대로, 나머지는 한국어로."""


HANGUL = re.compile(r"[가-힣]{2}")


@dataclass
class Extraction:
    is_incident: bool
    title: str = ""
    occurred_at: str = ""
    attacker: str = ""
    victim: str = ""
    damage: str = ""
    category: str = ""
    summary: str = ""
    method: str = "llm"
    hashtags: list[str] = field(default_factory=list)


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def clip(text: str, limit: int = 450) -> str:
    """Cap runaway summaries, cutting at a sentence boundary when possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("다. "), cut.rfind("."))
    return cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…"


MAX_BACKDATE_DAYS = 180


def plausible_date(occurred: str, published_at: str) -> str:
    """The model sometimes invents dates (2021 for a 2026 article): keep only dates up to ~6 months before publishing."""
    try:
        d, p = date.fromisoformat(occurred), date.fromisoformat(published_at)
    except ValueError:
        return published_at
    return occurred if p - timedelta(days=MAX_BACKDATE_DAYS) <= d <= p else published_at


def parse_llm_json(text: str, published_at: str) -> Extraction:
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    occurred = _s(data.get("occurred_at"))
    occurred = plausible_date(occurred, published_at) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", occurred) else published_at
    return Extraction(bool(data.get("is_incident")), _s(data.get("title")), occurred,
                      _s(data.get("attacker")), _s(data.get("victim")), _s(data.get("damage")),
                      _s(data.get("category")), clip(plain_form(_s(data.get("summary")))), hashtags=clean_hashtags(data.get("hashtags")))


MAX_HASHTAGS = 3


def clean_hashtags(raw) -> list[str]:
    """Up to 3 distinct tags: '#' and spaces removed, 2-20 chars. Accepts a list or a "#a #b, #c" string.
    Bad tags are dropped rather than failing the extraction (tags are a convenience, not a checked field)."""
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+(?=#)|,", raw) if isinstance(raw, str) else []
    out: list[str] = []
    for t in items:
        t = re.sub(r"[\s#]+", "", _s(t)).strip(".,·'\"“”‘’")
        if 2 <= len(t) <= 20 and re.search(r"[0-9A-Za-z가-힣]", t) and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:MAX_HASHTAGS]


TOKEN = re.compile(r"[0-9A-Za-z가-힣]{2,}")
RETRY_TEMPERATURES = (0.3, 0.6)  # a retry at temperature 0 would just repeat the same answer


def _squash(text: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", text.lower())


def ungrounded_names(ex: Extraction, source_text: str) -> list[str]:
    """Name tokens of victim/attacker that appear nowhere in the article (small models garble names)."""
    src = _squash(source_text)
    bad = []
    for field in ("victim", "attacker"):
        tokens = TOKEN.findall(getattr(ex, field))
        if tokens and not any(_squash(t) in src for t in tokens):
            bad += tokens
    return bad


def _english_text(text: str) -> bool:
    """A phrase written in English instead of Korean (names alone don't count: needs 3+ English words)."""
    return not re.search(r"[가-힣]", text) and len(re.findall(r"[A-Za-z]{3,}", text)) >= 3


def _problems(ex: Extraction, source_text: str, check_names: bool = True) -> list[str]:
    if not ex.is_incident:
        return []
    problems = []
    if not ex.title or _english_text(ex.title) or not HANGUL.search(ex.title):
        problems.append("title이 한국어가 아님")
    if _english_text(ex.damage):
        problems.append("damage가 한국어가 아님")
    if not (ex.summary and HANGUL.search(ex.summary)):
        problems.append("summary가 비었거나 한국어가 아님")
    bad = ungrounded_names(ex, source_text) if check_names else []
    if bad:
        problems.append("기사 본문에 없는 이름: " + ", ".join(bad))
    return problems


def repair_extraction(ex: Extraction, source_text: str) -> Extraction:
    """Names that are not in the article (a descriptive "정부 기관", a garbled company) are blanked in the
    victim/attacker fields. For a Korean article, a bad name that also leaked into the title/summary/damage makes the
    result unusable; for other languages Korean words in the prose can't be matched against the source, so they pass."""
    bad = ungrounded_names(ex, source_text)
    if not bad:
        return ex
    for f in ("victim", "attacker"):
        if any(t in getattr(ex, f) for t in bad):
            setattr(ex, f, "")
    if is_korean(source_text[:700]) and any(t in getattr(ex, f) for t in bad for f in ("title", "summary", "damage")):
        raise ValueError("LLM 결과 검증 실패: 기사 본문에 없는 이름이 제목/요약에 쓰임: " + ", ".join(bad))
    return ex


def extract_llm(llm, model: str, article: dict, temperature: float = 0, deadline: float | None = None) -> Extraction:
    """llm: an llm.Ollama / llm.Claude backend."""
    body = article["body"][:4000]
    user = (f"게시일: {article['published_at']}\n제목: {article['title']}\n본문: {body}"
            "\n\nJSON만 출력하고, title과 summary는 반드시 한국어로 작성하라.")
    source_text = article["title"] + " " + body
    user += names_hint(source_text)
    problems: list[str] = []
    seen: list[str] = []  # distinct problems across all attempts, for the final error
    last: Extraction | None = None
    for temp in (temperature, *RETRY_TEMPERATURES):
        if deadline is not None and time.monotonic() > deadline:
            seen.append("시간 초과")
            break
        hint = f"\n\n이전 답변의 문제: {'; '.join(problems)}. 기사에 나온 이름을 한 글자도 바꾸지 말고 원문 표기 그대로(영어 기사라면 영어 그대로) 사용하라." if problems else ""
        try:
            content = llm.chat(model, SYSTEM, user + hint, json=True, temperature=max(temp, temperature), max_tokens=1000)
            ex = parse_llm_json(content, article["published_at"])
        except (llmmod.LLMError, json.JSONDecodeError, KeyError) as e:  # transient 5xx / refusal / bad JSON: retry
            problems = [f"LLM 응답 오류: {type(e).__name__}"]
            seen += [p for p in problems if p not in seen]
            continue
        if ex.is_incident and not is_korean(source_text[:700]):
            names = proper_nouns(source_text, limit=30)
            ex.title, ex.summary, ex.damage = (strip_transliterations(x, names) for x in (ex.title, ex.summary, ex.damage))
        problems = _problems(ex, source_text)
        if not problems:
            return ex
        last = ex
        seen += [p for p in problems if p not in seen]
    if last is not None and last.is_incident and not _problems(last, source_text, check_names=False):
        return repair_extraction(last, source_text)  # only the victim/attacker names were wrong
    raise ValueError("LLM 결과 검증 실패(" + str(1 + len(RETRY_TEMPERATURES)) + "회 시도): " + "; ".join(seen))


# ---- dedicated summary step -------------------------------------------------
# Inside a JSON object a 10B model writes cramped notes ("유출 추정 항목: …", "~했음").
# Writing the summary as plain prose, with the extracted fields as scaffolding, reads far better.

SUMMARY_SYSTEM = """너는 보안 뉴스 편집자다. 주어진 기사로 사이버 보안 사고 요약문을 쓴다.
규칙:
- 3~4개의 완전한 문장으로 쓴다. 모든 문장은 '~했다.', '~됐다.', '~이다.'처럼 '다.'로 끝낸다. '~했음'이나 명사로 끝나는 메모체, 목록, '△', 괄호 메모는 쓰지 않는다.
- 신문 기사체(평서문)로 쓴다. 존댓말은 절대 쓰지 않는다: '~했습니다'가 아니라 '~했다', '~입니다'가 아니라 '~이다', '~합니다'가 아니라 '~한다'.
- 첫 문장: 언제, 누가(공격자) 누구를(피해 대상) 어떻게 공격했는지. 공격자를 모르면 공격자는 언급하지 않는다.
- 두 번째 문장: 피해 규모(건수, 금액, 유출 항목, 서비스 영향).
- 마지막 문장: 회사·당국의 대응이나 후속 조치.
- 기사에 없는 내용은 쓰지 않는다. 기업·기관·그룹·제품 이름은 기사에 나온 표기를 한 글자도 바꾸지 않는다.
- 기사가 영어여도 요약은 반드시 한국어로 쓰되, 위 이름들은 번역·음차하지 않고 영어 원문 그대로 쓴다. 금액·건수는 기사의 숫자와 단위를 정확히 옮긴다(예: $6.3 billion → 63억 달러).
- 같은 내용을 반복하지 않는다. 매체 이름, 기자, 출처 표기는 쓰지 않는다.
- 요약문 본문만 출력한다. 제목, 머리말, 따옴표, JSON을 붙이지 않는다.

형식 예시(내용이 아니라 문장 구조만 참고):
2026년 3월 2일 A사가 랜섬웨어 조직 B의 공격을 받아 주요 서버가 암호화됐다. 이 사고로 고객 12만 명의 이름과 연락처가 유출된 것으로 확인됐고, 서비스는 이틀간 중단됐다. A사는 외부 전문기관과 원인을 조사하고 있으며 피해 고객에게 개별 안내를 진행하고 있다."""

# a period after an English abbreviation or initial is not a sentence end: "Perry Johnson & Associates, Inc. (PJ&A)",
# "U.S. agencies", "John F. Kennedy" (otherwise the summary check flags a fine sentence as 메모체)
_ABBREV = ("Inc", "Corp", "Ltd", "Co", "LLC", "Bros", "Jr", "Sr", "Mr", "Ms", "Mrs", "Dr", "St", "No", "vs", "etc", "U.S", "U.K")
_SENT_SPLIT = re.compile("".join(rf"(?<!\b{re.escape(a)}\.)" for a in _ABBREV) + r"(?<!\b[A-Z]\.)(?<=[.!?])\s+")
_SENT_END = re.compile(r"다[.!?」”’\"')]*$")
_POLITE_END = re.compile(r"([가-힣])니다[.!?」”’\"')]*$|요[.!?」”’\"')]*$")


def _polite(sentence: str) -> bool:
    """존댓말 ending: "~했습니다." / "~입니다." "~합니다." (ㅂ before 니다) / "~했어요." — but not "아니다."."""
    m = _POLITE_END.search(sentence)
    return bool(m) and (m.group(1) is None or m.group(1) == "습" or _jong(m.group(1)) == 17)


def _jong(ch: str) -> int:
    """Final-consonant index of a Hangul syllable (0 = none, 20 = ㅆ, 18 = ㅄ); -1 for anything else."""
    return (ord(ch) - 0xAC00) % 28 if "가" <= ch <= "힣" else -1


def plain_form(text: str) -> str:
    """Polite sentence endings -> plain news style, only where the conversion is unambiguous:
    past/existential "~었습니다/~했습니다/~겠습니다/~있습니다/~없습니다" -> "~었다/...", "~입니다" -> "~이다",
    "~됩니다" -> "~된다", "~아닙니다" -> "~아니다". Others ("~합니다": 필요하다 vs 발표한다) are left for the
    summary check to reject, so the model rewrites them."""
    def past(m):
        return m.group(1) + "다" if _jong(m.group(1)) in (20, 18) else m.group(0)  # syllable ending in ㅆ or ㅄ
    text = re.sub(r"([가-힣])습니다(?=[.!?」”’\"')]*(\s|$))", past, text)
    text = re.sub(r"아닙니다(?=[.!?」”’\"')]*(\s|$))", "아니다", text)
    text = re.sub(r"입니다(?=[.!?」”’\"')]*(\s|$))", "이다", text)
    return re.sub(r"됩니다(?=[.!?」”’\"')]*(\s|$))", "된다", text)
_HANJA = re.compile(r"[\u4e00-\u9fff]")


def summary_problems(text: str, forbidden: list[str] = (), source_text: str | None = None,
                     allowed_years: set[str] | None = None) -> list[str]:
    """Why a summary reads badly (empty list = fine)."""
    text = text.strip()
    if not HANGUL.search(text):
        return ["한국어가 아님"]
    problems = []
    if len(text) < 40:
        problems.append("너무 짧음")
    if len(text) > 520:
        problems.append("너무 긺")
    if re.search(r"[△▲▶■●※《》]|^\s*[-*•]", text, re.M):
        problems.append("목록/기호 사용")
    if len(_HANJA.findall(text)) > 3:
        problems.append("한자가 섞임")
    sents = [x.strip() for x in _SENT_SPLIT.split(text) if x.strip()]
    if len(sents) > 6:
        problems.append("문장이 너무 많음")
    if any(_polite(x) for x in sents):
        problems.append("존댓말 사용('~습니다', '~입니다', '~요'): '~했다', '~이다'처럼 '~다'체로 써야 함")
    broken = [x for x in sents if not _SENT_END.search(x)]
    if len(broken) > len(sents) // 5:
        problems.append("'~다.'로 끝나지 않는 문장이 있음(메모체)")
    keys = [_squash(x)[:15] for x in sents]
    if len(set(keys)) < len(keys):
        problems.append("같은 문장 반복")
    bad = [t for t in forbidden if t and t in text]
    if bad:
        problems.append("기사에 없는 이름: " + ", ".join(bad))
    if allowed_years is not None:  # the model invents years ("2023년" in a 2026 article)
        wrong = sorted({y for y in re.findall(r"(?<!\d)((?:19|20)\d{2})\s*년", text) if y not in allowed_years})
        if wrong:
            problems.append("기사에 없는 연도: " + ", ".join(wrong))
    if source_text is not None:  # English words the article never used = the model drifted into English
        src = source_text.lower()
        foreign = sorted({w for w in re.findall(r"[A-Za-z]{4,}", text) if w.lower() not in src})
        if foreign:
            problems.append("기사에 없는 영어 단어: " + ", ".join(foreign[:4]))
    return problems


def known_years(source_text: str, published_at: str) -> set[str]:
    return set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", source_text)) | {published_at[:4]}


def strip_transliterations(text: str, names: list[str]) -> str:
    """"클로이데(Claude)" -> "Claude": a (bad) Korean transliteration written before the real English name."""
    for n in names:
        text = re.sub(r"[가-힣]{2,}\s*\(\s*" + re.escape(n) + r"\s*\)", n, text)
    return text


def _clean_summary(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("\"'“”‘’")
    return re.sub(r"^(요약|요약문)\s*[:：]\s*", "", text)


def write_summary(llm, model: str, article: dict, fields: dict, temperature: float = 0.2,
                  deadline: float | None = None, forbidden: list[str] = ()) -> str:
    """Plain-prose summary; retried (hotter, with the reasons as a hint) until it passes summary_problems."""
    body = article["body"][:4000]
    facts = [("발생일", article["published_at"]), ("피해 대상", fields.get("victim")),
             ("공격자", fields.get("attacker")), ("피해 규모", fields.get("damage"))]
    info = "\n".join(f"- {k}: {v}" for k, v in facts if v)
    user = (f"확인된 정보(참고용. 기사와 다르면 기사를 따른다):\n{info}\n\n"
            f"제목: {article['title']}\n본문: {body}\n\n위 기사의 요약문만 출력하라.") + names_hint(article["title"] + " " + body)
    source_text = article["title"] + " " + body
    problems: list[str] = []
    seen: list[str] = []
    for temp in (temperature, *RETRY_TEMPERATURES):
        if deadline is not None and time.monotonic() > deadline:
            seen.append("시간 초과")
            break
        hint = f"\n\n이전 답변의 문제: {'; '.join(problems)}. 규칙을 지켜 다시 쓰라." if problems else ""
        try:
            text = plain_form(_clean_summary(llm.chat(model, SUMMARY_SYSTEM, user + hint,
                                                      temperature=max(temp, temperature), max_tokens=700)))
        except llmmod.LLMError as e:
            problems = [f"LLM 응답 오류: {type(e).__name__}"]
            seen += [p for p in problems if p not in seen]
            continue
        if not is_korean(source_text):
            text = strip_transliterations(text, proper_nouns(source_text, limit=30))
        problems = summary_problems(text, forbidden, source_text, known_years(source_text, article["published_at"]))
        if not problems:
            return text
        seen += [p for p in problems if p not in seen]
    raise ValueError("요약문 품질 검증 실패: " + "; ".join(seen))


_NOISE = re.compile(r"기자|@|사진\s*=|출처|무단\s*전재|저작권|[▶▲△■●]")


def extractive_summary(body: str, limit: int = 380) -> str:
    """Lead sentences of the article itself: crude but readable and grounded by construction."""
    out, total = [], 0
    for sent in (x.strip() for x in _SENT_SPLIT.split(body) if x.strip()):
        if len(sent) < 15 or _NOISE.search(sent) or not _SENT_END.search(sent):
            continue
        if out and total + len(sent) + 1 > limit:
            break
        out.append(sent)
        total += len(sent) + 1
        if len(out) == 3:
            break
    return " ".join(out)


NAMEFIX_SYSTEM = """너는 교정자다. 아래 요약문에서, 주어진 영어 이름이 한글로 잘못 음차되어 있으면(예: Claude → 클로이디) 영어 원문 그대로 고쳐 쓴다.
이름이 요약문에 아예 없으면 억지로 넣지 않는다. 그 밖의 내용은 한 글자도 바꾸지 않는다. 고친 요약문만 출력한다."""


def headline_names(article: dict) -> list[str]:
    """Names in the headline that the body confirms as names (title-case headlines make every word look capitalized)."""
    return [n for n in proper_nouns(article["body"], limit=40) if n in article["title"]]


def polish_names(llm, model: str, article: dict, summary: str) -> str:
    """English articles: when a headline name (Claude, Gemini...) is missing from the summary the model usually
    transliterated it. Ask for a minimal edit; keep the original unless the edit is clean."""
    source_text = article["title"] + " " + article["body"][:4000]
    missing = [n for n in headline_names(article) if n not in summary]
    if not missing or is_korean(source_text[:700]):
        return summary
    try:
        fixed = plain_form(_clean_summary(llm.chat(model, NAMEFIX_SYSTEM,
                                                   f"영어 이름: {', '.join(missing)}\n\n요약문:\n{summary}", max_tokens=700)))
    except llmmod.LLMError:
        return summary
    years = known_years(source_text, article["published_at"])
    ok = (not summary_problems(fixed, source_text=source_text, allowed_years=years)
          and any(n in fixed for n in missing) and len(fixed) >= 0.7 * len(summary))
    return fixed if ok else summary


def repair_fields(fields: dict, source_text: str, headline: str) -> dict:
    """Drop names that appear nowhere in the article (and a title containing them)."""
    probe = Extraction(True, victim=fields.get("victim", ""), attacker=fields.get("attacker", ""))
    bad = ungrounded_names(probe, source_text)
    if not bad:
        return dict(fields)
    out = dict(fields)
    for f in ("victim", "attacker", "damage"):
        if any(t in out.get(f, "") for t in bad):
            out[f] = ""
    if any(t in out.get("title", "") for t in bad):
        out["title"] = headline
    return out


STRONG_WORDS = ["유출", "랜섬웨어", "해킹", "침해사고", "탈취", "마비", "피해"]

EN_INCIDENT = re.compile(r"\b(hack(ed|er|ers|ing)?|breach(ed|es)?|ransomware|leak(ed|s|ing)?|stolen|steal(s|ing)?|theft|"
                         r"attack(s|ed|er|ers)?|cyber-?attack|compromis\w+|malware|phishing|ddos|extort\w*|infostealer|"
                         r"botnet|backdoor|exfiltrat\w+|heist|data exposure|exposed|"
                         # official breach notices: "Notice of Data Privacy Incident", "unauthorized access"
                         r"(data|privacy|security|cyber|cybersecurity|network) (privacy )?(incident|event)s?|"
                         r"unauthori[sz]ed (access|actor|part(y|ies)|third part(y|ies))|data breach notice)\b", re.I)
EN_VULN_TITLE = re.compile(r"\b(vulnerabilit\w*|CVE-\d+|patch(es|ed)?|flaw|bug|zero-days?|updates?|advisory)\b", re.I)
EN_STRONG_TITLE = re.compile(r"\b(breach(ed|es)?|ransomware|hack(ed)?|stolen|leak(ed|s)?|attacks?|attacked|"
                             r"compromis\w+|extort\w*|heist|infostealer|botnet|data (theft|exposure))\b", re.I)


def is_korean(text: str) -> bool:
    hangul = len(re.findall(r"[가-힣]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return hangul >= 0.2 * (hangul + latin) and hangul > 0


_NAME_TOKEN = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9-]*|[A-Z][a-z]+(?:[A-Z][A-Za-z0-9]*)+|[A-Za-z]+\d[A-Za-z0-9-]*)\b")
_MID_CAP = re.compile(r"(?<=[a-z,] )[A-Z][a-z]{2,}\b")
_NOT_NAMES = {"January", "February", "March", "April", "June", "July", "August", "September", "October", "November",
              "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "However",
              "According", "Because", "Although", "Meanwhile", "Researchers", "Attackers", "Hackers"}


def proper_nouns(text: str, limit: int = 14) -> list[str]:
    """Company/product/malware names as spelled in an English article (CamelCase, acronyms, Capitalized mid-sentence)."""
    found = _NAME_TOKEN.findall(text) + _MID_CAP.findall(text)
    found += [part for tok in found if "-" in tok for part in tok.split("-") if re.fullmatch(r"[A-Z]{2,}[A-Za-z0-9]*", part)]
    counts = Counter(m for m in found if m not in _NOT_NAMES)
    return [n for n, _ in counts.most_common(limit)]


def names_hint(article_text: str) -> str:
    """Prompt suffix for non-Korean articles: small models transliterate names ("Claude" -> "클로이데") unless told not to."""
    if is_korean(article_text[:700]):
        return ""
    names = proper_nouns(article_text)
    return ("\n\n다음 이름은 한글로 음차하지 말고 이 영어 표기 그대로 써라: " + ", ".join(names)) if names else ""


def model_for(cfg, korean: bool) -> str:
    """Model for an article: see llm.model_for (claude: claude_model; local: model / foreign_model)."""
    return llmmod.model_for(cfg, korean)


def looks_like_incident(article: dict) -> bool:
    """Cheap keyword gate before the LLM (Korean or English). The LLM still makes the final call."""
    text = article["title"] + " " + article["body"][:1500]
    if not is_korean(article["title"] + " " + article["body"][:600]):
        if not EN_INCIDENT.search(text):
            return False
        # vulnerability / patch advisories are not incidents unless the headline reports actual attacks
        return not (EN_VULN_TITLE.search(article["title"]) and not EN_STRONG_TITLE.search(article["title"]))
    if not any(w in text for w in INCIDENT_WORDS):
        return False
    # vulnerability / risk advisories are not incidents unless the title reports actual damage
    return not (re.search(r"취약|패치|가능성|우려|위험", article["title"]) and not any(w in article["title"] for w in STRONG_WORDS))


def extract_heuristic(article: dict) -> Extraction:
    """Korean-only fallback: it quotes the article, so it must never be used for non-Korean text."""
    if not looks_like_incident(article):
        return Extraction(False, method="heuristic")
    sentences = re.split(r"(?<=[.다])\s+", article["body"])
    summary = " ".join(sentences[:3])[:400] or article["title"]
    return Extraction(True, article["title"], article["published_at"], summary=summary,
                      category="기타", method="heuristic")


def extract(llm, cfg, article: dict) -> Extraction:
    """llm: the backend for cfg (llm.from_config); unused when the LLM is disabled."""
    # cheap pre-filter: no incident vocabulary at all -> not worth an LLM call
    if not looks_like_incident(article):
        return Extraction(False, method="prefilter")
    korean = is_korean(article["title"] + " " + article["body"][:600])
    if not korean and not cfg.llm_enabled:
        raise RuntimeError("LLM이 꺼져 있어 영어 기사를 한국어로 요약할 수 없습니다")
    if cfg.llm_enabled:
        try:
            model = model_for(cfg, korean)
            ex = extract_llm(llm, model, article)
            src = article["title"] + " " + article["body"][:4000]
            if ex.is_incident and summary_problems(ex.summary, source_text=src,
                                                   allowed_years=known_years(src, article["published_at"])):  # cramped JSON summary: write it properly
                try:
                    ex.summary = write_summary(llm, model, article,
                                               {"victim": ex.victim, "attacker": ex.attacker, "damage": ex.damage})
                except Exception:
                    if not korean:  # nothing to fall back on for English text: fail and retry next cycle
                        raise
                    # Korean article: the article's own lead beats a broken summary; else keep the terse JSON one
                    lead = extractive_summary(article["body"])
                    if lead and not summary_problems(lead):
                        ex.summary = lead
            if ex.is_incident and not korean:
                ex.summary = polish_names(llm, model, article, ex.summary)
            return ex
        except Exception:
            if not korean:  # the heuristic would store English text: fail so the article is retried later
                raise
    return extract_heuristic(article)
