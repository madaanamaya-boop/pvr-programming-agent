"""Central config for the PVR Programming Intelligence agent.

Everything city/region specific lives here so pointing the agent at another
metro is a one-line change, not a code change.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.getenv("PVR_DB_PATH", DATA_DIR / "pvr.db"))
STORAGE_STATE = Path(os.getenv("PVR_STORAGE_STATE", ROOT / "storage_state.json"))

# --- Target market ---------------------------------------------------------
# BookMyShow region code for Delhi-NCR is "NCR"; the URL slug is "national-capital-region-ncr".
REGION_CODE = os.getenv("PVR_REGION_CODE", "NCR")
CITY_SLUG = os.getenv("PVR_CITY_SLUG", "national-capital-region-ncr")
CITY_NAME = os.getenv("PVR_CITY_NAME", "Delhi-NCR")

BMS_BASE = "https://in.bookmyshow.com"

# Region cookie — pins the browsing region to Delhi-NCR. Without it, fresh
# browser contexts intermittently hit BMS's "select your city" interstitial and
# the movie listing comes back empty. Injecting this makes discovery reliable.
import json as _json  # noqa: E402
REGION_COOKIE_VALUE = _json.dumps({
    "regionCode": REGION_CODE,
    "regionName": CITY_NAME,
    "subRegionCode": "",
    "regionSlug": CITY_SLUG,
})

# --- Chain classification --------------------------------------------------
# Map keywords found in a venue name -> normalized chain label.
CHAIN_KEYWORDS = {
    "pvr": "PVR",
    "inox": "INOX",
    "cinepolis": "Cinepolis",
    "cinépolis": "Cinepolis",
    "miraj": "Miraj",
    "delite": "Delite",
    "movietime": "Movie Time",
    "wave": "Wave",
    "m2k": "M2K",
    "carnival": "Carnival",
    "us cinemas": "US Cinemas",
    "galaxy": "Galaxy",
    "moviemax": "MovieMax",
    "connplex": "Connplex",
}
PVR_GROUP = {"PVR", "INOX"}  # post-merger, both are the PVR-INOX group


def classify_chain(venue_name: str) -> str:
    low = (venue_name or "").lower()
    for kw, label in CHAIN_KEYWORDS.items():
        if kw in low:
            return label
    return "Other"


# --- Crawl behaviour -------------------------------------------------------
HEADLESS = os.getenv("PVR_HEADLESS", "true").lower() == "true"
# Politeness: max concurrent seat-map fetches and delay between requests (seconds).
MAX_CONCURRENCY = int(os.getenv("PVR_MAX_CONCURRENCY", "3"))
REQUEST_DELAY = float(os.getenv("PVR_REQUEST_DELAY", "1.5"))
NAV_TIMEOUT_MS = int(os.getenv("PVR_NAV_TIMEOUT_MS", "45000"))

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# Refresh cadence hints (used by scheduler.py). A show gets re-polled more often
# the closer it is to starting.
IMMINENT_WINDOW_HOURS = 4       # shows starting within this window are high-priority
REFRESH_HIGH_MIN = 30           # minutes between polls for imminent/high-occupancy shows
REFRESH_LOW_MIN = 180           # minutes between polls for far-off shows

# --- BookMyShow availStatus mapping ---------------------------------------
# The buytickets page exposes a coarse fill bucket per show ("availStatus"),
# NOT exact seat counts. This maps each code to a human label and a rough
# occupancy proxy % so the dashboard has a number for the cheap (no-seat-map)
# crawl path. Exact % comes from the optional seat-map layer when it runs.
#
# NOTE: the code->meaning mapping below is derived from live captures and should
# be eyeballed once against the site (green=Available, yellow=Fast Filling,
# red=Almost Full, grey=Sold Out/closed). Adjust here if BMS's colours differ.
AVAIL_STATUS = {
    "3": {"label": "Available",   "occ": 25,  "bookable": True},
    "2": {"label": "Fast Filling", "occ": 65, "bookable": True},
    "1": {"label": "Almost Full",  "occ": 90, "bookable": True},
    "0": {"label": "Sold Out / Closed", "occ": None, "bookable": False},
}


def avail_meta(code) -> dict:
    return AVAIL_STATUS.get(str(code), {"label": "Unknown", "occ": None, "bookable": False})
