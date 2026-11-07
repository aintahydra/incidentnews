"""Collected-article counts per day / month / year (by collection time, local timezone), zero-filled."""
from datetime import date, timedelta

UNITS = {"day": 10, "month": 7, "year": 4}  # length of the local-time key prefix: YYYY-MM-DD / YYYY-MM / YYYY
DAYS_SHOWN = 30
MONTHS_SHOWN = 12


def periods(unit: str, today: date, first_year: int | None = None) -> list[str]:
    if unit == "day":
        return [(today - timedelta(days=n)).isoformat() for n in range(DAYS_SHOWN - 1, -1, -1)]
    if unit == "month":
        idx = today.year * 12 + today.month - 1
        return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(idx - MONTHS_SHOWN + 1, idx + 1)]
    return [str(y) for y in range(min(first_year or today.year, today.year), today.year + 1)]


def article_counts(conn, unit: str, today: date | None = None) -> list[dict]:
    today = today or date.today()
    n = UNITS[unit]
    rows = conn.execute(
        f"SELECT substr(datetime(fetched_at,'localtime'),1,{n}) k, COUNT(*) c FROM articles GROUP BY k").fetchall()
    counts = {r["k"]: r["c"] for r in rows if r["k"]}
    first_year = min((int(k[:4]) for k in counts), default=None)
    return [{"period": p, "count": counts.get(p, 0)} for p in periods(unit, today, first_year)]
