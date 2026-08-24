"""Rolling re-poll prioritization.

Full seat-map sweeps of all of Delhi-NCR every few minutes would get us blocked
and isn't necessary. This ranks which sessions to re-poll on each refresh pass so
limited request budget captures the shows that are actually moving: shows
starting soon, and shows already filling fast.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src import config


def _mins_since(iso_ts: str | None, now: datetime) -> float:
    if not iso_ts:
        return 1e9
    try:
        return (now - datetime.fromisoformat(iso_ts)).total_seconds() / 60.0
    except Exception:
        return 1e9


def refresh_priority(session_row, now: datetime) -> float:
    """Higher = poll sooner. Returns -inf if it's not due yet."""
    show_dt = _show_datetime(session_row["show_date"], session_row["show_time"])
    hours_to_show = (show_dt - now).total_seconds() / 3600.0 if show_dt else 999

    # Past shows: occupancy is final, poll once then leave alone.
    since = _mins_since(session_row["last_crawl"], now)
    imminent = 0 <= hours_to_show <= config.IMMINENT_WINDOW_HOURS
    high_occ = (session_row["last_occ"] or 0) >= 60

    interval = config.REFRESH_HIGH_MIN if (imminent or high_occ) else config.REFRESH_LOW_MIN
    if since < interval:
        return float("-inf")  # not due

    score = since  # staleness baseline
    if imminent:
        score += 1000
    if high_occ:
        score += 500
    if hours_to_show < 0:
        score -= 2000  # already started; deprioritize
    return score


def due_sessions(session_rows, now: datetime, budget: int):
    ranked = sorted(
        ((refresh_priority(r, now), r) for r in session_rows),
        key=lambda x: x[0],
        reverse=True,
    )
    return [r for score, r in ranked if score != float("-inf")][:budget]


def _show_datetime(show_date: str, show_time: str | None):
    try:
        hh, mm = (show_time or "00:00").split(":")
        return datetime.fromisoformat(show_date) + timedelta(hours=int(hh), minutes=int(mm))
    except Exception:
        return None
