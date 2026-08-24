"""Orchestration: a full discovery pass and a lightweight refresh pass.

    full     -> discover movies -> per-movie showtimes -> store cinemas/movies/
                sessions -> seat-map occupancy for a prioritized batch.
    refresh  -> re-poll seat maps for the sessions the scheduler says are due.

A `demo` generator seeds realistic synthetic data so the store + dashboard can
be exercised end-to-end without a live BookMyShow session.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from src import config, store
from src.logger import get_logger
from src.scheduler import due_sessions

log = get_logger("pipeline")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def run_full(show_date: str | None = None, limit_movies: int | None = None,
             seatmap_budget: int = 0) -> dict:
    """Cheap high-coverage pass: discover -> per-movie showtimes -> store
    cinemas/movies/sessions and an availStatus occupancy snapshot for EVERY show.

    seatmap_budget > 0 additionally fetches exact seat-map occupancy for that many
    prioritized sessions (heavier; off by default).
    """
    from src.crawler.discover import discover_movies
    from src.crawler.session import Crawler
    from src.crawler.showtimes import fetch_movie_showtimes

    store.init_db()
    show_date = show_date or _today()
    ts = _utc_now_iso()
    counts = {"movies": 0, "cinemas": 0, "sessions": 0, "snapshots": 0}

    with Crawler() as cr:
        movies = discover_movies(cr)
        if limit_movies:
            movies = movies[:limit_movies]
        counts["movies"] = len(movies)

        all_sessions: list[dict] = []
        with store.connect() as conn:
            for mv in movies:
                store.upsert_movie(conn, mv)
                data = fetch_movie_showtimes(cr, mv, show_date)
                # Cinepolis is crawled from its own API (real occupancy), so skip
                # its BMS (bucket-only) venues here to avoid double-counting.
                skip = {c["id"] for c in data["cinemas"] if c.get("chain") == "Cinepolis"}
                for cin in data["cinemas"]:
                    if cin["id"] in skip:
                        continue
                    store.upsert_cinema(conn, cin)
                for s in data["sessions"]:
                    if s["cinema_id"] in skip:
                        continue
                    store.upsert_session(conn, s)
                    store.add_snapshot(conn, _avail_snapshot(s, ts))
                    all_sessions.append(s)
                time.sleep(config.REQUEST_DELAY)
            counts["cinemas"] = conn.execute("SELECT COUNT(*) FROM cinemas").fetchone()[0]
        counts["sessions"] = len(all_sessions)
        counts["snapshots"] = len(all_sessions)

        if seatmap_budget:
            counts["exact_snapshots"] = _crawl_seatmaps(cr, all_sessions[:seatmap_budget])

    log.info("Full pass complete: %s", counts)
    return counts


def run_cinepolis(show_date: str | None = None) -> dict:
    """Crawl Cinepolis's own API for REAL occupancy (exact seat counts) across
    Delhi-NCR and store it (is_exact=1). One pass = every Cinepolis show."""
    from src.crawler.session import Crawler
    from src.crawler.cinepolis import crawl_cinepolis

    store.init_db()
    show_date = show_date or _today()
    ts = _utc_now_iso()
    with Crawler() as cr:
        data = crawl_cinepolis(cr, show_date)
    with store.connect() as conn:
        for c in data["cinemas"]:
            store.upsert_cinema(conn, c)
        for m in data["movies"]:
            store.upsert_movie(conn, m)
        n = 0
        for s in data["sessions"]:
            store.upsert_session(conn, s)
            occ = s.get("occupancy_pct")
            store.add_snapshot(conn, {
                "session_id": s["id"], "crawl_ts": ts,
                "avail_status": None,
                "fill_label": _label_from_occ(occ, s.get("sold_out")),
                "occupancy_pct": occ, "is_exact": 1,
                "seats_total": s.get("seats_total"), "seats_booked": s.get("seats_booked"),
                "est_revenue": None,
            })
            n += 1
    result = {"cinemas": len(data["cinemas"]), "movies": len(data["movies"]),
              "sessions": len(data["sessions"]), "snapshots": n}
    log.info("Cinepolis pass complete: %s", result)
    return result


def _label_from_occ(occ, sold_out=False) -> str | None:
    if sold_out:
        return "Sold Out / Closed"
    if occ is None:
        return None
    return "Almost Full" if occ >= 80 else "Fast Filling" if occ >= 55 else "Available"


def _avail_snapshot(session: dict, ts: str) -> dict:
    """Build an occupancy snapshot from the cheap availStatus bucket."""
    meta = config.avail_meta(session.get("avail_status"))
    return {
        "session_id": session["id"],
        "crawl_ts": ts,
        "avail_status": session.get("avail_status"),
        "fill_label": meta["label"],
        "occupancy_pct": meta["occ"],
        "is_exact": 0,
    }


def run_refresh(show_date: str | None = None, seatmap_budget: int = 0) -> dict:
    """Cheap velocity pass: re-fetch showtimes for every movie showing today and
    append a fresh availStatus snapshot per session, so the dashboard shows how
    fill buckets move through the day. Optionally also refresh exact seat maps for
    a prioritized batch (seatmap_budget > 0)."""
    from src.crawler.session import Crawler
    from src.crawler.showtimes import fetch_movie_showtimes

    store.init_db()
    show_date = show_date or _today()
    ts = _utc_now_iso()
    with store.connect() as conn:
        movie_rows = conn.execute(
            "SELECT DISTINCT m.id, m.title, m.slug FROM sessions s "
            "JOIN movies m ON m.id = s.movie_id WHERE s.show_date = ?", (show_date,)).fetchall()
        movies = {r["id"]: {"id": r["id"], "title": r["title"],
                            "slug": r["slug"] or _slugify(r["title"])} for r in movie_rows}

    written = 0
    with Crawler() as cr:
        with store.connect() as conn:
            for mid, mv in movies.items():
                data = fetch_movie_showtimes(cr, mv, show_date)
                for s in data["sessions"]:
                    store.upsert_session(conn, s)
                    store.add_snapshot(conn, _avail_snapshot(s, ts))
                    written += 1
                time.sleep(config.REQUEST_DELAY)

        if seatmap_budget:
            now = datetime.now()
            with store.connect() as conn:
                rows = store.sessions_for_refresh(conn, show_date)
            due = due_sessions(rows, now, seatmap_budget)
            _crawl_seatmaps(cr, [dict(r) for r in due])

    return {"movies": len(movies), "snapshots": written}


def _slugify(title: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-") or "movie"


def _crawl_seatmaps(ctx, sessions: list[dict]) -> int:
    from src.crawler.seatmap import fetch_occupancy

    written = 0
    ts = _utc_now_iso()
    with store.connect() as conn:
        for s in sessions:
            price_lookup = _price_lookup(s.get("price_tiers"))
            occ = fetch_occupancy(ctx, s, price_lookup)
            if occ:
                occ["crawl_ts"] = ts
                store.add_snapshot(conn, occ)
                written += 1
    return written


def _price_lookup(price_tiers) -> dict[str, float]:
    import json
    if isinstance(price_tiers, str):
        try:
            price_tiers = json.loads(price_tiers)
        except Exception:
            price_tiers = []
    out = {}
    for t in price_tiers or []:
        out[str(t.get("category", "")).lower()] = float(t.get("price") or 0)
    return out


# --------------------------------------------------------------------------
# Demo data — realistic Delhi-NCR programming for verifying the stack offline.
# --------------------------------------------------------------------------
_DEMO_CINEMAS = [
    ("V001", "PVR Select Citywalk, Saket", "PVR", "Saket"),
    ("V002", "PVR Priya, Vasant Vihar", "PVR", "Vasant Vihar"),
    ("V003", "INOX Nehru Place", "INOX", "Nehru Place"),
    ("V004", "PVR Pacific Mall, Tagore Garden", "PVR", "Tagore Garden"),
    ("V005", "Cinepolis DLF Mall of India, Noida", "Cinepolis", "Noida"),
    ("V006", "Miraj Cinemas, Dwarka", "Miraj", "Dwarka"),
    ("V007", "INOX DLF CyberHub, Gurugram", "INOX", "Gurugram"),
    ("V008", "Delite Cinema, Asaf Ali Road", "Delite", "Central Delhi"),
]
_DEMO_MOVIES = [
    ("M001", "Blockbuster Alpha", "Hindi", "2D"),
    ("M002", "Blockbuster Alpha", "Hindi", "IMAX"),
    ("M003", "Indie Beta", "English", "2D"),
    ("M004", "Family Gamma", "Hindi", "3D"),
    ("M005", "Regional Delta", "Punjabi", "2D"),
]
_SHOW_TIMES = ["09:30", "12:45", "15:30", "18:15", "21:30"]


def seed_demo(passes: int = 3) -> dict:
    """Create synthetic cinemas/movies/sessions + `passes` occupancy snapshots
    spaced through the day so velocity charts have data."""
    store.init_db()
    rng = random.Random(42)
    show_date = _today()
    with store.connect() as conn:
        for cid, name, chain, area in _DEMO_CINEMAS:
            store.upsert_cinema(conn, {"id": cid, "name": name, "chain": chain,
                                       "area": area, "screens": rng.randint(4, 9)})
        for mid, title, lang, fmt in _DEMO_MOVIES:
            store.upsert_movie(conn, {"id": mid, "title": title, "language": lang, "format": fmt})

        sessions = []
        for cid, *_ in _DEMO_CINEMAS:
            for mid, title, lang, fmt in _DEMO_MOVIES:
                if rng.random() < 0.45:
                    continue  # not every movie in every cinema
                for t in rng.sample(_SHOW_TIMES, rng.randint(2, len(_SHOW_TIMES))):
                    sid = f"{cid}-{mid}-{t.replace(':','')}"
                    base = 180 if fmt == "IMAX" else 130
                    tiers = [{"category": "recliner", "price": base + 120},
                             {"category": "gold", "price": base + 40},
                             {"category": "std", "price": base}]
                    store.upsert_session(conn, {
                        "id": sid, "cinema_id": cid, "movie_id": mid,
                        "show_date": show_date, "show_time": t,
                        "screen": f"Audi {rng.randint(1,7)}", "price_tiers": tiers})
                    sessions.append((sid, mid, t, tiers))

        # Snapshots: occupancy climbs across passes; popular movie fills faster.
        now = datetime.now(timezone.utc)
        for p in range(passes):
            ts = (now - timedelta(hours=(passes - 1 - p) * 3)).isoformat(timespec="seconds")
            for sid, mid, t, tiers in sessions:
                popularity = {"M001": 0.9, "M002": 0.95, "M003": 0.35,
                              "M004": 0.6, "M005": 0.45}[mid]
                total = rng.choice([120, 160, 200, 240])
                fill = min(0.98, popularity * (0.3 + 0.22 * p) * rng.uniform(0.7, 1.2))
                booked = int(total * fill)
                avg_price = sum(x["price"] for x in tiers) / len(tiers)
                occ = round(100 * booked / total, 1)
                label = ("Almost Full" if occ >= 80 else "Fast Filling" if occ >= 55
                         else "Available")
                store.add_snapshot(conn, {
                    "session_id": sid, "crawl_ts": ts, "is_exact": 1,
                    "fill_label": label, "avail_status": None,
                    "seats_total": total, "seats_booked": booked,
                    "occupancy_pct": occ, "est_revenue": round(booked * avg_price, 2)})
    with store.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    log.info("Seeded demo: %d sessions, %d snapshots", len(sessions), n)
    return {"sessions": len(sessions), "snapshots": n}
