"""District (Zomato) crawler — real occupancy from __NEXT_DATA__ SSR.

District embeds all cinema sessions with SeatsAvailable/SeatsTotal per area
in the __NEXT_DATA__ JSON on each movie's detail page. One page load per movie
gives every cinema in Delhi NCR with real seat counts — no seat-map scraping needed.
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from src.logger import get_logger

log = get_logger("district")

BASE = "https://www.district.in"
CITY_SLUG = "delhi-ncr"


def crawl_district(crawler, start_date: str) -> dict:
    """Crawl District for all movies in Delhi NCR.
    Returns {cinemas, movies, sessions} with real occupancy."""

    with crawler._context() as ctx:
        page = ctx.new_page()

        # Step 1: Get movie list from /movies page
        page.goto(f"{BASE}/movies", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)

        for _ in range(8):
            page.evaluate("window.scrollBy(0, 1000)")
            page.wait_for_timeout(800)

        links = page.evaluate("""() => {
            return [...new Set(
                Array.from(document.querySelectorAll('a[href*="/movies/"][href*="-MV"]'))
                .map(a => a.href)
            )]
        }""")

        # Deduplicate by movie ID
        movie_urls = {}
        for url in links:
            m = re.search(r"MV(\d+)", url)
            if m:
                mid = m.group(1)
                movie_urls[mid] = f"{BASE}/movies/{url.split('/movies/')[-1].rsplit('-movie-tickets', 1)[0]}-movie-tickets-in-{CITY_SLUG}-MV{mid}"

        log.info("District: found %d movies to crawl", len(movie_urls))

        all_cinemas = {}
        all_movies = {}
        all_sessions = []
        screen_cap = defaultdict(int)

        for i, (mid, url) in enumerate(movie_urls.items()):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)

                nd = page.evaluate("() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null }")
                if not nd:
                    continue

                data = json.loads(nd)
                ms_dict = data.get("props", {}).get("pageProps", {}).get("data", {}).get("serverState", {}).get("movieSessions", {})
                if not ms_dict:
                    continue

                ms = list(ms_dict.values())[0]
                movie_meta = ms.get("meta", {}).get("movie", {})
                movie_title = movie_meta.get("name") or movie_meta.get("title") or f"Movie-{mid}"
                movie_id = f"DIST-{mid}"

                all_movies[movie_id] = {
                    "id": movie_id,
                    "title": movie_title,
                    "language": movie_meta.get("language", ""),
                    "format": "",
                }

                for item in ms.get("arrangedSessions", []):
                    cdata = item.get("data", {})
                    cid = f"DIST-{cdata.get('id', '')}"
                    chain = cdata.get("chainKey", "")

                    if cid not in all_cinemas:
                        all_cinemas[cid] = {
                            "id": cid,
                            "name": cdata.get("name", ""),
                            "chain": _normalize_chain(chain),
                            "area": cdata.get("city", ""),
                            "lat": cdata.get("latitude"),
                            "lng": cdata.get("longitude"),
                        }

                    for s in item.get("sessions", []):
                        show_time_raw = s.get("showTime", "")
                        # District returns UTC times — convert to IST (+5:30)
                        if len(show_time_raw) >= 16:
                            try:
                                utc_dt = datetime.fromisoformat(show_time_raw.replace("Z", "+00:00")[:19])
                                ist_dt = utc_dt + timedelta(hours=5, minutes=30)
                                show_date = ist_dt.strftime("%Y-%m-%d")
                                show_time = ist_dt.strftime("%H:%M")
                            except (ValueError, TypeError):
                                show_date = show_time_raw[:10]
                                show_time = show_time_raw[11:16]
                        else:
                            show_date = show_time_raw[:10] if len(show_time_raw) >= 10 else start_date
                            show_time = ""

                        if show_date < start_date:
                            continue

                        total = 0
                        avail = 0
                        revenue = 0
                        price_tiers = []
                        for area in s.get("areas", []):
                            t = area.get("sTotal", 0) or 0
                            a = area.get("sAvail", 0) or 0
                            price = area.get("price", 0) or 0
                            area_booked = max(0, t - a)
                            total += t
                            avail += a
                            revenue += area_booked * price
                            if t > 0:
                                price_tiers.append({
                                    "label": area.get("label", ""),
                                    "price": price,
                                    "total": t,
                                    "booked": area_booked,
                                })

                        booked = max(0, total - avail)
                        occ = round(100 * booked / total, 1) if total > 0 else None

                        session_id = f"DIST-{cdata.get('id', '')}-{s.get('sid', '')}"
                        all_sessions.append({
                            "id": session_id,
                            "cinema_id": cid,
                            "movie_id": movie_id,
                            "show_date": show_date,
                            "show_time": show_time,
                            "screen": s.get("audi", ""),
                            "price_tiers": price_tiers,
                            "seats_total": total if total > 0 else None,
                            "seats_booked": booked if total > 0 else None,
                            "occupancy_pct": occ,
                            "est_revenue": revenue if total > 0 else None,
                            "sold_out": all(a.get("seatStatus") == "Sold Out" for a in s.get("areas", [])) if s.get("areas") else False,
                        })

                if (i + 1) % 5 == 0:
                    log.info("  District: %d/%d movies crawled, %d sessions so far",
                             i + 1, len(movie_urls), len(all_sessions))

            except Exception as e:
                log.warning("  District skip movie MV%s: %s", mid, e)
                continue

            time.sleep(0.5)

        page.close()

    log.info("District %s: %d cinemas, %d movies, %d sessions (real occupancy)",
             CITY_SLUG, len(all_cinemas), len(all_movies), len(all_sessions))
    return {
        "cinemas": list(all_cinemas.values()),
        "movies": list(all_movies.values()),
        "sessions": all_sessions,
    }


def _normalize_chain(chain_key: str) -> str:
    ck = (chain_key or "").strip().lower()
    if ck in ("pvr", "inox", "pvr inox"):
        return "PVR-INOX"
    if "cinepolis" in ck:
        return "Cinepolis"
    if "miraj" in ck:
        return "Miraj"
    if "wave" in ck:
        return "Wave"
    return chain_key.strip() or "Other"
