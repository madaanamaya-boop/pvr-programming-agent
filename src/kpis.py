"""The 8 programming KPIs from the project brief, computed across Delhi-NCR
with a PVR-INOX vs Cinepolis lens.

Data reality (see README): occupancy is BookMyShow's 4-level fill bucket, not an
exact seat count, and screen capacity/exact admissions are not scrapable. So
"admissions" here is an ESTIMATE = occupancy-proxy × a nominal screen capacity,
and every seat-derived figure is labelled estimated in the API/UI. Show counts,
chain, daypart and title mix are EXACT — so KPIs 5/6/7 and the show-allocation
diagnosis are exact; 1/2/4/8 are demand-proxy estimates.
"""
from __future__ import annotations

import re

from src import config
from src.store import connect


def _pretty(t: str) -> str:
    """Human-friendly title: drop any parenthetical qualifier, title-case."""
    t = re.sub(r"\s*\([^)]*\)", "", t or "").strip()
    return t.title() if t.isupper() else t


def norm_title(t: str) -> str:
    """Normalise a film title so the same movie matches across sources and
    across language/format variants. Strips all parentheticals and punctuation:
    'Awarapan 2', 'AWARAPAN 2 (HINDI)', 'AWARAPAN 2 (2D-ATMOS) (HINDI)' -> 'AWARAPAN 2'."""
    t = re.sub(r"\([^)]*\)", " ", (t or "").upper())
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()

# Prime dayparts = commercially relevant evening/night (17:00–24:00).
PRIME_START_HOUR = 17
NOMINAL_CAPACITY = 180  # stand-in for screen capacity (real seats not scrapable)

# Allocation-index diagnosis thresholds (demand share ÷ show share).
UNDER = 1.15   # index above this => under-showcased (deserves more shows)
OVER = 0.85    # index below this => over-showcased


def _group(chain: str) -> str:
    if chain in config.PVR_GROUP:
        return "PVR-INOX"
    if chain == "Cinepolis":
        return "Cinepolis"
    return "Other"


def _daypart(t: str | None) -> str:
    try:
        h = int((t or "0").split(":")[0])
    except Exception:
        h = 0
    return "Morning" if h < 12 else "Afternoon" if h < 17 else "Evening" if h < 21 else "Night"


