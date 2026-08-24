"""Flask API + static dashboard for PVR leadership.

Endpoints read the latest snapshot per session (occupancy is always the most
recent capture) and aggregate. The dashboard polls these on an interval so it
auto-refreshes as new crawl passes land.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config, store  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")

# A view over each session joined to its most recent snapshot.
LATEST = """
WITH latest AS (
  SELECT s.*, m.title, m.slug AS movie_slug, m.language, m.format, c.name AS cinema_name,
         c.chain, c.area,
         sn.seats_total, sn.seats_booked, sn.occupancy_pct, sn.est_revenue,
         sn.fill_label, sn.avail_status, sn.is_exact
  FROM sessions s
  LEFT JOIN (
     SELECT t.* FROM snapshots t
     JOIN (SELECT session_id, MAX(crawl_ts) mx FROM snapshots GROUP BY session_id) l
       ON l.session_id=t.session_id AND l.mx=t.crawl_ts
  ) sn ON sn.session_id = s.id
  JOIN movies m ON m.id = s.movie_id
  JOIN cinemas c ON c.id = s.cinema_id
  WHERE s.show_date = ?
)
SELECT * FROM latest
"""


def _date() -> str:
    return request.args.get("date") or datetime.now().strftime("%Y-%m-%d")


def _rows(date, chain=None):
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(LATEST, (date,)).fetchall()]
    if chain == "pvr":
        rows = [r for r in rows if r["chain"] in config.PVR_GROUP]
    return rows


@app.route("/api/summary")
def summary():
    date = _date()
    rows = _rows(date, request.args.get("chain"))
    total_shows = len(rows)
    occ_rows = [r for r in rows if r["occupancy_pct"] is not None]
    avg_occ = round(sum(r["occupancy_pct"] for r in occ_rows) / len(occ_rows), 1) if occ_rows else 0
    revenue = round(sum(r["est_revenue"] or 0 for r in rows))

    # Fill-bucket distribution (the coarse occupancy signal for every show).
    fill_dist = {}
    for r in rows:
        lbl = r["fill_label"] or "Unknown"
        fill_dist[lbl] = fill_dist.get(lbl, 0) + 1
    exact_count = sum(1 for r in rows if r["is_exact"])

    # PVR-group vs competition share of shows.
    pvr_shows = sum(1 for r in rows if r["chain"] in config.PVR_GROUP)
    by_movie, by_cinema = {}, {}
    for r in rows:
        m = by_movie.setdefault(r["title"], {"title": r["title"], "shows": 0, "occ": []})
        m["shows"] += 1
        if r["occupancy_pct"] is not None:
            m["occ"].append(r["occupancy_pct"])
        c = by_cinema.setdefault(r["cinema_name"], {"cinema": r["cinema_name"], "chain": r["chain"], "shows": 0, "occ": []})
        c["shows"] += 1
        if r["occupancy_pct"] is not None:
            c["occ"].append(r["occupancy_pct"])

    def finish(d):
        d["avg_occ"] = round(sum(d["occ"]) / len(d["occ"]), 1) if d["occ"] else None
        d.pop("occ")
        return d

    movies = sorted((finish(m) for m in by_movie.values()), key=lambda x: -x["shows"])
    cinemas = sorted((finish(c) for c in by_cinema.values()), key=lambda x: -x["shows"])
    return jsonify({
        "date": date,
        "total_shows": total_shows,
        "avg_occupancy": avg_occ,
        "est_revenue": revenue,
        "pvr_shows": pvr_shows,
        "competitor_shows": total_shows - pvr_shows,
        "fill_distribution": fill_dist,
        "exact_occupancy_shows": exact_count,
        "top_movies": movies[:15],
        "cinemas": cinemas,
        "last_updated": _last_updated(),
    })


@app.route("/api/sessions")
def sessions():
    date = _date()
    rows = _rows(date, request.args.get("chain"))
    movie = request.args.get("movie")
    cinema = request.args.get("cinema")
    if movie:
        rows = [r for r in rows if r["title"] == movie]
    if cinema:
        rows = [r for r in rows if r["cinema_name"] == cinema]
    for r in rows:
        r["bms_url"] = _bms_url(r)
    rows.sort(key=lambda r: (r["cinema_name"], r["show_time"] or ""))
    return jsonify(rows)


def _bms_url(r) -> str:
    """BookMyShow ticket link for this show.

    Uses the movie's buytickets page for the show's date:
      /movies/<city>/<slug>/buytickets/<ET>/<YYYYMMDD>
    which reliably opens for anyone and lists this cinema + time. (The
    fully-qualified per-seat deep link /…/<VenueCode>/<sessionId>/<date> exists
    but bounces to the homepage on a fresh visit without BMS click-through state,
    so it's returned separately as `bms_seat_url` for reference, not the primary.)"""
    slug = r["movie_slug"] or "movie"
    ymd = (r["show_date"] or "").replace("-", "")
    base = f"{config.BMS_BASE}/movies/{config.CITY_SLUG}/{slug}/buytickets/{r['movie_id']}"
    return f"{base}/{ymd}"


@app.route("/api/velocity/<session_id>")
def velocity(session_id):
    with store.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT crawl_ts, occupancy_pct, seats_booked, seats_total "
            "FROM snapshots WHERE session_id=? ORDER BY crawl_ts", (session_id,)).fetchall()]
    return jsonify(rows)


@app.route("/api/demand")
def demand():
    from src import intelligence
    return jsonify(intelligence.movie_demand(_date()))


@app.route("/api/recommendations")
def recommendations():
    from src import intelligence
    min_gap = int(request.args.get("min_gap", intelligence.SWAP_GAP_THRESHOLD))
    recs = intelligence.recommendations(_date(), min_gap=min_gap)
    chain = request.args.get("chain")
    if chain == "pvr":
        recs = [r for r in recs if r["chain"] in config.PVR_GROUP]
    return jsonify({
        "count": len(recs),
        "total_uplift": sum(r["est_uplift_tickets"] for r in recs),
        "recommendations": recs[:200],
    })


@app.route("/api/kpis")
def kpis_ep():
    from src import kpis
    g = request.args.get("group")  # 'PVR-INOX' | 'Cinepolis' | None
    return jsonify(kpis.kpis(_date(), group=g))


@app.route("/api/allocation")
def allocation_ep():
    from src import kpis
    return jsonify(kpis.allocation_table(_date()))


@app.route("/api/head2head")
def head2head_ep():
    from src import kpis
    return jsonify(kpis.head_to_head(_date()))


@app.route("/api/dates")
def dates():
    with store.connect() as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT DISTINCT show_date FROM sessions ORDER BY show_date DESC").fetchall()]
    return jsonify(rows)


def _last_updated():
    with store.connect() as conn:
        r = conn.execute("SELECT MAX(crawl_ts) FROM snapshots").fetchone()
    return r[0] if r else None


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


if __name__ == "__main__":
    store.init_db()
    app.run(host="0.0.0.0", port=int(__import__("os").getenv("PORT", "5061")), debug=False)
