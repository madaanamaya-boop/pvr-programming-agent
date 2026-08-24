"""Step 3 — the occupancy signal.

Open a session's seat-selection page; BMS fetches the seat layout, which marks
every seat's status. We count booked vs total per price category to compute
occupancy % and an estimated realized revenue.

Seat status conventions on BMS seat-layout payloads:
  - available  : status in {"A", "0", "AVL"}
  - booked/sold: status in {"S", "B", "1", "SOLD"}   -> counts as occupied
  - blocked    : status in {"R", "N", "BMS_BLOCKED"} -> excluded from total
"""
from __future__ import annotations

import time

from src.crawler.capture import ResponseCollector
from src.logger import get_logger

log = get_logger("seatmap")

_AVAILABLE = {"a", "0", "avl", "available"}
_BOOKED = {"s", "b", "1", "sold", "booked"}


def _looks_like_seatmap(url: str, body) -> bool:
    text = str(body)[:6000].lower()
    return "seat" in text and ("seatstatus" in text or "seatlayout" in text or "rows" in text or "seats" in text)


def fetch_occupancy(ctx, session: dict, price_lookup: dict[str, float]) -> dict | None:
    """Return occupancy dict for a session, or None if the seat map wasn't captured.

    session must include a 'seat_url' (the buytickets deep link) OR we derive it.
    price_lookup: {category_name_lower: price} for revenue estimate.
    """
    page = ctx.new_page()
    collector = ResponseCollector(page, _looks_like_seatmap)
    url = session.get("seat_url")
    if not url:
        page.close()
        return None
    try:
        page.goto(url, wait_until="networkidle")
        time.sleep(2.0)
    except Exception as exc:
        log.warning("seatmap nav failed for session %s: %s", session["id"], exc)

    total = booked = 0
    revenue = 0.0
    for hit in collector.hits:
        t, b, rev = _count_seats(hit["body"], price_lookup)
        if t > total:  # keep the richest capture
            total, booked, revenue = t, b, rev
    collector.detach()
    page.close()

    if total == 0:
        return None
    occ = round(100.0 * booked / total, 1)
    return {
        "session_id": session["id"],
        "seats_total": total,
        "seats_booked": booked,
        "occupancy_pct": occ,
        "est_revenue": round(revenue, 2),
    }


def _count_seats(body, price_lookup: dict[str, float]):
    total = booked = 0
    revenue = 0.0
    for seat in _seat_nodes(body):
        status = str(
            seat.get("SeatStatus") or seat.get("status") or seat.get("Status") or ""
        ).strip().lower()
        cat = str(
            seat.get("PriceDesc") or seat.get("category") or seat.get("SeatCategory") or ""
        ).strip().lower()
        price = price_lookup.get(cat)
        if price is None and price_lookup:
            price = next(iter(price_lookup.values()))
        is_avl = status in _AVAILABLE
        is_booked = status in _BOOKED
        if not (is_avl or is_booked):
            continue  # blocked/aisle/not-a-seat
        total += 1
        if is_booked:
            booked += 1
            revenue += price or 0.0
    return total, booked, revenue


def _seat_nodes(obj):
    """Yield individual seat objects from any seat-layout payload shape."""
    if isinstance(obj, dict):
        keys = set(obj.keys())
        # A seat object has a status plus a seat number/id.
        if ({"SeatStatus", "status", "Status"} & keys) and (
            {"SeatNo", "seatNumber", "SeatNumber", "seatId", "SeatId"} & keys
        ):
            yield obj
        for v in obj.values():
            yield from _seat_nodes(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _seat_nodes(item)
