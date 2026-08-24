"""Cinepolis crawler — REAL occupancy via Cinepolis's own booking API.

Unlike BookMyShow (which encrypts seat data), cinepolisindia.com exposes a clean
JSON backend at api_new.cinepolisindia.com. One call returns every session with
`SeatsAvailable` — the actual number of unsold seats — plus screen name, format
and showtime. We derive each screen's capacity as the maximum SeatsAvailable
ever seen for it (a near-empty show reveals the true seat count), then:

    admissions = capacity - SeatsAvailable
    occupancy% = admissions / capacity

These are REAL numbers, not fill-bucket proxies.

Endpoints (all GET, fetched from a loaded page context to satisfy CORS/anti-bot):
  /api/movies/cities                          -> city_id / city_name
  /api/cinemas/cinemas/?city_id=9             -> all cinemas (filter to NCR)
  /api/movies/now-playing/?city_id=9          -> film ID -> title
  /api/booking/get-sessions/?city_id=9        -> ALL sessions w/ SeatsAvailable
"""
from __future__ import annotations

import collections
import json

from src.logger import get_logger

log = get_logger("cinepolis")

API = "https://api_new.cinepolisindia.com"
HOME = "https://cinepolisindia.com/"

# Delhi-NCR city names as they appear in Cinepolis's cities feed.
NCR_CITIES = {"delhi", "noida", "greater noida", "gurugram", "gurgaon",
              "faridabad", "ghaziabad"}


def _fetch_json(page, url, retries=3):
    for i in range(retries):
        try:
            txt = page.evaluate(
                """async(u)=>{const r=await fetch(u,{headers:{'Accept':'application/json'}});return await r.text()}""",
                url)
            return json.loads(txt)
        except Exception:
            if i == retries - 1:
                raise
            page.wait_for_timeout(2000)


def _first_list(o):
    if isinstance(o, dict):
        for v in o.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            r = _first_list(v)
            if r:
                return r
    return None


def crawl_cinepolis(crawler, show_date: str) -> dict:
    """Return {cinemas, movies, sessions} for Cinepolis in Delhi-NCR on show_date.

    Sessions carry real occupancy (occupancy_pct, seats_total, seats_booked).
    `crawler` is our Crawler; we use one of its contexts to host page-context fetches.
    """
    with crawler._context() as ctx:  # noqa: SLF001 — internal use is fine here
        page = ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        cinemas_raw = _first_list(_fetch_json(page, f"{API}/api/cinemas/cinemas/?city_id=9")) or []
        movies_raw = _first_list(_fetch_json(page, f"{API}/api/movies/now-playing/?city_id=9")) or []
        sessions_raw = (_fetch_json(page, f"{API}/api/booking/get-sessions/?city_id=9") or {}).get("value", [])
        page.close()

    # NCR cinema set.
    ncr = {}
    for c in cinemas_raw:
        cname = (c.get("city_name") or "").strip().lower()
        if cname in NCR_CITIES:
            ncr[c["ID"]] = {
                "id": f"CINE-{c['ID']}",
                "raw_id": c["ID"],
                "name": _clean(c.get("Name")),
                "chain": "Cinepolis",
                "area": c.get("city_name"),
                "lat": _num(c.get("Latitude")), "lng": _num(c.get("Longitude")),
            }
    log.info("Cinepolis NCR cinemas: %d", len(ncr))

    title_by_film = {}
    for m in movies_raw:
        fid = m.get("ID") or m.get("ScheduledFilmId")
        if fid:
            title_by_film[fid] = _clean(m.get("Title") or m.get("movie_title"))

    # Sessions for the target date, in NCR cinemas.
    day = [s for s in sessions_raw
           if isinstance(s.get("SeatsAvailable"), int)
           and s.get("Showtime", "").startswith(show_date)
           and s.get("CinemaId") in ncr]

    # Capacity per (cinema, screen) = max SeatsAvailable across ALL future sessions.
    cap = collections.defaultdict(int)
    for s in sessions_raw:
        if isinstance(s.get("SeatsAvailable"), int) and s.get("CinemaId") in ncr \
                and s.get("Showtime", "") >= show_date:
            k = (s["CinemaId"], s.get("ScreenNumber"))
            cap[k] = max(cap[k], s["SeatsAvailable"])

    movies_out, sessions_out = {}, []
    for s in day:
        fid = s.get("ScheduledFilmId")
        title = title_by_film.get(fid) or fid or "Unknown"
        mid = f"CINE-{fid}"
        movies_out.setdefault(mid, {"id": mid, "title": title,
                                    "format": _fmt(s.get("FormatCode")), "slug": None})
        capacity = cap.get((s["CinemaId"], s.get("ScreenNumber")), 0)
        avail = s["SeatsAvailable"]
        booked = max(0, capacity - avail) if capacity else None
        occ = round(100 * booked / capacity, 1) if capacity else None
        sessions_out.append({
            "id": f"CINE-{s['CinemaId']}-{s['SessionId']}",
            "cinema_id": ncr[s["CinemaId"]]["id"],
            "movie_id": mid,
            "show_date": show_date,
            "show_time": s["Showtime"][11:16],
            "screen": s.get("ScreenName"),
            "price_tiers": [],
            # real occupancy payload:
            "seats_total": capacity or None,
            "seats_booked": booked,
            "occupancy_pct": occ,
            "sold_out": s.get("SoldoutStatus") == 1,
        })
    log.info("Cinepolis %s: %d cinemas, %d movies, %d sessions (real occupancy)",
             show_date, len(ncr), len(movies_out), len(sessions_out))
    return {"cinemas": list(ncr.values()), "movies": list(movies_out.values()),
            "sessions": sessions_out}


