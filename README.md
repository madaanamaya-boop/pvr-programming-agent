# PVR Programming Intelligence Agent

Crawls BookMyShow for **every cinema in Delhi-NCR**, records **what movies are
running, how many shows of each, and seat-map occupancy per show**, and serves a
live, auto-refreshing dashboard for PVR-INOX leadership — with PVR-vs-competition
benchmarks and per-show **fill velocity** (how fast a show sells through the day).

## How it works

```
discover  -> every movie playing in Delhi-NCR         (src/crawler/discover.py)
showtimes -> every cinema + session per movie          (src/crawler/showtimes.py)
store     -> SQLite time-series of snapshots            (src/store.py)
intel     -> per cinema x slot swap recommendations     (src/intelligence.py)
dashboard -> Flask API + single-page UI                 (web/)
seatmap   -> exact booked/total per show (optional)     (src/crawler/seatmap.py)
```

**How the live crawl actually works (reverse-engineered from BMS, Aug 2026):**

- **Region pinning.** Every browser context injects a `rgn` cookie for Delhi-NCR,
  otherwise fresh contexts hit BMS's "select your city" interstitial and the
  listing comes back empty. (`src/crawler/session.py`)
- **Fresh context per fetch.** BMS challenges the *second* full-page navigation in
  any browser session (1st load = full 2.8 MB HTML, 2nd = a 4 KB shell). So each
  page is loaded in a throwaway context with a new fingerprint. Verified reliable
  across many sequential fetches on one IP.
- **Discover** parses the movies-listing page's hydrated anchors
  (`/movies/<slug>/<ETcode>`) — 32 movies for NCR.
- **Showtimes** are embedded in each movie's buytickets page as a Next.js flight
  payload (not an XHR). We parse venue blocks (`venueCode`/`venueName`) and
  showtime pills (`sessionId`/`showDateTime`/`availStatus`) and associate sessions
  to venues by document order. One fetch per movie = *every* venue and show in NCR
  (e.g. 136 cinemas, 800+ shows for one film).

**Occupancy signal.** The showtimes page exposes a coarse fill bucket per show
(`availStatus` → Available / Fast Filling / Almost Full / Sold Out), NOT exact
seat counts — so that's the cheap, high-coverage primary signal, stored per pass
as a time-series (velocity through the day). Exact % requires the optional
seat-map layer (`--budget N`).

> availStatus→label mapping lives in `config.AVAIL_STATUS`; eyeball it once against
> the live site (green=Available … grey=Sold Out) and adjust if BMS changes colours.

## The 8 KPIs (project brief) — `src/kpis.py`

Focus: **PVR-INOX vs Cinepolis**. Computed across Delhi-NCR at `/api/kpis`
(`?group=PVR-INOX` to rescope), `/api/allocation`, `/api/head2head`:

1. **Admissions/Show** — *estimated* (occupancy-proxy × nominal capacity)
2. **Occupancy %** — BMS fill bucket
3. **Show Allocation Index** = demand share ÷ show share → Under/Over/Balanced
4. **Capacity Allocation Index** — collapses to #3 without real seat counts
5. **Competitive Show Index** = PVR-INOX shows ÷ Cinepolis shows — **exact**
6. **Prime-Time Coverage** — % of shows in prime dayparts (17:00+) — **exact**
7. **Catchment Coverage** — cinema + daypart spread — **exact**
8. **Opportunity Score** — extra shows/admissions an under-showcased title deserves

> **Exact admissions caveat.** BookMyShow protects seat data behind three layers:
> Cloudflare (needs a headed browser), interaction-gating (grid only loads after
> click-through), and **AES encryption** of the seat payload (`doTrans.aspx` →
> `strData`). So exact booked-seat counts and screen capacity are **not scraped**;
> occupancy is the 4-level fill bucket and admissions are estimated. Show counts,
> chain split, daypart, catchment and prices are exact. KPIs 5/6/7 are exact;
> 1/2/4/8 are demand-proxy estimates.

## Intelligence layer

`src/intelligence.py` ranks each movie's **demand** across NCR (occupancy +
momentum + how many shows rivals run of it), then for every **cinema × time-slot**
flags where swapping the weakest scheduled show for a higher-demand title would
lift sales, with an estimated incremental-tickets figure. Surfaced at
`/api/recommendations` and in the dashboard's 🎯 panel.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

No login or headed seeding is needed — the region is pinned via cookie and the
city is in the URL. Just run a pass.

## Crawl

```bash
python main.py --pass full            # discover + showtimes + occupancy (today)
python main.py --pass full --limit 3  # smoke test: 3 movies only
python main.py --pass refresh         # re-poll due seat maps (velocity)
python main.py --demo                 # seed synthetic data (no BMS needed)
```

## Dashboard

```bash
python web/server.py         # http://localhost:5061
# production:
gunicorn --chdir web server:app --bind 0.0.0.0:$PORT
```

Views: city KPIs · movies by shows & occupancy · PVR-vs-competition share ·
cinema table · full session list with occupancy bars · click any show → fill
velocity chart · date picker · PVR/INOX-only toggle · CSV export. Auto-refreshes
every 60s.

## Scheduling (near-realtime)

Run `--pass full` ~2×/day and `--pass refresh` every 30–60 min via launchd/cron.
The scheduler (`src/scheduler.py`) prioritizes shows starting soon and shows
already filling fast, so limited request budget captures what's actually moving.

## Notes
- **Legal:** BookMyShow's terms restrict scraping. This is internal competitive
  intelligence for PVR — confirm PVR is comfortable with the collection method,
  or pursue a formal data partnership.
- Point at another city by changing `PVR_REGION_CODE` / `PVR_CITY_SLUG` in `.env`.
