"""Intel & advisories ([[intel_sources]]): one Korean entry per article, no incident gate and no merging.

The LLM decides relevance (webinar promos, hiring and marketing posts are dropped), the kind, a Korean title,
a 3-4 sentence summary in 다체, a one-line recommended action, and up to four tags."""
import json
import re
from dataclasses import dataclass, field

from . import extractor, llm as llmmod

KINDS = ("권고", "위협 분석", "가이드", "정책")
# feed items that are never worth a summary: SANS ISC's daily podcast stubs (a title and a link, ~120 chars)
SKIP_TITLE = re.compile(r"^\s*ISC Stormcast\b", re.I)
TAG_ROLES = ("actor", "product", "technique", "topic")

SYSTEM = """너는 사이버 보안 위협 인텔리전스와 보안 권고문을 한국어로 정리하는 분석가다. 반드시 JSON 객체 하나만 출력한다.
기사는 영어이거나 한국어일 수 있다. 모든 문자열 값은 한국어(한글)로 쓴다. (Write all values in Korean.)
단, 기업·기관·공격 그룹·제품·악성코드 이름과 CVE 번호는 번역하거나 음차하지 말고 기사에 나온 원문 표기 그대로 쓴다.
필드:
- relevant: boolean. 보안 위협·취약점·권고·위협 분석·보안 수칙·보안 정책을 다루는 글이면 true. 행사·웨비나 홍보, 채용, 수상, 회사 소식, 제품 광고는 false.
- kind: 다음 중 하나.
  "권고": 특정 취약점·공격에 대한 공식 권고, 패치·완화 조치 안내
  "위협 분석": 공격 그룹·캠페인·악성코드·취약점 동향에 대한 분석과 연구
  "가이드": 보안 수칙, 모범 사례, 사용자를 위한 팁
  "정책": 법·제도, 정부 전략·발표, 규제
- title: 한국어 제목(50자 이내)
- summary: 한국어 3~4문장. 무엇에 관한 글인지, 핵심 내용(영향받는 대상, 위협, 근거)을 담는다. 기사에 없는 내용은 쓰지 않는다.
  모든 문장은 '~했다.', '~이다.'처럼 '다.'로 끝나는 평서문으로 쓰고, 존댓말('~습니다', '~입니다')은 쓰지 않는다. 목록·기호를 쓰지 않는다.
- action: 독자가 할 일을 한 문장으로(예: "Fortinet FortiOS를 7.4.5 이상으로 업데이트한다."). 글에 권고 조치가 없으면 "".
- tags: 다음 네 칸을 가진 객체. 각 칸에 태그 하나(짧은 명사구, '#' 없이, 띄어쓰기 없이). 글에서 알 수 없으면 ""로 비워 둔다.
  - actor: 위협 행위자(공격 그룹·악성코드 운영 조직 이름). 예: "APT29", "LockBit"
  - product: 영향받는 제품·서비스 또는 CVE 번호. 예: "FortiOS", "CVE-2026-1234"
  - technique: 공격 기법. 예: "피싱", "공급망공격"
  - topic: 글의 주제를 추상화한 말. 예: "랜섬웨어", "AI보안", "패치관리", "중요기반시설"
  이름은 원문 표기 그대로, 나머지는 한국어로 쓴다."""


@dataclass
class IntelEntry:
    title: str
    kind: str
    summary: str
    action: str = ""
    hashtags: list[str] = field(default_factory=list)


def parse(content: str) -> tuple[bool, IntelEntry]:
    m = re.search(r"\{.*\}", content, re.S)
    data = json.loads(m.group(0) if m else content)
    s = extractor._s
    kind = s(data.get("kind")).replace(" ", "")
    kind = next((k for k in KINDS if k.replace(" ", "") == kind), "위협 분석")
    action = extractor.plain_form(s(data.get("action")))
    if action and not action.endswith((".", "다")):
        action += "."
    tags = data.get("tags")
    return bool(data.get("relevant", True)), IntelEntry(
        s(data.get("title")), kind, extractor.clip(extractor.plain_form(s(data.get("summary")))),
        action, extractor.clean_hashtags(tags, TAG_ROLES))


def problems(entry: IntelEntry, source_text: str, published_at: str) -> list[str]:
    out = []
    if not entry.title or not extractor.HANGUL.search(entry.title) or extractor._english_text(entry.title):
        out.append("title이 한국어가 아님")
    if entry.action and extractor._polite(entry.action):
        out.append("action에 존댓말 사용: '~한다'체로 써야 함")
    out += extractor.summary_problems(entry.summary, source_text=source_text,
                                      allowed_years=extractor.known_years(source_text, published_at))
    return out


def extract(llm, model: str, article: dict) -> IntelEntry | None:
    """The entry, or None when the article is not security content. Raises ValueError when no attempt passes the
    checks (the collector retries the article next cycle), llm.LLMUnavailable when the LLM can't be reached."""
    if SKIP_TITLE.search(article["title"]):
        return None
    body = article["body"][:5000]
    source_text = article["title"] + " " + body
    user = f"제목: {article['title']}\n게시일: {article['published_at']}\n본문:\n{body}" + extractor.names_hint(source_text)
    english = not extractor.is_korean(source_text[:700])
    found: list[str] = []
    hint = ""
    for temp in (0.0, *extractor.RETRY_TEMPERATURES):
        try:
            content = llm.chat(model, SYSTEM, user + hint, json=True, temperature=temp, max_tokens=1200)
            relevant, entry = parse(content)
        except (llmmod.LLMError, json.JSONDecodeError, KeyError, AttributeError) as e:
            bad = [f"LLM 응답 오류: {type(e).__name__}"]
        else:
            if not relevant:
                return None
            if english:
                names = extractor.proper_nouns(source_text, limit=30)
                entry.title, entry.summary, entry.action = (extractor.strip_transliterations(x, names)
                                                            for x in (entry.title, entry.summary, entry.action))
            bad = problems(entry, source_text, article["published_at"])
            if not bad:
                return entry
        found += [p for p in bad if p not in found]
        hint = f"\n\n이전 답변의 문제: {'; '.join(bad)}. 규칙을 지켜 다시 쓰라."
    raise ValueError("인텔 요약 검증 실패: " + "; ".join(found))


def save(conn, article_id: int, entry: IntelEntry, published_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO intel(article_id,title,kind,summary,action,hashtags,published_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(article_id) DO UPDATE SET title=excluded.title, kind=excluded.kind, summary=excluded.summary, "
        "action=excluded.action, hashtags=excluded.hashtags, updated_at=CURRENT_TIMESTAMP",
        (article_id, entry.title, entry.kind, entry.summary, entry.action,
         json.dumps(entry.hashtags, ensure_ascii=False), published_at))
    return cur.lastrowid
