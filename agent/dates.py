from __future__ import annotations

import datetime as dt


def parse_email_date(value: str) -> dt.date:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value[:19] if "T" in value else value, fmt).date()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as e:
        raise ValueError(f"unrecognised email date {value!r}") from e


def is_business_day(d: dt.date, holidays: set[str]) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays


def add_business_days(start: dt.date, n: int, holidays: set[str]) -> dt.date:
    d, added = start, 0
    while added < n:
        d += dt.timedelta(days=1)
        if is_business_day(d, holidays):
            added += 1
    return d


def business_days_between(start: dt.date, end: dt.date, holidays: set[str]) -> int:
    """Business days after start up to and including end (negative if end is before start)."""
    if end < start:
        return -1
    d, n = start, 0
    while d < end:
        d += dt.timedelta(days=1)
        if is_business_day(d, holidays):
            n += 1
    return n