def _rows(date: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT t.session_id, t.occupancy_pct, t.fill_label, t.seats_total, t.seats_booked
              FROM snapshots t
              JOIN (SELECT session_id, MAX(crawl_ts) mx FROM snapshots GROUP BY session_id) l
                ON l.session_id=t.session_id AND l.mx=t.crawl_ts)
            SELECT s.id, s.show_time, s.movie_id, s.cinema_id,
                   m.title, m.format, c.name AS cinema_name, c.chain,
                   latest.occupancy_pct, latest.fill_label,
                   latest.seats_total, latest.seats_booked
            FROM sessions s
            JOIN movies m ON m.id=s.movie_id
            JOIN cinemas c ON c.id=s.cinema_id
            LEFT JOIN latest ON latest.session_id=s.id
            WHERE s.show_date=?
            """, (date,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["group"] = _group(d["chain"])
        d["daypart"] = _daypart(d["show_time"])
        d["prime"] = _hour(d["show_time"]) >= PRIME_START_HOUR
        # Real admissions/capacity when available (Cinepolis), else occupancy proxy.
        if d.get("seats_booked") is not None:
            d["adm_est"] = d["seats_booked"]
            d["capacity"] = d.get("seats_total") or 0
        else:
            occ = d["occupancy_pct"] or 0
            d["adm_est"] = occ / 100.0 * NOMINAL_CAPACITY
            d["capacity"] = NOMINAL_CAPACITY
        out.append(d)
    return out


def _hour(t):
    try:
        return int((t or "0").split(":")[0])
    except Exception:
        return 0


def kpis(date: str, group: str | None = None) -> dict:
    """Per-movie KPI table plus headline totals. `group` optionally restricts to
    'PVR-INOX' or 'Cinepolis'."""
    rows = _rows(date)
    scope = [r for r in rows if not group or r["group"] == group]
    total_shows = len(scope)
    total_adm = sum(r["adm_est"] for r in scope) or 1
    total_cap = sum(r.get("capacity") or 0 for r in scope) or 1

    by_movie: dict[str, dict] = {}
    for r in scope:
        key = norm_title(r["title"])
        m = by_movie.setdefault(key, {
            "movie_id": key, "title": _pretty(r["title"]), "shows": 0, "adm_est": 0.0,
            "capacity": 0, "occ": [], "prime_shows": 0, "cinemas": set(), "dayparts": set(),
            "pvr_shows": 0, "cine_shows": 0})
        m["shows"] += 1
        m["adm_est"] += r["adm_est"]
        m["capacity"] += r.get("capacity") or 0
        if r["occupancy_pct"] is not None:
            m["occ"].append(r["occupancy_pct"])
        if r["prime"]:
            m["prime_shows"] += 1
        m["cinemas"].add(r["cinema_id"])
        m["dayparts"].add(r["daypart"])
        if r["group"] == "PVR-INOX":
            m["pvr_shows"] += 1
        elif r["group"] == "Cinepolis":
            m["cine_shows"] += 1

    total_cinemas = len({r["cinema_id"] for r in scope}) or 1
    out = []
    for m in by_movie.values():
        shows = m["shows"]
        demand_share = m["adm_est"] / total_adm
        show_share = shows / total_shows if total_shows else 0
        seat_share = (m["capacity"] / total_cap) if total_cap else show_share
        alloc = round(demand_share / show_share, 2) if show_share else None
        cap_idx = round(demand_share / seat_share, 2) if seat_share else None
        diagnosis = ("Under-showcased" if alloc and alloc >= UNDER
                     else "Over-showcased" if alloc and alloc <= OVER else "Balanced")
        # Opportunity: extra shows deserved if under-showcased.
        deserved = demand_share * total_shows
        extra_shows = max(0, round(deserved - shows))
        adm_per_show = m["adm_est"] / shows if shows else 0
        out.append({
            "movie_id": m["movie_id"], "title": m["title"],
            "shows": shows,
            "adm_per_show": round(adm_per_show),                 # KPI 1 (est)
            "avg_occ": round(sum(m["occ"]) / len(m["occ"]), 1) if m["occ"] else None,  # KPI 2
            "admissions_share": round(100 * demand_share, 1),
            "show_share": round(100 * show_share, 1),
            "allocation_index": alloc,                            # KPI 3
            "capacity_index": cap_idx,                            # KPI 4 (real seat share)
            "competitive_show_index": (round(m["pvr_shows"] / m["cine_shows"], 2)
                                       if m["cine_shows"] else (None if not m["pvr_shows"] else float("inf"))),  # KPI 5
            "pvr_shows": m["pvr_shows"], "cine_shows": m["cine_shows"],
            "prime_time_pct": round(100 * m["prime_shows"] / shows, 1) if shows else 0,  # KPI 6
            "catchment_pct": round(100 * len(m["cinemas"]) / total_cinemas, 1),          # KPI 7
            "daypart_spread": len(m["dayparts"]),                                         # KPI 7b
            "opportunity_extra_shows": extra_shows,                                        # KPI 8
            "opportunity_adm": round(extra_shows * adm_per_show),                          # KPI 8 (est)
            "diagnosis": diagnosis,
        })
    out.sort(key=lambda x: -x["shows"])

    pvr = sum(1 for r in rows if r["group"] == "PVR-INOX")
    cine = sum(1 for r in rows if r["group"] == "Cinepolis")
    return {
        "date": date, "scope": group or "All chains",
        "total_shows": total_shows,
        "est_total_admissions": round(sum(r["adm_est"] for r in scope)),
        "pvr_shows": pvr, "cinepolis_shows": cine,
        "competitive_show_index": round(pvr / cine, 2) if cine else None,
        "movies": out,
        "note": "Occupancy = BMS fill bucket; admissions are estimated (occupancy × nominal capacity). Show counts, chain, daypart are exact.",
    }


def allocation_table(date: str) -> list[dict]:
    """The brief's 'Share of Shows vs Share of Demand' table with diagnosis."""
    k = kpis(date)
    return [{
        "film": m["title"], "admissions_share": m["admissions_share"],
        "show_share": m["show_share"], "allocation_index": m["allocation_index"],
        "diagnosis": m["diagnosis"],
    } for m in k["movies"]]


def head_to_head(date: str) -> dict:
    """PVR-INOX vs Cinepolis, title by title: who runs more shows / fills better."""
    rows = _rows(date)
    movies: dict[str, dict] = {}
    for r in rows:
        if r["group"] not in ("PVR-INOX", "Cinepolis"):
            continue
        key = norm_title(r["title"])
        m = movies.setdefault(key, {
            "title": r["title"].title() if r["title"].isupper() else r["title"],
            "pvr_shows": 0, "cine_shows": 0, "pvr_occ": [], "cine_occ": []})
        if r["group"] == "PVR-INOX":
            m["pvr_shows"] += 1
            if r["occupancy_pct"] is not None:
                m["pvr_occ"].append(r["occupancy_pct"])
        else:
            m["cine_shows"] += 1
            if r["occupancy_pct"] is not None:
                m["cine_occ"].append(r["occupancy_pct"])
    out = []
    for m in movies.values():
        out.append({
            "title": m["title"],
            "pvr_shows": m["pvr_shows"], "cine_shows": m["cine_shows"],
            "show_gap": m["pvr_shows"] - m["cine_shows"],
            "pvr_occ": round(sum(m["pvr_occ"]) / len(m["pvr_occ"]), 1) if m["pvr_occ"] else None,
            "cine_occ": round(sum(m["cine_occ"]) / len(m["cine_occ"]), 1) if m["cine_occ"] else None,
        })
    out.sort(key=lambda x: -(x["pvr_shows"] + x["cine_shows"]))
    return {"date": date, "movies": out}
