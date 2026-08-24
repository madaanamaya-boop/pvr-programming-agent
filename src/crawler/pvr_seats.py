"""PVR seat occupancy crawler using Playwright browser rendering.
The PVR seatlayout API returns seat grid data but marks all seats as
en=False without a browser session. The website's React app, however,
renders real availability (seat-current-pvr vs seat-disable classes).
This module automates that browser rendering to extract real occupancy."""
from __future__ import annotations

import json
import time
from src.crawler.pvr import _post, NCR_COORDS, HEADERS

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


def get_available_shows(cinema_ids: list[str] | None = None) -> list[dict]:
    """Get all available/filling-fast shows with encrypted IDs."""
    if cinema_ids is None:
        from src.crawler.pvr import fetch_cinemas
        cinema_ids = [c["theatreId"] for c in fetch_cinemas()]

    shows = []
    for cid in cinema_ids:
        try:
            d = _post("csessions", {
                "cid": cid, "city": "Delhi-NCR",
                **NCR_COORDS, "dated": "NA", "qr": "NO",
                "cineType": "", "cineTypeQR": "",
            })
        except Exception as e:
            print(f"  skip cinema {cid}: {e}")
            continue

        out = d.get("output")
        if not out:
            continue

        cinema_name = out.get("cinemaRe", {}).get("name", "")
        for cms in out.get("cinemaMovieSessions", []):
            movie_name = cms.get("movieRe", {}).get("n", "")
            for es in cms.get("experienceSessions", []):
                for show in es.get("shows", []):
                    if show.get("statusTxt") in ("Available", "Filling Fast"):
                        shows.append({
                            "cinema_id": f"PVR-{cid}",
                            "cinema_name": cinema_name,
                            "session_id": f"PVR-{cid}-{show['sessionId']}",
                            "movie": movie_name,
                            "show_time": show.get("showTime", ""),
                            "screen": show.get("screenName", ""),
                            "encrypted": show["encrypted"],
                            "status": show["statusTxt"],
                        })
        time.sleep(0.3)

    return shows


def scrape_seats_browser(shows: list[dict], max_shows: int = 50, store_fn=None) -> list[dict]:
    """Use Playwright to load seat layout pages and extract occupancy.
    If store_fn is provided, results are flushed to DB every 20 shows."""
    if not HAS_PLAYWRIGHT:
        print("Playwright not installed, skipping seat scraping")
        return []

    results = []
    batch = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            viewport={"width": 1440, "height": 900},
        )

        page = context.new_page()
        page.goto("https://www.pvrcinemas.com", wait_until="domcontentloaded", timeout=30000)
        page.evaluate("localStorage.setItem('city', 'Delhi-NCR')")
        time.sleep(1)

        for i, show in enumerate(shows[:max_shows]):
            enc = show["encrypted"]
            url = f"https://www.pvrcinemas.com/seatlayout/{enc}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(3000)

                seat_data = page.evaluate("""() => {
                    const avail = document.querySelectorAll('.seat-current-pvr').length;
                    const booked = document.querySelectorAll('.seat-disable').length;
                    const hidden = document.querySelectorAll('.seat_hidden').length;
                    return {available: avail, booked, hidden, total: avail + booked};
                }""")

                total = seat_data["total"]
                booked = seat_data["booked"]
                if total == 0:
                    continue
                occ = round(100 * booked / total, 1)

                r = {
                    "session_id": show["session_id"],
                    "seats_total": total,
                    "seats_booked": booked,
                    "occupancy_pct": occ,
                    "is_exact": True,
                }
                results.append(r)
                batch.append(r)

                if store_fn and len(batch) >= 20:
                    store_fn(batch)
                    batch = []

                if (i + 1) % 10 == 0:
                    print(f"  Scraped {i+1}/{min(len(shows), max_shows)} shows")

            except Exception as e:
                print(f"  skip {show['cinema_name']} {show['show_time']}: {e}")
                try:
                    page.close()
                    page = context.new_page()
                    page.goto("https://www.pvrcinemas.com", wait_until="domcontentloaded", timeout=15000)
                    page.evaluate("localStorage.setItem('city', 'Delhi-NCR')")
                except Exception:
                    pass
                continue

        if store_fn and batch:
            store_fn(batch)

        browser.close()

    return results


if __name__ == "__main__":
    print("Getting available shows...")
    shows = get_available_shows(cinema_ids=["301"])
    print(f"Found {len(shows)} available shows")
    for s in shows[:5]:
        print(f"  {s['cinema_name']} | {s['movie']} | {s['show_time']} | {s['status']}")

    if shows:
        print("\nScraping seat data via browser...")
        results = scrape_seats_browser(shows, max_shows=5)
        for r in results:
            print(f"  {r['session_id']}: {r['seats_total']} seats, {r['seats_booked']} booked, {r['occupancy_pct']}%")
