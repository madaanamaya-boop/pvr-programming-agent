"""Programming intelligence layer.

Turns the raw show/occupancy data into actionable recommendations for the PVR
programming team, answering: "at this cinema, in this time slot, would a
different movie sell better than what's scheduled?"

Demand for a movie is inferred from two signals, city-wide across Delhi-NCR:
  1. Occupancy  — how full that movie's shows are running elsewhere.
  2. Momentum   — share of its shows that are Fast Filling / Almost Full.
  3. Competition— how many shows rival chains are running of it (revealed demand).

For every cinema x time-slot we compare the scheduled movie's occupancy against
the best-demand movie in that slot the cinema is under-serving, and estimate the
incremental tickets from a swap.
"""
from __future__ import annotations

from src import config
from src.store import connect

# Time-of-day buckets (show_time is "HH:MM", 24h).
SLOTS = [("Morning", 0, 12), ("Afternoon", 12, 17), ("Evening", 17, 21), ("Night", 21, 24)]

# Without seat-map counts we approximate a screen's capacity for uplift math.
AVG_SEATS_PER_SHOW = 180

# Minimum occupancy-point gap before we bother recommending a swap.
SWAP_GAP_THRESHOLD = 20


def _slot(show_time: str | None) -> str:
    try:
        h = int((show_time or "00:00").split(":")[0])
    except Exception:
        h = 0
    for name, lo, hi in SLOTS:
        if lo <= h < hi:
            return name
    return "Night"


