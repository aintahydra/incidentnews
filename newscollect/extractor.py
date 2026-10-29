"""Incident extraction: LLM (Ollama) with a keyword-heuristic fallback."""
import json
import re
import time
from dataclasses import dataclass

import httpx

INCIDENT_WORDS = ["해킹", "랜섬웨어", "유출", "침해", "공격", "탈취", "감염", "피싱", "악성코드",
                  "디도스", "DDoS", "먹통", "사칭", "해커", "데이터 노출", "침투", "크리덴셜"]

SYSTEM = """너는 사이버 보안 뉴스에서 '사고'를 추출하는 분석가다. 반드시 JSON 객체 하나만 출력한다.
사고란 해킹, 랜섬웨어, 개인정보/데이터 유출, 침해, 디도스, 악성코드 감염 등으로 실제 피해(유출, 서비스 마비, 금전 요구 등)가 발생한 보안 사건이다.
다음은 사고가 아니므로 is_incident=false: 취약점 발견·권고·패치, 잠재적 위험 경고, 연구 결과, 정책·제도, 제품·행사 소개, 통계·설문, 의견 기사.
모든 문자열 값은 반드시 한국어(한글)로 작성한다. 영어로 번역하지 말고, 기업·그룹명은 원문 표기를 유지한다. (Write all values in Korean.)
필드:
- is_incident: boolean
- title: 사고를 한 줄로 요약한 제목(40자 이내, 한국어)
- occurred_at: 사고 발생일 YYYY-MM-DD (알 수 없으면 기사 게시일 사용)
- attacker: 공격자/공격 그룹 (모르면 "")
- victim: 피해 기업/기관 (모르면 "")
- damage: 피해 규모(건수, 금액, 영향 등) (모르면 "")
- category: 랜섬웨어|데이터 유출|해킹|악성코드|DDoS|피싱|기타 중 하나
- summary: 한국어 2~4문장. 언제, 누가 누구를 공격했는지, 피해 규모가 어느 정도인지 반드시 포함. 기사에 없는 내용은 지어내지 않는다."""


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


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


def clip(text: str, limit: int = 450) -> str:
    """Cap runaway summaries, cutting at a sentence boundary when possible."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("다. "), cut.rfind("."))
    return cut[:end + 1] if end > limit // 2 else cut.rstrip() + "…"


def parse_llm_json(text: str, published_at: str) -> Extraction:
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    occurred = _s(data.get("occurred_at"))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", occurred):
        occurred = published_at
    return Extraction(bool(data.get("is_incident")), _s(data.get("title")), occurred,
                      _s(data.get("attacker")), _s(data.get("victim")), _s(data.get("damage")),
                      _s(data.get("category")), clip(_s(data.get("summary"))))


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


def _problems(ex: Extraction, source_text: str) -> list[str]:
    if not ex.is_incident:
        return []
    problems = []
    if not (ex.summary and HANGUL.search(ex.summary)):
        problems.append("summary가 비었거나 한국어가 아님")
    bad = ungrounded_names(ex, source_text)
    if bad:
        problems.append("기사 본문에 없는 이름: " + ", ".join(bad))
    return problems


def extract_llm(client: httpx.Client, base_url: str, model: str, article: dict,
                temperature: float = 0, num_ctx: int = 8192, deadline: float | None = None) -> Extraction:
    body = article["body"][:4000]
    user = (f"게시일: {article['published_at']}\n제목: {article['title']}\n본문: {body}"
            "\n\nJSON만 출력하고, title과 summary는 반드시 한국어로 작성하라.")
    source_text = article["title"] + " " + body
    problems: list[str] = []
    seen: list[str] = []  # distinct problems across all attempts, for the final error
    for temp in (temperature, *RETRY_TEMPERATURES):
        if deadline is not None and time.monotonic() > deadline:
            seen.append("시간 초과")
            break
        hint = f"\n\n이전 답변의 문제: {'; '.join(problems)}. 기사에 나온 이름을 한 글자도 바꾸지 말고 그대로 사용하라." if problems else ""
        try:
            r = client.post(f"{base_url}/api/chat", json={
                "model": model, "stream": False, "format": "json",
                "options": {"temperature": max(temp, temperature), "num_ctx": num_ctx, "num_predict": 1000},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user + hint}],
            })
            r.raise_for_status()
            ex = parse_llm_json(r.json()["message"]["content"], article["published_at"])
        except (httpx.HTTPStatusError, json.JSONDecodeError, KeyError) as e:  # transient 5xx / bad JSON: retry
            problems = [f"LLM 응답 오류: {type(e).__name__}"]
            seen += [p for p in problems if p not in seen]
            continue
        problems = _problems(ex, source_text)
        if not problems:
            return ex
        seen += [p for p in problems if p not in seen]
    raise ValueError("LLM 결과 검증 실패(" + str(1 + len(RETRY_TEMPERATURES)) + "회 시도): " + "; ".join(seen))


# ---- dedicated summary step -------------------------------------------------
# Inside a JSON object a 10B model writes cramped notes ("유출 추정 항목: …", "~했음").
# Writing the summary as plain prose, with the extracted fields as scaffolding, reads far better.

SUMMARY_SYSTEM = """너는 보안 뉴스 편집자다. 주어진 기사로 사이버 보안 사고 요약문을 쓴다.
규칙:
- 3~4개의 완전한 문장으로 쓴다. 모든 문장은 '~했다.', '~됐다.', '~이다.'처럼 '다.'로 끝낸다. '~했음'이나 명사로 끝나는 메모체, 목록, '△', 괄호 메모는 쓰지 않는다.
- 첫 문장: 언제, 누가(공격자) 누구를(피해 대상) 어떻게 공격했는지. 공격자를 모르면 공격자는 언급하지 않는다.
- 두 번째 문장: 피해 규모(건수, 금액, 유출 항목, 서비스 영향).
- 마지막 문장: 회사·당국의 대응이나 후속 조치.
- 기사에 없는 내용은 쓰지 않는다. 기업·기관·그룹 이름은 기사에 나온 표기를 한 글자도 바꾸지 않는다.
- 같은 내용을 반복하지 않는다. 매체 이름, 기자, 출처 표기는 쓰지 않는다.
- 요약문 본문만 출력한다. 제목, 머리말, 따옴표, JSON을 붙이지 않는다.