def crawl_cinepolis_all_dates(crawler, start_date: str) -> dict:
    """Fetch once, return sessions for ALL available dates (not just one day)."""
    with crawler._context() as ctx:
        page = ctx.new_page()
        page.goto(HOME, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)

        cinemas_raw = _first_list(_fetch_json(page, f"{API}/api/cinemas/cinemas/?city_id=9")) or []
        movies_raw = _first_list(_fetch_json(page, f"{API}/api/movies/now-playing/?city_id=9")) or []
        sessions_raw = (_fetch_json(page, f"{API}/api/booking/get-sessions/?city_id=9") or {}).get("value", [])
        page.close()

    ncr = {}
    for c in cinemas_raw:
        cname = (c.get("city_name") or "").strip().lower()
        if cname in NCR_CITIES:
            ncr[c["ID"]] = {
                "id": f"CINE-{c['ID']}",
                "raw_id": c["ID"],
                "name": _clean(c.get("Name")),
                "chain": "Cinepolis",
                "area": c.get("city_name"),
                "lat": _num(c.get("Latitude")), "lng": _num(c.get("Longitude")),
            }
    log.info("Cinepolis NCR cinemas: %d", len(ncr))

    title_by_film = {}
    for m in movies_raw:
        fid = m.get("ID") or m.get("ScheduledFilmId")
        if fid:
            title_by_film[fid] = _clean(m.get("Title") or m.get("movie_title"))

    all_sessions = [s for s in sessions_raw
                    if isinstance(s.get("SeatsAvailable"), int)
                    and s.get("Showtime", "") >= start_date
                    and s.get("CinemaId") in ncr]

    cap = collections.defaultdict(int)
    for s in sessions_raw:
        if isinstance(s.get("SeatsAvailable"), int) and s.get("CinemaId") in ncr \
                and s.get("Showtime", "") >= start_date:
            k = (s["CinemaId"], s.get("ScreenNumber"))
            cap[k] = max(cap[k], s["SeatsAvailable"])

    movies_out, sessions_out = {}, []
    for s in all_sessions:
        fid = s.get("ScheduledFilmId")
        title = title_by_film.get(fid) or fid or "Unknown"
        mid = f"CINE-{fid}"
        movies_out.setdefault(mid, {"id": mid, "title": title,
                                    "format": _fmt(s.get("FormatCode")), "slug": None})
        capacity = cap.get((s["CinemaId"], s.get("ScreenNumber")), 0)
        avail = s["SeatsAvailable"]
        booked = max(0, capacity - avail) if capacity else None
        occ = round(100 * booked / capacity, 1) if capacity else None
        show_date = s["Showtime"][:10]
        sessions_out.append({
            "id": f"CINE-{s['CinemaId']}-{s['SessionId']}",
            "cinema_id": ncr[s["CinemaId"]]["id"],
            "movie_id": mid,
            "show_date": show_date,
            "show_time": s["Showtime"][11:16],
            "screen": s.get("ScreenName"),
            "price_tiers": [],
            "seats_total": capacity or None,
            "seats_booked": booked,
            "occupancy_pct": occ,
            "sold_out": s.get("SoldoutStatus") == 1,
        })
    log.info("Cinepolis all dates from %s: %d cinemas, %d movies, %d sessions",
             start_date, len(ncr), len(movies_out), len(sessions_out))
    return {"cinemas": list(ncr.values()), "movies": list(movies_out.values()),
            "sessions": sessions_out}


_FORMATS = {"0000000001": "2D", "0000000002": "2D", "0000000003": "3D",
            "0000000004": "IMAX", "0000000005": "4DX"}


def _fmt(code):
    return _FORMATS.get(str(code), None)


def _clean(s):
    return (s or "").strip()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
