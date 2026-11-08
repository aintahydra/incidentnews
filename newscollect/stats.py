"""Collected-article counts per day / month / year by each article's published date, zero-filled.

published_at is the feed's YYYY-MM-DD date; an article without one (older rows, odd feeds) falls back to the local
date it was collected. Counting by collection time instead would pile a backlog fetched in one run onto that day."""
from datetime import date, timedelta

UNITS = {"day": 10, "month": 7, "year": 4}  # length of the date key prefix: YYYY-MM-DD / YYYY-MM / YYYY
DATE = "COALESCE(NULLIF(published_at, ''), date(fetched_at, 'localtime'))"  # fetched_at is UTC
DAYS_SHOWN = 30  # default daily span; the web app passes [collector].max_age_days
MONTHS_SHOWN = 12


def periods(unit: str, today: date, first_year: int | None = None, days: int = DAYS_SHOWN) -> list[str]:
    if unit == "day":
        return [(today - timedelta(days=n)).isoformat() for n in range(max(1, days) - 1, -1, -1)]
    if unit == "month":
        idx = today.year * 12 + today.month - 1
        return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(idx - MONTHS_SHOWN + 1, idx + 1)]
    return [str(y) for y in range(min(first_year or today.year, today.year), today.year + 1)]


def article_counts(conn, unit: str, today: date | None = None, days: int = DAYS_SHOWN) -> list[dict]:
    today = today or date.today()
    n = UNITS[unit]
    rows = conn.execute(
        f"SELECT substr({DATE},1,{n}) k, COUNT(*) c FROM articles GROUP BY k").fetchall()
    counts = {r["k"]: r["c"] for r in rows if r["k"] and r["k"][:4].isdigit()}
    first_year = min((int(k[:4]) for k in counts), default=None)
    return [{"period": p, "count": counts.get(p, 0)} for p in periods(unit, today, first_year, days)]


def average(series: list[dict]) -> dict | None:
    """Mean count per period over the stretch that has data: from the first non-zero period, leaving out the last
    (current, still incomplete) period unless it is the only one. Dividing by every zero-filled period before
    collection began made the average look many times smaller than the real rate."""
    first = next((i for i, x in enumerate(series) if x["count"]), None)
    if first is None:
        return None
    end = len(series) - 1
    if end > first:
        end -= 1
    span = series[first:end + 1]
    return {"value": sum(x["count"] for x in span) / len(span), "from": span[0]["period"], "to": span[-1]["period"],
            "periods": len(span)}
