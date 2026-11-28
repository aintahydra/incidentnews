"""Give a 보안 사고 card the 정부기관 / 공공기관 tag by hand, which puts it in the 정부공공 tab:

    python -m newscollect gov 123                    # card #123 (the number in /incidents/123) -> 정부기관
    python -m newscollect gov https://news...        # the card that holds this article -> 정부기관
    python -m newscollect gov --public 123 456       # -> 공공기관
    python -m newscollect gov --remove 123           # take the hand-given tags off again
    python -m newscollect gov --list                 # cards tagged by hand

(or ./govtag ...). Tags normally come from the victim name (orgs.py); hand-given ones are stored in
incidents.manual_org_tags and shown in addition to those. --remove only removes hand-given tags: a tag that comes
from the victim name stays. The running server shows the change on its next /api/version poll.
"""
import argparse
import json

from . import db, orgs, pages


def find_card(conn, target: str):
    """(incident id, None) for a card number or an article URL, else (None, reason)."""
    if target.isdigit():
        row = conn.execute("SELECT id FROM incidents WHERE id=?", (int(target),)).fetchone()
        return (row[0], None) if row else (None, f"보안 사고 #{target}이(가) 없습니다")
    if not target.startswith(("http://", "https://")):
        return None, "카드 번호(/incidents/123의 123) 또는 기사 주소를 주세요"
    want = pages.norm_url(target)
    aid = next((r["id"] for r in conn.execute("SELECT id, url FROM articles") if pages.norm_url(r["url"]) == want), None)
    if aid is None:
        return None, "저장된 기사가 아닙니다 (먼저 ./addnews 로 추가하세요)"
    row = conn.execute("SELECT incident_id FROM incident_articles WHERE article_id=?", (aid,)).fetchone()
    if row:
        return row[0], None
    return None, "이 기사는 보안 사고 카드에 들어 있지 않습니다 (인텔·권고이거나 처리되지 않음)"


def manual_tags(raw) -> list[str]:
    try:
        tags = json.loads(raw or "[]")
    except ValueError:
        return []
    return [t for t in (orgs.GOV, orgs.PUBLIC) if isinstance(tags, list) and t in tags]


def set_tag(conn, inc_id: int, tag: str | None) -> list[str]:
    """Add `tag` to the card's hand-given tags, or clear them all when tag is None. Returns the new list."""
    cur = manual_tags(conn.execute("SELECT manual_org_tags FROM incidents WHERE id=?", (inc_id,)).fetchone()[0])
    new = [] if tag is None else [t for t in (orgs.GOV, orgs.PUBLIC) if t in cur or t == tag]
    if new != cur:
        conn.execute("UPDATE incidents SET manual_org_tags=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (json.dumps(new, ensure_ascii=False), inc_id))
        conn.commit()
    return new


def _line(conn, classifier, inc_id: int) -> str:
    r = conn.execute("SELECT id, title, victim, manual_org_tags FROM incidents WHERE id=?", (inc_id,)).fetchone()
    auto, manual = classifier.tags(r["victim"]), manual_tags(r["manual_org_tags"])
    shown = [t for t in (orgs.GOV, orgs.PUBLIC) if t in auto or t in manual]
    marks = " ".join(t + ("(수동)" if t in manual and t not in auto else "") for t in shown) or "태그 없음"
    return f"#{r['id']} {r['title']} (피해 대상: {r['victim'] or '-'})\n  → {marks}"


def main(argv: list[str], cfg) -> int:
    ap = argparse.ArgumentParser(prog="python -m newscollect gov", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", metavar="CARD", help="card number (/incidents/123 -> 123) or article URL")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--public", action="store_true", help="공공기관 instead of 정부기관")
    g.add_argument("--remove", action="store_true", help="remove the hand-given tags")
    g.add_argument("--list", action="store_true", help="list cards tagged by hand")
    args = ap.parse_args(argv)
    if not args.list and not args.targets:
        ap.error("카드 번호나 기사 주소를 하나 이상 주세요 (또는 --list)")
    conn = db.connect(cfg.database)
    classifier = orgs.Classifier(cfg.gov_list, cfg.public_list)
    if args.list:
        rows = conn.execute("SELECT id FROM incidents WHERE manual_org_tags NOT IN ('', '[]') ORDER BY id").fetchall()
        for r in rows:
            print(_line(conn, classifier, r[0]))
        if not rows:
            print("수동으로 태그를 붙인 카드가 없습니다")
        return 0
    tag = None if args.remove else (orgs.PUBLIC if args.public else orgs.GOV)
    failed = False
    for t in args.targets:
        inc_id, why = find_card(conn, t)
        if inc_id is None:
            print(f"{t}\n  ✗ {why}")
            failed = True
            continue
        set_tag(conn, inc_id, tag)
        print(_line(conn, classifier, inc_id))
    return 1 if failed else 0
