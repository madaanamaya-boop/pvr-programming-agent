"""Step 2 — for one movie, extract every cinema + session in Delhi-NCR.

BookMyShow renders the buytickets page server-side: the whole venue/showtime
tree is embedded in the HTML as a Next.js flight payload, not fetched via XHR.
So we load the page, take the rendered HTML, and pull out the objects by their
known shapes:

  venue block :  "venueCode":"PVVW","seatLegendIds":[...],
                 "favouriteActionState":"...","venueName":"PVR: Vegas Dwarka"
  session pill:  "sessionId":"60028","availStatus":"3","cutOffDateTime":"...",
                 ...,"showDateTime":"202608221345","showTime":"01:45 PM"

Sessions are associated to the venue that immediately precedes them in document
order (validated: 134 venues / 1205 sessions for one movie, cleanly bucketed).

One page load yields EVERY venue and show for the movie, each with a coarse
`availStatus` fill bucket — the cheap, high-coverage occupancy signal. Exact
seat counts are not on this page (they live behind the seat-map, see seatmap.py).
"""
from __future__ import annotations

import bisect
import re

from src import config
from src.logger import get_logger

log = get_logger("showtimes")

_VENUE_RE = re.compile(
    r'"venueCode":"([^"]+)","seatLegendIds":\[[^\]]*\],'
    r'"favouriteActionState":"[^"]*","venueName":"([^"]+)"'
)
_SESSION_RE = re.compile(
    r'"sessionId":"(\d+)","availStatus":"(\d+)","cutOffDateTime":"[^"]*",'
    r'"cutOffDateTimeEpoch":"[^"]*","showDateCode":"(\d+)","showDateTime":"(\d+)",'
    r'"showTimeCode":"(\d+)","showTime":"([^"]+)"'
)


def fetch_movie_showtimes(crawler, movie: dict, show_date: str) -> dict:
    """Return {cinemas: [...], sessions: [...]} for a movie on show_date (YYYY-MM-DD).

    Uses a fresh browser context (crawler.fetch_html) to dodge BMS's per-session
    second-navigation block.
    """
    ymd = show_date.replace("-", "")
    url = (f"{config.BMS_BASE}/movies/{config.CITY_SLUG}/{movie.get('slug','movie')}"
           f"/buytickets/{movie['id']}/{ymd}")
    log.info("Showtimes: %s", url)
    html = crawler.fetch_html(url, marker="venueName")
    return parse_showtimes_html(html, movie, show_date)


def parse_showtimes_html(html: str, movie: dict, show_date: str) -> dict:
    """Pure parser — separated so it can be unit-tested against saved HTML."""
    venues = [(m.start(), m.group(1), m.group(2)) for m in _VENUE_RE.finditer(html)]
    vpos = [v[0] for v in venues]

    cinemas: dict[str, dict] = {}
    for _, code, name in venues:
        cinemas.setdefault(code, {
            "id": code,
            "name": name,
            "chain": config.classify_chain(name),
            "area": _area_from_name(name),
        })

    sessions: list[dict] = []
    for m in _SESSION_RE.finditer(html):
        sid, avail, date_code, dt, tcode, show_time = m.groups()
        idx = bisect.bisect_right(vpos, m.start()) - 1
        if idx < 0:
            continue
        cinema_id = venues[idx][1]
        sessions.append({
            "id": sid,
            "cinema_id": cinema_id,
            "movie_id": movie["id"],
            "show_date": show_date,
            "show_time": _hhmm(dt) or _norm_ampm(show_time),
            "screen": None,
            "avail_status": avail,
            "price_tiers": [],  # exact prices only available on the seat page
        })

    log.info("  %s: %d venues, %d sessions", movie["title"], len(cinemas), len(sessions))
    return {"cinemas": list(cinemas.values()), "sessions": sessions}


def _area_from_name(name: str) -> str | None:
    # "PVR: Vegas Dwarka" / "Cinepolis: DLF Avenue, Saket" -> last comma segment or after colon.
    tail = name.split(":", 1)[-1].strip()
    if "," in tail:
        return tail.rsplit(",", 1)[-1].strip()
    return tail or None


def _hhmm(show_datetime: str) -> str | None:
    # showDateTime like 202608221345 -> "13:45"
    if show_datetime and len(show_datetime) >= 12:
        return f"{show_datetime[8:10]}:{show_datetime[10:12]}"
    return None


def _norm_ampm(raw: str) -> str | None:
    m = re.match(r"(\d{1,2}):(\d{2})\s*([AP]M)", raw or "", re.I)
    if not m:
        return raw
    h, mm, ap = int(m.group(1)), m.group(2), m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mm}"
