"""Tag counts for the tag cloud: each incident's tags (attacker, victim, vulnerable system, technique, incident type),
over the incidents published in a period."""
import json
from collections import Counter
from datetime import date, timedelta

PERIODS = {"day": 1, "week": 7, "month": 30, "year": 365}  # days, today included


def since(period: str, today: date | None = None) -> str:
    return ((today or date.today()) - timedelta(days=PERIODS[period] - 1)).isoformat()


def _tags(value) -> list[str]:
    if isinstance(value, list):
        return [t for t in value if isinstance(t, str)]
    try:
        tags = json.loads(value or "[]")
    except ValueError:
        return []
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def cloud(rows, limit: int = 60, exclude=()) -> list[dict]:
    """rows: incidents with a "hashtags" JSON list. Each tag counts once per incident; spellings that differ only in
    case are merged under the most common one. exclude: whole tags left out ("해킹" drops the tag 해킹, not 금융권해킹)."""
    skip = {e.strip().lower() for e in exclude or () if e and e.strip()}
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for r in rows:
        seen = set()
        for t in _tags(r["hashtags"]):
            key = t.strip().lower()
            if not key or key in skip or key in seen:
                continue
            seen.add(key)
            counts[key] += 1
            spellings.setdefault(key, Counter())[t.strip()] += 1
    return [{"tag": spellings[k].most_common(1)[0][0], "count": c} for k, c in counts.most_common(limit)]
