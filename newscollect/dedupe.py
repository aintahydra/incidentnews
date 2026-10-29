"""Match an extracted incident to an existing one (±7 days)."""
import re
import sqlite3
from datetime import date, timedelta

WINDOW_DAYS = 7
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


def find_match(conn: sqlite3.Connection, ex) -> int | None:
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
    return best


def upsert_incident(conn: sqlite3.Connection, ex, article_id: int) -> int:
    inc_id = find_match(conn, ex)
    if inc_id is None:
        cur = conn.execute(
            "INSERT INTO incidents(title,occurred_at,attacker,victim,damage,category,summary) VALUES(?,?,?,?,?,?,?)",
            (ex.title, ex.occurred_at, ex.attacker, ex.victim, ex.damage, ex.category, ex.summary))
        inc_id = cur.lastrowid
    else:
        cur_row = conn.execute("SELECT * FROM incidents WHERE id=?", (inc_id,)).fetchone()
        upd = {k: getattr(ex, k) for k in ("attacker", "victim", "damage", "category") if not cur_row[k] and getattr(ex, k)}
        if len(ex.summary) > len(cur_row["summary"]):
            upd["summary"] = ex.summary
        if upd:
            sets = ",".join(f"{k}=?" for k in upd)
            conn.execute(f"UPDATE incidents SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?", (*upd.values(), inc_id))
    conn.execute("INSERT OR IGNORE INTO incident_articles VALUES(?,?)", (inc_id, article_id))
    return inc_id