def _latest_rows(conn, date: str) -> list[dict]:
    """Latest snapshot per session on `date`, joined to movie + cinema."""
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT t.session_id, t.occupancy_pct, t.fill_label, t.avail_status
          FROM snapshots t
          JOIN (SELECT session_id, MAX(crawl_ts) mx FROM snapshots GROUP BY session_id) l
            ON l.session_id=t.session_id AND l.mx=t.crawl_ts
        )
        SELECT s.id, s.show_time, s.movie_id, s.cinema_id,
               m.title, c.name AS cinema_name, c.chain, c.area,
               latest.occupancy_pct, latest.fill_label
        FROM sessions s
        JOIN movies m  ON m.id = s.movie_id
        JOIN cinemas c ON c.id = s.cinema_id
        LEFT JOIN latest ON latest.session_id = s.id
        WHERE s.show_date = ?
        """, (date,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["slot"] = _slot(d["show_time"])
        out.append(d)
    return out


def movie_demand(date: str) -> list[dict]:
    """Per-movie, per-slot demand across all of Delhi-NCR."""
    with connect() as conn:
        rows = _latest_rows(conn, date)
    agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r["movie_id"], r["slot"])
        d = agg.setdefault(key, {
            "movie_id": r["movie_id"], "title": r["title"], "slot": r["slot"],
            "shows": 0, "occ": [], "hot": 0, "pvr_shows": 0, "competitor_shows": 0,
            "cinemas": set(),
        })
        d["shows"] += 1
        d["cinemas"].add(r["cinema_id"])
        if r["occupancy_pct"] is not None:
            d["occ"].append(r["occupancy_pct"])
        if r["fill_label"] in ("Fast Filling", "Almost Full"):
            d["hot"] += 1
        if r["chain"] in config.PVR_GROUP:
            d["pvr_shows"] += 1
        else:
            d["competitor_shows"] += 1
    out = []
    for d in agg.values():
        occ = round(sum(d["occ"]) / len(d["occ"]), 1) if d["occ"] else None
        hot_pct = round(100 * d["hot"] / d["shows"], 1) if d["shows"] else 0
        out.append({
            "movie_id": d["movie_id"], "title": d["title"], "slot": d["slot"],
            "shows": d["shows"], "avg_occ": occ, "hot_pct": hot_pct,
            "pvr_shows": d["pvr_shows"], "competitor_shows": d["competitor_shows"],
            "cinemas": len(d["cinemas"]),
            "demand_index": _demand_index(occ, hot_pct, d["competitor_shows"]),
        })
    out.sort(key=lambda x: -(x["demand_index"] or 0))
    return out


def _demand_index(avg_occ, hot_pct, competitor_shows) -> float | None:
    """0-100 demand score: occupancy is the backbone, momentum and competitor
    supply add signal that a title is in demand."""
    if avg_occ is None:
        return None
    idx = 0.7 * avg_occ + 0.2 * hot_pct + 0.1 * min(100, competitor_shows * 4)
    return round(idx, 1)


def recommendations(date: str, min_gap: int = SWAP_GAP_THRESHOLD) -> list[dict]:
    """Per cinema x slot: recommend swapping to a higher-demand movie the cinema
    is under-serving. Ranked by estimated incremental tickets/day."""
    with connect() as conn:
        rows = _latest_rows(conn, date)

    # Demand by (slot -> movie) lookup.
    demand = {}
    for d in movie_demand(date):
        demand[(d["slot"], d["movie_id"])] = d
    # Best-demand movies per slot.
    best_by_slot: dict[str, list[dict]] = {}
    for (slot, mid), d in demand.items():
        best_by_slot.setdefault(slot, []).append(d)
    for slot in best_by_slot:
        best_by_slot[slot].sort(key=lambda x: -(x["demand_index"] or 0))

    # What each cinema already shows per slot.
    shown: dict[tuple, set] = {}
    cinema_meta: dict[str, dict] = {}
    slot_shows: dict[tuple, list] = {}
    for r in rows:
        shown.setdefault((r["cinema_id"], r["slot"]), set()).add(r["movie_id"])
        cinema_meta[r["cinema_id"]] = {"name": r["cinema_name"], "chain": r["chain"], "area": r["area"]}
        slot_shows.setdefault((r["cinema_id"], r["slot"]), []).append(r)

    recs = []
    for (cinema_id, slot), sessions in slot_shows.items():
        cin = cinema_meta[cinema_id]
        # The cinema's weakest scheduled show in this slot is the swap candidate.
        occ_sessions = [s for s in sessions if s["occupancy_pct"] is not None]
        if not occ_sessions:
            continue
        weakest = min(occ_sessions, key=lambda s: s["occupancy_pct"])
        cur_occ = weakest["occupancy_pct"]
        # Demand index of what's currently scheduled (weakest show) for a fair
        # apples-to-apples comparison against alternatives.
        cur_demand = demand.get((slot, weakest["movie_id"]), {}).get("demand_index", cur_occ)

        already = shown.get((cinema_id, slot), set())
        # Best-demand movie in this slot the cinema is NOT already showing.
        alt = next((d for d in best_by_slot.get(slot, [])
                    if d["movie_id"] not in already and d["demand_index"] is not None), None)
        if not alt:
            continue
        gap = (alt["demand_index"] or 0) - (cur_demand or cur_occ)
        # Trigger on a clear demand gap, OR a strong supply signal the cinema is
        # missing entirely (rivals heavily programming a title it doesn't show).
        supply_gap = alt["competitor_shows"] >= 20 and alt["movie_id"] not in already
        if gap < min_gap and not supply_gap:
            continue
        uplift = round(max(gap, 0) / 100.0 * AVG_SEATS_PER_SHOW)
        recs.append({
            "cinema_id": cinema_id,
            "cinema": cin["name"],
            "chain": cin["chain"],
            "area": cin["area"],
            "slot": slot,
            "current_movie": weakest["title"],
            "current_occ": cur_occ,
            "suggested_movie": alt["title"],
            "suggested_demand": alt["demand_index"],
            "suggested_occ": alt["avg_occ"],
            "competitor_shows_of_suggested": alt["competitor_shows"],
            "est_uplift_tickets": uplift,
            "rationale": (
                f"{alt['title']} is running at {alt['avg_occ']}% avg occupancy across "
                f"{alt['cinemas']} NCR cinemas this slot ({alt['competitor_shows']} competitor shows), "
                f"vs your {weakest['title']} at {cur_occ}%."
            ),
        })
    recs.sort(key=lambda x: -x["est_uplift_tickets"])
    return recs
