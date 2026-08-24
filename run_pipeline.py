#!/usr/bin/env python3
"""Hourly pipeline: crawl Cinepolis + PVR (shows + seats) → store → export to Google Sheets.
Designed to run via launchd on a schedule."""
import os
import sys
import time
import logging
from datetime import datetime, timedelta

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "pipeline.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("pvr-pipeline")


def run():
    start = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    log.info(f"=== Pipeline start: {today} ===")

    # 1. Crawl Cinepolis (real occupancy via Playwright + Cinepolis API) — all available dates
    log.info("Step 1: Crawling Cinepolis (all available dates)...")
    try:
        from src.crawler.session import Crawler
        from src.crawler.cinepolis import crawl_cinepolis_all_dates
        from src import store

        store.init_db()
        with Crawler() as cr:
            cine_data = crawl_cinepolis_all_dates(cr, today)
        with store.connect() as conn:
            for c in cine_data["cinemas"]:
                store.upsert_cinema(conn, c)
            for m in cine_data["movies"]:
                store.upsert_movie(conn, m)
            for s in cine_data["sessions"]:
                store.upsert_session(conn, s)
                store.add_snapshot(conn, {
                    "session_id": s["id"],
                    "crawl_ts": int(time.time()),
                    "fill_label": s.get("fill_label", ""),
                    "occupancy_pct": s.get("occupancy_pct"),
                    "is_exact": 1,
                    "seats_total": s.get("seats_total"),
                    "seats_booked": s.get("seats_booked"),
                })
        log.info(f"  Cinepolis: {len(cine_data.get('sessions', []))} sessions stored")
    except Exception as e:
        log.error(f"  Cinepolis crawl failed: {e}")

    # 2. Crawl PVR show schedules (API, no seats) — next 7 days
    dates = [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    log.info(f"Step 2: Crawling PVR show schedules ({dates[0]} to {dates[-1]})...")
    try:
        from src.crawler.pvr import crawl_pvr_multi_day
        from src import store

        pvr_data = crawl_pvr_multi_day(dates)
        ts = int(time.time())
        with store.connect() as conn:
            for c in pvr_data["cinemas"]:
                store.upsert_cinema(conn, c)
            for m in pvr_data["movies"]:
                store.upsert_movie(conn, m)
            for s in pvr_data["sessions"]:
                store.upsert_session(conn, s)
                store.add_snapshot(conn, {
                    "session_id": s["id"],
                    "crawl_ts": ts,
                    "fill_label": s.get("status", ""),
                    "is_exact": 0,
                })
        log.info(f"  PVR: {len(pvr_data['sessions'])} sessions stored")
    except Exception as e:
        log.error(f"  PVR schedule crawl failed: {e}")

    # 3. Crawl PVR seat occupancy (Playwright browser)
    log.info("Step 3: Crawling PVR seat occupancy via browser...")
    try:
        from src.crawler.pvr_seats import get_available_shows, scrape_seats_browser
        from src import store

        shows = get_available_shows()
        log.info(f"  Found {len(shows)} available PVR shows")

        if shows:
            def flush_batch(batch):
                ts = int(time.time())
                with store.connect() as conn:
                    for r in batch:
                        store.add_snapshot(conn, {
                            "session_id": r["session_id"],
                            "crawl_ts": ts,
                            "seats_total": r["seats_total"],
                            "seats_booked": r["seats_booked"],
                            "occupancy_pct": r["occupancy_pct"],
                            "fill_label": "Available",
                            "is_exact": 1,
                        })

            results = scrape_seats_browser(shows, max_shows=len(shows), store_fn=flush_batch)
            log.info(f"  PVR seats: {len(results)} shows with real occupancy")
    except Exception as e:
        log.error(f"  PVR seat crawl failed: {e}")

    # 3b. Crawl District (all chains, real occupancy via SSR)
    log.info("Step 3b: Crawling District (all chains, real occupancy)...")
    try:
        from src.crawler.session import Crawler
        from src.crawler.district import crawl_district
        from src import store

        with Crawler() as cr:
            dist_data = crawl_district(cr, today)
        ts = int(time.time())
        with store.connect() as conn:
            for c in dist_data["cinemas"]:
                store.upsert_cinema(conn, c)
            for m in dist_data["movies"]:
                store.upsert_movie(conn, m)
            for s in dist_data["sessions"]:
                store.upsert_session(conn, s)
                store.add_snapshot(conn, {
                    "session_id": s["id"],
                    "crawl_ts": ts,
                    "fill_label": "Available",
                    "occupancy_pct": s.get("occupancy_pct"),
                    "is_exact": 1,
                    "seats_total": s.get("seats_total"),
                    "seats_booked": s.get("seats_booked"),
                    "est_revenue": s.get("est_revenue"),
                })
        log.info(f"  District: {len(dist_data['sessions'])} sessions stored")
    except Exception as e:
        log.error(f"  District crawl failed: {e}")

    # 4. Export to Google Sheets
    log.info("Step 4: Exporting to Google Sheets...")
    try:
        from export_sheets import main as export_main
        export_main()
        log.info("  Google Sheets updated")
    except Exception as e:
        log.error(f"  Sheet export failed: {e}")

    elapsed = round(time.time() - start, 1)
    log.info(f"=== Pipeline done in {elapsed}s ===\n")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    run()