형식 예시(내용이 아니라 문장 구조만 참고):
2026년 3월 2일 A사가 랜섬웨어 조직 B의 공격을 받아 주요 서버가 암호화됐다. 이 사고로 고객 12만 명의 이름과 연락처가 유출된 것으로 확인됐고, 서비스는 이틀간 중단됐다. A사는 외부 전문기관과 원인을 조사하고 있으며 피해 고객에게 개별 안내를 진행하고 있다."""

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_SENT_END = re.compile(r"다[.!?」”’\"')]*$")
_HANJA = re.compile(r"[\u4e00-\u9fff]")


def summary_problems(text: str, forbidden: list[str] = (), source_text: str | None = None) -> list[str]:
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
    broken = [x for x in sents if not _SENT_END.search(x)]
    if len(broken) > len(sents) // 5:
        problems.append("'~다.'로 끝나지 않는 문장이 있음(메모체)")
    keys = [_squash(x)[:15] for x in sents]
    if len(set(keys)) < len(keys):
        problems.append("같은 문장 반복")
    bad = [t for t in forbidden if t and t in text]
    if bad:
        problems.append("기사에 없는 이름: " + ", ".join(bad))
    if source_text is not None:  # English words the article never used = the model drifted into English
        src = source_text.lower()
        foreign = sorted({w for w in re.findall(r"[A-Za-z]{4,}", text) if w.lower() not in src})
        if foreign:
            problems.append("기사에 없는 영어 단어: " + ", ".join(foreign[:4]))
    return problems


def _clean_summary(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip().strip("\"'“”‘’")
    return re.sub(r"^(요약|요약문)\s*[:：]\s*", "", text)


def write_summary(client: httpx.Client, base_url: str, model: str, article: dict, fields: dict,
                  temperature: float = 0.2, num_ctx: int = 8192, deadline: float | None = None,
                  forbidden: list[str] = ()) -> str:
    """Plain-prose summary; retried (hotter, with the reasons as a hint) until it passes summary_problems."""
    body = article["body"][:4000]
    facts = [("발생일", article["published_at"]), ("피해 대상", fields.get("victim")),
             ("공격자", fields.get("attacker")), ("피해 규모", fields.get("damage"))]
    info = "\n".join(f"- {k}: {v}" for k, v in facts if v)
    user = (f"확인된 정보(참고용. 기사와 다르면 기사를 따른다):\n{info}\n\n"
            f"제목: {article['title']}\n본문: {body}\n\n위 기사의 요약문만 출력하라.")
    problems: list[str] = []
    seen: list[str] = []
    for temp in (temperature, *RETRY_TEMPERATURES):
        if deadline is not None and time.monotonic() > deadline:
            seen.append("시간 초과")
            break
        hint = f"\n\n이전 답변의 문제: {'; '.join(problems)}. 규칙을 지켜 다시 쓰라." if problems else ""
        try:
            r = client.post(f"{base_url}/api/chat", json={
                "model": model, "stream": False,
                "options": {"temperature": max(temp, temperature), "num_ctx": num_ctx, "num_predict": 700},
                "messages": [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user + hint}],
            })
            r.raise_for_status()
            text = _clean_summary(r.json()["message"]["content"])
        except (httpx.HTTPStatusError, KeyError) as e:
            problems = [f"LLM 응답 오류: {type(e).__name__}"]
            seen += [p for p in problems if p not in seen]
            continue
        problems = summary_problems(text, forbidden, article["title"] + " " + body)
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


def extract_heuristic(article: dict) -> Extraction:
    text = article["title"] + " " + article["body"][:1500]
    if not any(w in text for w in INCIDENT_WORDS):
        return Extraction(False, method="heuristic")
    # vulnerability / risk advisories are not incidents unless the title reports actual damage
    if re.search(r"취약|패치|가능성|우려|위험", article["title"]) and not any(w in article["title"] for w in STRONG_WORDS):
        return Extraction(False, method="heuristic")
    sentences = re.split(r"(?<=[.다])\s+", article["body"])
    summary = " ".join(sentences[:3])[:400] or article["title"]
    return Extraction(True, article["title"], article["published_at"], summary=summary,
                      category="기타", method="heuristic")


def extract(client, cfg, article: dict) -> Extraction:
    # cheap pre-filter: no incident vocabulary at all -> not worth an LLM call
    if not extract_heuristic(article).is_incident:
        return Extraction(False, method="prefilter")
    if cfg.llm_enabled:
        try:
            ex = extract_llm(client, cfg.llm_base_url, cfg.llm_model, article, num_ctx=cfg.llm_num_ctx)
            if ex.is_incident and summary_problems(ex.summary):  # cramped JSON summary: write it properly
                try:
                    ex.summary = write_summary(client, cfg.llm_base_url, cfg.llm_model, article,
                                               {"victim": ex.victim, "attacker": ex.attacker, "damage": ex.damage},
                                               num_ctx=cfg.llm_num_ctx)
                except Exception:
                    pass  # keep the JSON summary (Korean and grounded, just terse)
            return ex
        except Exception:
            pass
    return extract_heuristic(article)
