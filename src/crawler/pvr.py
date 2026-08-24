"""PVR-INOX API crawler. Fetches show schedules (no seat counts) for all
Delhi-NCR PVR/INOX cinemas via api3.pvrcinemas.com."""
from __future__ import annotations

import re
import time
import requests

NCR_COORDS = {"lat": "28.632445", "lng": "77.2198104"}
BASE = "https://api3.pvrcinemas.com/api/v1/booking/content"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "https://www.pvrcinemas.com",
    "Referer": "https://www.pvrcinemas.com/",
    "appVersion": "1.0",
    "chain": "PVR",
    "country": "INDIA",
    "platform": "WEBSITE",
    "city": "Delhi-NCR",
}


def _post(endpoint, body, **kw):
    r = requests.post(f"{BASE}/{endpoint}", headers=HEADERS, json=body, timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def fetch_cinemas() -> list[dict]:
    d = _post("search", {"city": "Delhi-NCR", **NCR_COORDS, "type": "HOME", "text": ""})
    raw = d.get("output", {}).get("cinemas", [])
    out = []
    for c in raw:
        out.append({
            "id": f"PVR-{c['theatreId']}",
            "theatreId": c["theatreId"],
            "name": c.get("name", ""),
            "chain": "PVR-INOX",
            "area": c.get("cityName", ""),
            "lat": c.get("latitude"),
            "lng": c.get("longitude"),
        })
    return out


def fetch_sessions(theatre_id: str, show_date: str) -> dict:
    d = _post("csessions", {
        "cid": theatre_id, "city": "Delhi-NCR",
        **NCR_COORDS, "dated": "NA", "qr": "NO",
        "cineType": "", "cineTypeQR": "",
    })
    out = d.get("output")
    if not out:
        return {"cinema": {}, "movies": [], "sessions": []}

    cinema_re = out.get("cinemaRe") or {}
    cinema = {
        "name": cinema_re.get("name", ""),
        "screens": cinema_re.get("screenCount"),
    }

    movies = []
    sessions = []
    seen_movies = set()

    for cms in out.get("cinemaMovieSessions", []):
        mr = cms.get("movieRe", {})
        common_name = mr.get("n", mr.get("filmCommonName", ""))

        for es in cms.get("experienceSessions", []):
            for show in es.get("shows", []):
                film_name = _find_film_name(mr, show.get("movieId"))
                movie_id = show.get("movieId", "")
                if movie_id not in seen_movies:
                    seen_movies.add(movie_id)
                    movies.append({
                        "id": f"PVR-{movie_id}",
                        "title": film_name or common_name,
                        "language": show.get("language", ""),
                        "format": show.get("movieFormat") or show.get("filmFormat") or "",
                    })

                sessions.append({
                    "id": f"PVR-{theatre_id}-{show['sessionId']}",
                    "cinema_id": f"PVR-{theatre_id}",
                    "movie_id": f"PVR-{movie_id}",
                    "show_date": show.get("showDate", show_date),
                    "show_time": _convert_time(show.get("showTime", "")),
                    "screen": show.get("screenName", ""),
                    "status": show.get("statusTxt", ""),
                    "price_tiers": [],
                })

    return {"cinema": cinema, "movies": movies, "sessions": sessions}


def _find_film_name(movie_re, movie_id):
    for f in movie_re.get("films", []):
        if f.get("filmId") == str(movie_id):
            return f.get("filmName", "")
    return movie_re.get("filmName", "")


def _convert_time(t: str) -> str:
    m = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", t.strip().upper())
    if not m:
        return t
    h, mn, ap = int(m.group(1)), m.group(2), m.group(3)
    if ap == "PM" and h != 12:
        h += 12
    elif ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mn}"


def crawl_pvr(show_date: str) -> dict:
    cinemas = fetch_cinemas()
    all_movies = {}
    all_sessions = []

    for cin in cinemas:
        try:
            data = fetch_sessions(cin["theatreId"], show_date)
        except Exception as e:
            print(f"  skip {cin['name']}: {e}")
            continue

        if data.get("cinema", {}).get("screens"):
            cin["screens"] = data["cinema"]["screens"]

        for m in data["movies"]:
            all_movies[m["id"]] = m
        all_sessions.extend(data["sessions"])
        time.sleep(0.3)

    return {
        "cinemas": cinemas,
        "movies": list(all_movies.values()),
        "sessions": all_sessions,
    }


def crawl_pvr_multi_day(dates: list) -> dict:
    """Crawl PVR for multiple dates. Pass specific date strings to get future shows."""
    cinemas = fetch_cinemas()
    all_movies = {}
    all_sessions = []

    today = dates[0] if dates else ""
    for di, d in enumerate(dates):
        dated_param = "NA" if d == today else d
        day_count = 0
        errors = 0
        for cin in cinemas:
            try:
                data = fetch_sessions_dated(cin["theatreId"], d, dated_param)
            except Exception as e:
                errors += 1
                time.sleep(2)
                continue

            if data.get("cinema", {}).get("screens"):
                cin["screens"] = data["cinema"]["screens"]

            for m in data["movies"]:
                all_movies[m["id"]] = m
            day_count += len(data["sessions"])
            all_sessions.extend(data["sessions"])
            time.sleep(0.8)
        print(f"  PVR {d}: {day_count} sessions (errors: {errors})")
        if di < len(dates) - 1:
            time.sleep(10)

    return {
        "cinemas": cinemas,
        "movies": list(all_movies.values()),
        "sessions": all_sessions,
    }


def fetch_sessions_dated(theatre_id: str, show_date: str, dated: str) -> dict:
    """Like fetch_sessions but with explicit dated param for future dates."""
    d = _post("csessions", {
        "cid": theatre_id, "city": "Delhi-NCR",
        **NCR_COORDS, "dated": dated, "qr": "NO",
        "cineType": "", "cineTypeQR": "",
    })
    out = d.get("output")
    if not out:
        return {"cinema": {}, "movies": [], "sessions": []}

    cinema_re = out.get("cinemaRe") or {}
    cinema = {
        "name": cinema_re.get("name", ""),
        "screens": cinema_re.get("screenCount"),
    }

    movies = []
    sessions = []
    seen_movies = set()

    for cms in out.get("cinemaMovieSessions", []):
        mr = cms.get("movieRe", {})
        common_name = mr.get("n", mr.get("filmCommonName", ""))

        for es in cms.get("experienceSessions", []):
            for show in es.get("shows", []):
                film_name = _find_film_name(mr, show.get("movieId"))
                movie_id = show.get("movieId", "")
                if movie_id not in seen_movies:
                    seen_movies.add(movie_id)
                    movies.append({
                        "id": f"PVR-{movie_id}",
                        "title": film_name or common_name,
                        "language": show.get("language", ""),
                        "format": show.get("movieFormat") or show.get("filmFormat") or "",
                    })

                sessions.append({
                    "id": f"PVR-{theatre_id}-{show['sessionId']}",
                    "cinema_id": f"PVR-{theatre_id}",
                    "movie_id": f"PVR-{movie_id}",
                    "show_date": show.get("showDate", show_date),
                    "show_time": _convert_time(show.get("showTime", "")),
                    "screen": show.get("screenName", ""),
                    "status": show.get("statusTxt", ""),
                    "encrypted": show.get("encrypted", ""),
                    "price_tiers": [],
                })

    return {"cinema": cinema, "movies": movies, "sessions": sessions}
