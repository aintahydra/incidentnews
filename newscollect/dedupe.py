"""Match an extracted incident to an existing one (±7 days)."""
import json
import re
import sqlite3
from datetime import date, timedelta

WINDOW_DAYS = 7
CROSS_LANG_DAYS = 7  # LLM judge window for Korean-vs-English victim names (the judge guards against false merges)
TITLE_THRESHOLD = 0.5
_SUFFIX = re.compile(r"(주식회사|\(주\)|㈜|inc\.?|corp\.?|co\.?,? ?ltd\.?|ltd\.?|그룹|group)", re.I)
_GENERIC = {"", "불명", "미상", "알수없음", "정보없음", "없음"}


def norm(name: str) -> str:
    return re.sub(r"[\s\W_]+", "", _SUFFIX.sub("", name or "").lower())


def bigrams(s: str) -> set[str]:
    s = re.sub(r"[\s\W_]+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def title_similarity(a: str, b: str) -> float:
    A, B = bigrams(a), bigrams(b)
    return len(A & B) / len(A | B) if A and B else 0.0


def same_party(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if a in _GENERIC or b in _GENERIC or min(len(a), len(b)) < 2:
        return False
    return a == b or a in b or b in a


def _script(name: str) -> str:
    return "ko" if re.search(r"[가-힣]", name or "") else ("en" if re.search(r"[A-Za-z]", name or "") else "")


JUDGE_SYSTEM = """두 보안 사고 기록이 같은 실제 사건에 대한 것인지 판단하라. 서로 다른 언어의 기사에서 나온 기록일 수 있다(예: 바이낸스 = Binance, 마이크로소프트 = Microsoft).
같은 사건으로 본다: 같은 피해 대상이 같은 시기에 겪은 같은 유형의 사건(예: 같은 거래소의 대규모 암호화폐 탈취). 다음 차이는 같은 사건이라도 생기므로 무시한다:
- 날짜가 며칠 다른 것(기사 게시일이 달라서), 금액이 통화·단위가 달라 달라 보이는 것($6.3 billion ≈ 8조원), 요약의 초점이나 세부 내용이 다른 것.
다른 사건으로 본다: 피해 대상이 다르거나, 같은 회사라도 서로 다른 별개의 침해·공격일 때.
JSON 객체 {"same": true|false} 만 출력한다."""


def llm_judge(llm, model: str):
    """Returns judge(row, ex) -> bool, asking the LLM (an llm.Ollama / llm.Claude backend) whether two records
    describe one incident."""
    def card(title, victim, attacker, date_, summary):
        return f"제목: {title}\n날짜: {date_}\n피해 대상: {victim}\n공격자: {attacker}\n요약: {summary[:300]}"
    def judge(row, ex) -> bool:
        user = ("[기록 A]\n" + card(row["title"], row["victim"], row["attacker"], row["occurred_at"], row["summary"]) +
                "\n\n[기록 B]\n" + card(ex.title, ex.victim, ex.attacker, ex.occurred_at, ex.summary))
        try:
            m = re.search(r"\{.*\}", llm.chat(model, JUDGE_SYSTEM, user, json=True, max_tokens=60), re.S)
            return json.loads(m.group(0) if m else "{}").get("same") is True
        except Exception:
            return False  # when unsure, keep them separate
    return judge


def find_match(conn: sqlite3.Connection, ex, judge=None) -> int | None:
    d = date.fromisoformat(ex.occurred_at)
    lo, hi = (d - timedelta(days=WINDOW_DAYS)).isoformat(), (d + timedelta(days=WINDOW_DAYS)).isoformat()
    rows = conn.execute("SELECT * FROM incidents WHERE occurred_at BETWEEN ? AND ?", (lo, hi)).fetchall()
    best, best_score = None, 0.0
    for r in rows:
        if same_party(r["victim"], ex.victim):
            if not (norm(r["attacker"]) and norm(ex.attacker)) or same_party(r["attacker"], ex.attacker):
                return r["id"]
        score = title_similarity(r["title"], ex.title)
        if score >= TITLE_THRESHOLD and score > best_score:
            best, best_score = r["id"], score
    if best is not None or judge is None:
        return best
    # Korean "바이낸스" vs English "Binance" can't match by name: ask the LLM, but only for close dates
    # and only when the two victim names are in different scripts.
    cands = [r for r in rows if {_script(r["victim"]), _script(ex.victim)} == {"ko", "en"}
             and abs((date.fromisoformat(r["occurred_at"]) - d).days) <= CROSS_LANG_DAYS]
    cands.sort(key=lambda r: title_similarity(r["title"], ex.title), reverse=True)
    for r in cands[:2]:
        if judge(r, ex):
            return r["id"]
    return None


def upsert_incident(conn: sqlite3.Connection, ex, article_id: int, judge=None) -> int:
    inc_id = find_match(conn, ex, judge)
    if inc_id is None:
        cur = conn.execute(
            "INSERT INTO incidents(title,occurred_at,attacker,victim,damage,category,summary,hashtags) VALUES(?,?,?,?,?,?,?,?)",
            (ex.title, ex.occurred_at, ex.attacker, ex.victim, ex.damage, ex.category, ex.summary,
             json.dumps(getattr(ex, "hashtags", []), ensure_ascii=False)))
        inc_id = cur.lastrowid
    else:
        cur_row = conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        upd = {k: getattr(ex, k) for k in ("attacker", "victim", "damage", "category") if not cur_row[k] and getattr(ex, k)}
        if len(ex.summary) > len(cur_row["summary"]):
            upd["summary"] = ex.summary
        if getattr(ex, "hashtags", None) and cur_row["hashtags"] in ("", "[]"):  # first article with tags wins
            upd["hashtags"] = json.dumps(ex.hashtags, ensure_ascii=False)
        if upd:
            sets = ",".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE incidents SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*upd.values(), inc_id))
    conn.execute("INSERT OR IGNORE INTO incident_articles VALUES(?,?)", (inc_id, article_id))
    return inc_id
