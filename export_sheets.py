"""Export Cinepolis + PVR data to Google Sheets with multiple views."""
import json
import os
import re
import sqlite3
from collections import Counter

import gspread

SA_PATH = os.path.join(os.path.dirname(__file__), "..", "zepto-ads-automation", "service_account.json")
SHEET_KEY = "1R6qTXA6BbyYaltPWYaoGPl4yVjujS0grQwtSWHzK4qY"
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "pvr.db")

DEMO_MOVIES = {"Blockbuster Alpha", "Indie Beta", "Family Gamma", "Regional Delta"}


def _normalize_cinema_name(name: str) -> str:
    """Strip punctuation, city suffixes, mall names, and extra whitespace for fuzzy matching."""
    n = name.lower()
    n = re.sub(r'\b(new delhi|delhi|gurugram|gurgaon|noida|ghaziabad|faridabad)\b', '', n)
    n = re.sub(r'[,\-–—:().\']', ' ', n)
    n = re.sub(r'\b(pvr|inox|cinepolis|cinépolis)\b', '', n)
    n = re.sub(r'\b(mall|district centre|sector \d+|sec \d+|indirapuram|saket|mathura road)\b', '', n)
    n = re.sub(r'\b(dlf city centre|funcity|aipl joystreet|dlf)\b', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def _build_zone_lookup(cinema_names: list[str] = None) -> dict:
    """Build cinema_name → zone mapping with fuzzy fallback for District names."""
    exact = {}
    norm_to_zone = {}
    for zone, chains in ZONE_MAP.items():
        for chain, names in chains.items():
            for name in names:
                exact[name] = zone
                norm_to_zone[_normalize_cinema_name(name)] = zone

    if not cinema_names:
        return exact

    lookup = dict(exact)
    for cn in cinema_names:
        if cn in lookup:
            continue
        cn_norm = _normalize_cinema_name(cn)
        if cn_norm in norm_to_zone:
            lookup[cn] = norm_to_zone[cn_norm]
            continue
        for ref_norm, zone in norm_to_zone.items():
            ref_words = set(ref_norm.split())
            cn_words = set(cn_norm.split())
            if len(ref_words) >= 2 and len(ref_words & cn_words) >= len(ref_words) * 0.6:
                lookup[cn] = zone
                break
    return lookup


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def norm(t):
    t = re.sub(r"\([^)]*\)", " ", (t or "").upper())
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pretty(t):
    t = re.sub(r"\s*\([^)]*\)", "", t or "").strip()
    return t.title() if t.isupper() else t


def avg_price(price_tiers_json):
    try:
        tiers = json.loads(price_tiers_json) if isinstance(price_tiers_json, str) else (price_tiers_json or [])
        prices = [t.get("price", 0) for t in tiers if t.get("price")]
        return round(sum(prices) / len(prices)) if prices else 250
    except Exception:
        return 250


def _revenue_from_tiers(price_tiers_json, total_booked):
    """Calculate revenue from per-tier price × booked seats. Falls back to avg×booked."""
    try:
        tiers = json.loads(price_tiers_json) if isinstance(price_tiers_json, str) else (price_tiers_json or [])
        if not tiers or not any(t.get("price") for t in tiers):
            return round((total_booked or 0) * 250)
        # If tiers have per-area booked counts, use them for exact revenue
        if any(t.get("booked") is not None for t in tiers):
            return round(sum(t.get("booked", 0) * t.get("price", 0) for t in tiers))
        # Otherwise use weighted avg price × total booked
        total_seats = sum(t.get("total", 0) for t in tiers) or 1
        weighted_price = sum(t.get("price", 0) * t.get("total", 0) for t in tiers) / total_seats
        return round((total_booked or 0) * weighted_price)
    except Exception:
        return round((total_booked or 0) * 250)


def load_sessions():
    with connect() as conn:
        rows = conn.execute("""
            WITH latest AS (
              SELECT t.session_id, t.occupancy_pct, t.fill_label, t.seats_total, t.seats_booked, t.is_exact, t.est_revenue
              FROM snapshots t
              JOIN (SELECT session_id, MAX(crawl_ts) mx FROM snapshots GROUP BY session_id) l
                ON l.session_id=t.session_id AND l.mx=t.crawl_ts)
            SELECT s.id, s.show_date, s.show_time, s.screen,
                   m.title, m.language, m.format,
                   c.name AS cinema, c.chain, c.area,
                   latest.occupancy_pct, latest.seats_total, latest.seats_booked,
                   latest.fill_label, latest.is_exact, s.price_tiers, latest.est_revenue AS db_revenue
            FROM sessions s
            JOIN movies m ON m.id=s.movie_id
            JOIN cinemas c ON c.id=s.cinema_id
            JOIN latest ON latest.session_id=s.id
            ORDER BY s.show_date DESC, c.name, s.show_time
        """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["title"] in DEMO_MOVIES:
            continue
        d["has_occ"] = d["is_exact"] == 1 and d["occupancy_pct"] is not None and (d["seats_total"] or 0) > 0
        d["avg_price"] = avg_price(d["price_tiers"])
        if d.get("db_revenue") and d["db_revenue"] > 0:
            d["est_revenue"] = round(d["db_revenue"])
        elif d["has_occ"]:
            d["est_revenue"] = _revenue_from_tiers(d["price_tiers"], d["seats_booked"])
        else:
            d["est_revenue"] = None
        d["movie_norm"] = pretty(d["title"])
        d["chain_group"] = "PVR-INOX" if d["chain"] in ("PVR", "INOX") else d["chain"]
        out.append(d)

    # Build canonical cinema name map (prefer longest name per normalized key)
    canon_name = {}
    for s in out:
        key = _normalize_cinema_name(s["cinema"])
        prev = canon_name.get(key, "")
        if len(s["cinema"]) > len(prev):
            canon_name[key] = s["cinema"]

    # Normalize all cinema names to canonical form
    for s in out:
        s["cinema"] = canon_name.get(_normalize_cinema_name(s["cinema"]), s["cinema"])

    # Deduplicate PVR vs DIST sessions (same cinema, date, time ±5min)
    # Prefer DIST (has pricing) over PVR
    seen = {}
    deduped = []
    for s in out:
        cinema_key = _normalize_cinema_name(s["cinema"])
        time_key = s["show_time"] or ""
        dedup_key = (cinema_key, s["show_date"], s["movie_norm"], time_key[:4])
        existing = seen.get(dedup_key)
        if existing is not None:
            old = deduped[existing]
            if s["has_occ"] and not old["has_occ"]:
                deduped[existing] = s
            elif s.get("est_revenue") and not old.get("est_revenue"):
                deduped[existing] = s
            continue
        seen[dedup_key] = len(deduped)
        deduped.append(s)
    return deduped


def sheet1_cinepolis_data(sessions):
    """All Cinepolis sessions with real occupancy."""
    header = ["Date", "Cinema", "Area", "Movie", "Language", "Format", "Screen",
              "Show Time", "Seats Total", "Seats Booked", "Occupancy %",
              "Fill Status", "Avg Ticket Price", "Est Revenue"]
    rows = [header]
    for s in sessions:
        if s["chain"] != "Cinepolis":
            continue
        rows.append([
            s["show_date"], s["cinema"], s["area"], s["movie_norm"], s["language"], s["format"],
            s["screen"], s["show_time"], s["seats_total"], s["seats_booked"],
            s["occupancy_pct"], s["fill_label"], s["avg_price"], s["est_revenue"]
        ])
    return rows


def sheet_pvr_data(sessions):
    """All PVR-INOX sessions (show schedules, no occupancy)."""
    header = ["Date", "Cinema", "Area", "Movie", "Language", "Format", "Screen",
              "Show Time", "Status"]
    rows = [header]
    for s in sessions:
        if s["chain_group"] != "PVR-INOX":
            continue
        rows.append([
            s["show_date"], s["cinema"], s["area"], s["movie_norm"], s["language"], s["format"],
            s["screen"], s["show_time"], s["fill_label"] or ""
        ])
    return rows


def sheet2_shows_split(sessions):
    """Total shows % split by chain, per date."""
    from collections import Counter, defaultdict
    by_date = defaultdict(Counter)
    for s in sessions:
        by_date[s["show_date"]][s["chain_group"]] += 1

    header = ["Date", "Chain", "Total Shows", "% of Total Shows"]
    rows = [header]
    for date in sorted(by_date.keys(), reverse=True):
        chain_counts = by_date[date]
        total = sum(chain_counts.values()) or 1
        for chain, count in sorted(chain_counts.items(), key=lambda x: -x[1]):
            rows.append([date, chain, count, round(100 * count / total, 1)])
        rows.append([date, "TOTAL", total, 100.0])
    return rows


def sheet3_hall_occupancy(sessions):
    """Occupancy per cinema hall (cinema + screen), per date."""
    from collections import defaultdict
    halls = defaultdict(lambda: {"shows": 0, "occ_shows": 0, "occ_sum": 0, "booked": 0, "total": 0, "revenue": 0})
    for s in sessions:
        key = (s["show_date"], s["cinema"], s["screen"], s["chain_group"])
        h = halls[key]
        h["shows"] += 1
        if s["has_occ"]:
            h["occ_shows"] += 1
            h["occ_sum"] += (s["occupancy_pct"] or 0)
            h["booked"] += (s["seats_booked"] or 0)
            h["total"] += (s["seats_total"] or 0)
            h["revenue"] += (s["est_revenue"] or 0)

    header = ["Date", "Cinema", "Screen/Hall", "Chain", "Shows", "Total Seats",
              "Total Booked", "Avg Occupancy %", "Est Revenue"]
    rows = [header]
    for (date, cinema, screen, chain), h in sorted(halls.items(), key=lambda x: (x[0][0], x[0][1]), reverse=True):
        avg_occ = round(h["occ_sum"] / h["occ_shows"], 1) if h["occ_shows"] else ""
        rows.append([date, cinema, screen, chain, h["shows"], h["total"] or "",
                     h["booked"] or "", avg_occ, h["revenue"] or ""])
    return rows


def sheet4_cinema_breakdown(sessions):
    """Cinema-wise summary per date: total shows, avg occupancy, est revenue."""
    from collections import defaultdict
    cinemas = defaultdict(lambda: {"chain": "", "area": "", "shows": 0, "occ_shows": 0, "occ_sum": 0, "booked": 0, "total": 0, "revenue": 0, "screens": set()})
    for s in sessions:
        c = cinemas[(s["show_date"], s["cinema"])]
        c["chain"] = s["chain_group"]
        c["area"] = s["area"]
        c["shows"] += 1
        c["screens"].add(s["screen"])
        if s["has_occ"]:
            c["occ_shows"] += 1
            c["occ_sum"] += (s["occupancy_pct"] or 0)
            c["booked"] += (s["seats_booked"] or 0)
            c["total"] += (s["seats_total"] or 0)
            c["revenue"] += (s["est_revenue"] or 0)

    header = ["Date", "Cinema", "Chain", "Area", "Screens Used", "Total Shows",
              "Total Capacity", "Total Booked", "Avg Occupancy %", "Est Revenue"]
    rows = [header]
    for (date, name), c in sorted(cinemas.items(), key=lambda x: (x[0][0], -x[1]["shows"]), reverse=True):
        avg_occ = round(c["occ_sum"] / c["occ_shows"], 1) if c["occ_shows"] else ""
        rows.append([date, name, c["chain"], c["area"], len(c["screens"]),
                     c["shows"], c["total"] or "", c["booked"] or "", avg_occ, c["revenue"] or ""])
    return rows


def sheet5_movie_occupancy(sessions):
    """Overall movie occupancy numbers, per date."""
    from collections import defaultdict
    movies = defaultdict(lambda: {"shows": 0, "occ_shows": 0, "occ_sum": 0, "booked": 0, "total": 0, "revenue": 0, "cinemas": set()})
    for s in sessions:
        key = (s["show_date"], norm(s["title"]))
        m = movies[key]
        m["title"] = s["movie_norm"]
        m["date"] = s["show_date"]
        m["shows"] += 1
        m["cinemas"].add(s["cinema"])
        if s["has_occ"]:
            m["occ_shows"] += 1
            m["occ_sum"] += (s["occupancy_pct"] or 0)
            m["booked"] += (s["seats_booked"] or 0)
            m["total"] += (s["seats_total"] or 0)
            m["revenue"] += (s["est_revenue"] or 0)

    header = ["Date", "Movie", "Total Shows", "Cinemas Playing", "Total Capacity",
              "Total Booked", "Avg Occupancy %", "Est Revenue"]
    rows = [header]
    for key, m in sorted(movies.items(), key=lambda x: (x[0][0], -x[1]["shows"]), reverse=True):
        avg_occ = round(m["occ_sum"] / m["occ_shows"], 1) if m["occ_shows"] else ""
        rows.append([m["date"], m["title"], m["shows"], len(m["cinemas"]),
                     m["total"] or "", m["booked"] or "", avg_occ, m["revenue"] or ""])
    return rows


def sheet6_cinema_movie_occupancy(sessions):
    """PVR vs Cinepolis movie occupancy comparison, per date."""
    from collections import defaultdict
    data = defaultdict(lambda: {"pvr_shows": 0, "pvr_occ": [], "pvr_booked": 0,
                                "cine_shows": 0, "cine_occ": [], "cine_booked": 0})
    for s in sessions:
        key = (s["show_date"], norm(s["title"]))
        d = data[key]
        d["title"] = s["movie_norm"]
        d["date"] = s["show_date"]
        if s["chain_group"] == "PVR-INOX":
            d["pvr_shows"] += 1
            if s["has_occ"]:
                d["pvr_occ"].append(s["occupancy_pct"] or 0)
                d["pvr_booked"] += (s["seats_booked"] or 0)
        elif s["chain_group"] == "Cinepolis":
            d["cine_shows"] += 1
            if s["has_occ"]:
                d["cine_occ"].append(s["occupancy_pct"] or 0)
                d["cine_booked"] += (s["seats_booked"] or 0)

    header = ["Date", "Movie", "Cinepolis Shows", "Cinepolis Avg Occ %", "Cinepolis Booked",
              "PVR Shows", "PVR Avg Occ %", "PVR Booked"]
    rows = [header]
    for key, d in sorted(data.items(), key=lambda x: (x[0][0], -(x[1]["cine_shows"] + x[1]["pvr_shows"])), reverse=True):
        cine_avg = round(sum(d["cine_occ"]) / len(d["cine_occ"]), 1) if d["cine_occ"] else ""
        pvr_avg = round(sum(d["pvr_occ"]) / len(d["pvr_occ"]), 1) if d["pvr_occ"] else ""
        rows.append([d["date"], d["title"], d["cine_shows"], cine_avg, d["cine_booked"],
                     d["pvr_shows"], pvr_avg or "", d["pvr_booked"] or ""])
    return rows


def sheet7_show_allocation(sessions):
    """Show allocation per movie per chain, per date."""
    from collections import defaultdict
    alloc = defaultdict(lambda: defaultdict(int))
    for s in sessions:
        key = (s["show_date"], s["movie_norm"])
        chain = s["chain_group"]
        alloc[key][chain] += 1

    chains = sorted({s["chain_group"] for s in sessions})
    header = ["Date", "Movie"] + [f"{c} Shows" for c in chains] + ["Total Shows"]
    rows = [header]
    for (date, movie) in sorted(alloc.keys(), reverse=True):
        row = [date, movie]
        total = 0
        for c in chains:
            count = alloc[(date, movie)].get(c, 0)
            row.append(count)
            total += count
        row.append(total)
        rows.append(row)
    return rows


ZONE_MAP = {
    "DL01-Saket": {
        "Cinepolis": ["Cinépolis DLF Place Saket"],
        "PVR-INOX": ["PVR Anupam Saket Delhi", "PVR Select City Walk Delhi", "PVR Select Citywalk, Saket"],
    },
    "DL02-South Delhi": {
        "Cinepolis": ["Cinépolis Savitri GK II", "Cinepolis Savitri Complex, Greater Kailash 2, New Delhi"],
        "PVR-INOX": ["INOX COCA-COLA IMAX Paras, Nehru Place, Delhi", "INOX Insignia At Epicuria,Nehru Place Delhi",
                      "INOX Nehru Place Delhi", "PVR ECX Chanakyapuri Delhi", "INOX Pacific Mall, Jasola Delhi"],
    },
    "DL03-Patel Nagar": {
        "Cinepolis": ["Cinépolis (Fun Cinema) CRM Mall Shahdara", "Cinepolis Cross River Mall, Shahdara, New Delhi"],
        "PVR-INOX": ["INOX Patel Nagar Delhi", "PVR Naraina Delhi", "PVR Midtown Moti Nagar Delhi",
                      "PVR Naraina, Community Centre, New Delhi"],
    },
    "DL04-Lajpat Nagar": {
        "Cinepolis": [],
        "PVR-INOX": ["PVR 3CS Lajpat Nagar Delhi", "INOX Insignia At RCube Monad Mall Delhi"],
    },
    "DL05-Janak Place": {
        "Cinepolis": ["Cinépolis Janak", "Cinepolis Janak Cinema, Janakpuri, New Delhi"],
        "PVR-INOX": ["INOX Janak Place Delhi", "PVR Vikaspuri Delhi",
                      "PVR Vikaspuri, Virjanand Marg, New Delhi"],
    },
    "DL06-North Delhi": {
        "Cinepolis": ["Cinépolis Pacific NSP2", "Cinépolis Unity One Rohini Delhi"],
        "PVR-INOX": ["PVR Cinemagic, Unity One Elegante, NSP, Pitampura", "PVR Prashant Vihar Delhi",
                      "PVR Shalimar Bagh Delhi"],
    },
    "DL07-East Delhi": {
        "Cinepolis": ["Cinépolis V3S East Centre", "Cinépolis V3S Mall Laxmi Nagar"],
        "PVR-INOX": ["INOX Vishal Mall, Rajouri Garden Delhi", "PVR Pacific Subhash Nagar Delhi"],
    },
    "DL08-CP-Central": {
        "Cinepolis": [],
        "PVR-INOX": ["INOX Odeon,Connaught Place Delhi", "PVR Plaza-CP, Delhi", "PVR Sangam Delhi",
                      "PVR IMAX with Laser - Priya Delhi", "PVR Sangam, R.K. Puram, New Delhi"],
    },
    "DL09-Dwarka": {
        "Cinepolis": ["Cinépolis City Centre Dwarka", "Cinepolis Unity City Centre, Dwarka"],
        "PVR-INOX": ["PVR Pacific D21 Dwarka", "PVR Vegas Dwarka"],
    },
    "DL10-Vasant Kunj": {
        "Cinepolis": [],
        "PVR-INOX": ["PVR Promenade Vasant Kunj Delhi", "Delhi PVR Directors Cut Ambience Mall",
                      "PVR Priya, Vasant Vihar"],
    },
    "GG01-Gurugram Central": {
        "Cinepolis": ["Cinépolis Airia Gurugram", "Cinépolis The Esplanade",
                      "Cinepolis Airia Mall, Sohna Road, Gurugram"],
        "PVR-INOX": ["Gurugram Pepsi PVR Ambience", "Gurugram: PVR Directors Cut Ambience Mall",
                      "INOX DLF CyberHub, Gurugram",
                      "HDFC Millennia PVR: MGF,  Gurugram", "INOX Sapphire 83 Gurugram",
                      "INOX Sapphire 90 Mall, Gurugram", "INOX World Mark, Gurugram",
                      "PVR City Centre Gurugram"],
    },
    "GG02-Gurugram South": {
        "Cinepolis": ["Cinépolis Ireo", "Cinepolis Grand View High Street, Gurugram"],
        "PVR-INOX": ["INOX AIPL Joy Street Mall Gurugram", "INOX Ardee Mall, Gurugram",
                      "INOX IRIS Broadway Gurugram", "PVR Mega Mall Gurugram",
                      "PVR Elan Mercado, Sec 80, Gurugram", "PVR Elan Miracle, Sec 84, Gurugram",
                      "PVR Elan Town Centre, Sec 67, Gurugram"],
    },
    "ND01-Noida": {
        "Cinepolis": ["Cinépolis Modi", "Cinepolis Modi Mall (Formerly Spice Mall), Noida"],
        "PVR-INOX": ["PVR Superplex Logix Noida", "PVR Superplex Mall Of India Noida",
                      "Noida: PVR Directors Cut, DLF Mall of India"],
    },
    "ND02-Greater Noida": {
        "Cinepolis": ["Cinépolis The Grand Venice Mall Greater Noida"],
        "PVR-INOX": ["PVR Gaur City Greater Noida", "INOX Omaxe Connaught Place Mall Greater Noida"],
    },
    "GZ01-Ghaziabad": {
        "Cinepolis": [],
        "PVR-INOX": ["GHAZIABAD OPULENT", "INOX Shipra Mall Ghaziabad", "PVR Edm Ghaziabad",
                      "PVR Mahagun Ghaziabad", "PVR VVIP Ghaziabad",
                      "PVR EDM, EDM Mall, Ghaziabad", "PVR Mahagun, Mahagun Metro Mall, Ghaziabad"],
    },
    "FB01-Faridabad": {
        "Cinepolis": ["Cinépolis Pacific"],
        "PVR-INOX": ["INOX Crown Interiorz Mall, Faridabad", "INOX EF3 Mall,Mathura Road Faridabad",
                      "PVR Pacific Mall (The Mall of Faridabad NIT)", "PVR Pebble Downtown Sec-12, Faridabad",
                      "PVR Piyush Mahendra Faridabad"],
    },
}


def sheet8_competitor_map(sessions):
    """Zone-wise competitor mapping in hierarchical pivot: Zone → Chain → Cinema → Movies."""
    from collections import defaultdict

    all_cinema_names = list({s["cinema"] for s in sessions})
    cinema_lookup = _build_zone_lookup(all_cinema_names)

    # Aggregate per zone/chain/cinema/movie
    agg = defaultdict(lambda: {"shows": 0, "occ": [], "booked": 0, "total": 0})
    for s in sessions:
        zone = cinema_lookup.get(s["cinema"])
        if not zone:
            continue
        key = (zone, s["chain_group"], s["cinema"], s["movie_norm"])
        d = agg[key]
        d["shows"] += 1
        if s["has_occ"]:
            d["occ"].append(s["occupancy_pct"] or 0)
            d["booked"] += (s["seats_booked"] or 0)
            d["total"] += (s["seats_total"] or 0)

    # Build hierarchy: zone → chain → cinema → [(movie, stats)]
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for (zone, chain, cinema, movie), d in agg.items():
        avg_occ = round(sum(d["occ"]) / len(d["occ"]), 1) if d["occ"] else ""
        tree[zone][chain][cinema].append({
            "movie": movie, "shows": d["shows"],
            "avg_occ": avg_occ, "booked": d["booked"], "total": d["total"],
        })

    header = ["Zone / Chain / Cinema", "Movie", "Shows", "Avg Occupancy %",
              "Seats Booked", "Total Capacity"]
    rows = [header]

    for zone in sorted(tree.keys()):
        # Zone totals
        z_shows = sum(m["shows"] for ch in tree[zone].values() for ci in ch.values() for m in ci)
        z_occ_vals = [v for ch in tree[zone].values() for ci in ch.values() for m in ci for v in ([m["avg_occ"]] if m["avg_occ"] != "" else [])]
        z_avg = round(sum(z_occ_vals) / len(z_occ_vals), 1) if z_occ_vals else ""
        z_booked = sum(m["booked"] for ch in tree[zone].values() for ci in ch.values() for m in ci)
        z_total = sum(m["total"] for ch in tree[zone].values() for ci in ch.values() for m in ci)
        rows.append([zone, "", z_shows, z_avg, z_booked or "", z_total or ""])

        for chain in sorted(tree[zone].keys()):
            ch_shows = sum(m["shows"] for ci in tree[zone][chain].values() for m in ci)
            ch_occ_vals = [v for ci in tree[zone][chain].values() for m in ci for v in ([m["avg_occ"]] if m["avg_occ"] != "" else [])]
            ch_avg = round(sum(ch_occ_vals) / len(ch_occ_vals), 1) if ch_occ_vals else ""
            ch_booked = sum(m["booked"] for ci in tree[zone][chain].values() for m in ci)
            ch_total = sum(m["total"] for ci in tree[zone][chain].values() for m in ci)
            rows.append([f"  {chain}", "", ch_shows, ch_avg, ch_booked or "", ch_total or ""])

            for cinema in sorted(tree[zone][chain].keys()):
                movies = sorted(tree[zone][chain][cinema], key=lambda m: -m["shows"])
                ci_shows = sum(m["shows"] for m in movies)
                ci_occ_vals = [m["avg_occ"] for m in movies if m["avg_occ"] != ""]
                ci_avg = round(sum(ci_occ_vals) / len(ci_occ_vals), 1) if ci_occ_vals else ""
                ci_booked = sum(m["booked"] for m in movies)
                ci_total = sum(m["total"] for m in movies)
                rows.append([f"    {cinema}", "", ci_shows, ci_avg, ci_booked or "", ci_total or ""])

                for m in movies:
                    rows.append(["", m["movie"], m["shows"], m["avg_occ"],
                                 m["booked"] or "", m["total"] or ""])

    return rows


def _screen_price(screen: str) -> int:
    """Estimate avg ticket price from screen type."""
    sc = (screen or "").upper()
    if "INSIGNIA" in sc or "DIRECTOR" in sc:
        return 700
    if "GOLD" in sc or "LUXE" in sc:
        return 600
    if "IMAX" in sc:
        return 500
    if "4DX" in sc:
        return 450
    return 250


def _screen_category(screen: str) -> str:
    """Classify screen into a constraint category."""
    sc = (screen or "").upper()
    if "JUNIOR" in sc or "KID" in sc or "PLAY" in sc:
        return "kids"
    if "IMAX" in sc:
        return "imax"
    if "4DX" in sc:
        return "4dx"
    if "SCREEN X" in sc or "SCREENX" in sc:
        return "screenx"
    if "ICE" in sc:
        return "ice"
    if "GOLD" in sc or "LUXE" in sc or "INSIGNIA" in sc or "DIRECTOR" in sc:
        return "premium"
    return "regular"


_KIDS_MOVIES = {"paw patrol", "minions", "moana", "frozen", "despicable", "lego",
                "kung fu panda", "trolls", "coco", "encanto", "elemental", "inside out"}


def _is_kids_movie(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in _KIDS_MOVIES)


def _format_tag(fmt: str) -> str:
    """Normalize format to a comparable tag."""
    f = (fmt or "").upper()
    if "IMAX" in f:
        return "imax"
    if "4DX" in f:
        return "4dx"
    if "3D" in f:
        return "3d"
    if "ICE" in f:
        return "ice"
    if "SCREEN X" in f or "SCREENX" in f:
        return "screenx"
    return "2d"


def sheet9_swap_recommendations(sessions, occ_threshold=15):
    """Suggest show swaps for upcoming PVR-INOX shows where occupancy is low.
    Respects language, format, and screen-type constraints."""
    from collections import defaultdict
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")

    # Build zone lookup (fuzzy matching for District names)
    all_cinema_names = list({s["cinema"] for s in sessions})
    cinema_to_zone = _build_zone_lookup(all_cinema_names)

    # Today and future dates (upcoming shows that can still be changed)
    future = [s for s in sessions if s["show_date"] >= today]
    pvr_future = [s for s in future if s["chain_group"] == "PVR-INOX"]
    cine_future = [s for s in future if s["chain_group"] == "Cinepolis"]

    if not pvr_future:
        return [["No upcoming PVR-INOX data available for swap analysis"]]

    # --- Movie metadata: primary language and format ---
    movie_meta = defaultdict(lambda: {"languages": Counter(), "formats": Counter(), "is_kids": False})
    for s in future:
        mm = movie_meta[s["movie_norm"]]
        if s.get("language"):
            mm["languages"][s["language"]] += 1
        if s.get("format"):
            mm["formats"][_format_tag(s["format"])] += 1
        if _is_kids_movie(s["movie_norm"]):
            mm["is_kids"] = True

    def movie_primary_lang(movie):
        langs = movie_meta[movie]["languages"]
        return langs.most_common(1)[0][0] if langs else ""

    def movie_has_format(movie, fmt_tag):
        return fmt_tag in movie_meta[movie]["formats"]

    # --- City-wide movie stats (PVR) ---
    pvr_movie_stats = defaultdict(lambda: {"shows": 0, "occ": [], "booked": 0, "total": 0, "cinemas": set()})
    for s in pvr_future:
        m = pvr_movie_stats[(s["show_date"], s["movie_norm"])]
        m["shows"] += 1
        m["cinemas"].add(s["cinema"])
        if s["has_occ"]:
            m["occ"].append(s["occupancy_pct"] or 0)
            m["booked"] += (s["seats_booked"] or 0)
            m["total"] += (s["seats_total"] or 0)

    # --- Cinepolis movie stats for comparison ---
    cine_movie_stats = defaultdict(lambda: {"shows": 0, "occ": [], "booked": 0})
    for s in cine_future:
        m = cine_movie_stats[(s["show_date"], s["movie_norm"])]
        m["shows"] += 1
        if s["has_occ"]:
            m["occ"].append(s["occupancy_pct"] or 0)
            m["booked"] += (s["seats_booked"] or 0)

    # --- Per cinema+movie+date stats (PVR) ---
    pvr_cinema_movie = defaultdict(lambda: {"shows": 0, "occ": [], "booked": 0, "total": 0,
                                             "revenue": 0, "screens": set(),
                                             "languages": set(), "formats": set()})
    for s in pvr_future:
        k = (s["show_date"], s["cinema"], s["movie_norm"])
        d = pvr_cinema_movie[k]
        d["shows"] += 1
        d["screens"].add(s["screen"])
        d["area"] = s["area"]
        if s.get("language"):
            d["languages"].add(s["language"])
        if s.get("format"):
            d["formats"].add(_format_tag(s["format"]))
        if s["has_occ"]:
            d["occ"].append(s["occupancy_pct"] or 0)
            d["booked"] += (s["seats_booked"] or 0)
            d["total"] += (s["seats_total"] or 0)
            d["revenue"] += (s.get("est_revenue") or 0)
            d["avg_price"] = s.get("avg_price", 250)

    # --- Build high-demand movie pool per date ---
    high_demand_cache = {}
    def get_high_demand(date):
        if date in high_demand_cache:
            return high_demand_cache[date]
        candidates = []
        for (d, movie), stats in pvr_movie_stats.items():
            if d != date:
                continue
            avg = sum(stats["occ"]) / len(stats["occ"]) if stats["occ"] else None
            cine = cine_movie_stats.get((d, movie))
            cine_avg = sum(cine["occ"]) / len(cine["occ"]) if cine and cine["occ"] else None
            score = avg or 0
            if cine_avg and cine_avg > 40:
                score = max(score, cine_avg)
            if score >= 12:
                candidates.append({
                    "movie": movie, "pvr_avg_occ": avg, "cine_avg_occ": cine_avg,
                    "pvr_shows": stats["shows"], "cine_shows": cine["shows"] if cine else 0,
                    "score": round(score, 1),
                    "primary_lang": movie_primary_lang(movie),
                    "is_kids": movie_meta[movie]["is_kids"],
                })
        high_demand_cache[date] = sorted(candidates, key=lambda x: -x["score"])
        return high_demand_cache[date]

    # --- Zone-level movie performance (what's hot in each zone) ---
    zone_movie_occ = defaultdict(lambda: defaultdict(list))
    for s in future:
        z = cinema_to_zone.get(s["cinema"])
        if z and s["has_occ"]:
            zone_movie_occ[(s["show_date"], z)][s["movie_norm"]].append(s["occupancy_pct"] or 0)

    # --- Find underperformers and generate swaps ---
    cinema_date_suggestions = defaultdict(set)
    recommendations = []

    underperformers = []
    for (date, cinema, movie), d in pvr_cinema_movie.items():
        if d["shows"] < 2:
            continue
        local_avg = sum(d["occ"]) / len(d["occ"]) if d["occ"] else None
        if local_avg is None:
            continue
        if local_avg >= occ_threshold:
            continue
        city_stats = pvr_movie_stats.get((date, movie))
        city_avg = sum(city_stats["occ"]) / len(city_stats["occ"]) if city_stats and city_stats["occ"] else None
        underperformers.append((date, cinema, movie, d, local_avg, city_avg))

    underperformers.sort(key=lambda x: x[4])

    for date, cinema, movie, d, local_avg, city_avg in underperformers:
        zone = cinema_to_zone.get(cinema, "")
        high_demand = get_high_demand(date)
        already_suggested = cinema_date_suggestions[(date, cinema)]

        cinema_movies = {m for (dd, cc, m) in pvr_cinema_movie if dd == date and cc == cinema}

        # Current show constraints
        current_langs = d["languages"]
        current_formats = d["formats"]
        screen_cats = {_screen_category(sc) for sc in d["screens"]}
        is_kids_screen = "kids" in screen_cats
        is_imax_screen = "imax" in screen_cats
        is_4dx_screen = "4dx" in screen_cats
        is_screenx_screen = "screenx" in screen_cats
        is_ice_screen = "ice" in screen_cats

        scored = []
        for hd in high_demand:
            if hd["movie"] == movie:
                continue
            existing = pvr_cinema_movie.get((date, cinema, hd["movie"]))
            if existing and existing["shows"] >= 4:
                continue

            # Language not enforced — cinemas regularly swap across languages

            # --- Screen-type constraints ---
            if is_kids_screen and not hd["is_kids"]:
                continue
            if is_imax_screen and not movie_has_format(hd["movie"], "imax"):
                continue
            if is_4dx_screen and not movie_has_format(hd["movie"], "4dx"):
                continue
            if is_screenx_screen and not movie_has_format(hd["movie"], "screenx"):
                continue
            if is_ice_screen and not movie_has_format(hd["movie"], "ice"):
                continue

            # --- Don't put adult movies on kids screens ---
            if is_kids_screen and not _is_kids_movie(hd["movie"]):
                continue

            penalty = 0
            suggestion_count = sum(1 for k, v in cinema_date_suggestions.items() if k[0] == date and hd["movie"] in v)
            if hd["movie"] in already_suggested:
                penalty = 25
            if suggestion_count > 3:
                penalty += min(20, suggestion_count * 2)

            zone_occs = zone_movie_occ.get((date, zone), {}).get(hd["movie"], [])
            zone_bonus = min(10, sum(zone_occs) / len(zone_occs) / 5) if zone_occs else 0
            fresh_bonus = 5 if hd["movie"] not in cinema_movies else 0
            effective_score = hd["score"] - penalty + zone_bonus + fresh_bonus
            scored.append((effective_score, hd))

        scored.sort(key=lambda x: -x[0])
        if not scored:
            continue
        best = scored[0][1]
        cinema_date_suggestions[(date, cinema)].add(best["movie"])

        # Use real revenue if available, else screen-type pricing
        if d["revenue"] > 0:
            current_rev = round(d["revenue"])
            per_show_price = min(500, d["revenue"] / max(d["booked"], 1)) if d["booked"] else d.get("avg_price", 250)
        else:
            per_show_price = round(sum(_screen_price(sc) for sc in d["screens"]) / max(len(d["screens"]), 1))
            current_rev = round(d["booked"] / max(d["shows"], 1) * per_show_price)
        # Conservative projection: use 50% of citywide avg, cap at 30% occ
        projected_occ = min(0.30, best["score"] / 100 * 0.50)
        avg_capacity = d["total"] / max(d["shows"], 1) if d["total"] else 200
        projected_rev = round(projected_occ * avg_capacity * per_show_price * d["shows"])
        uplift = projected_rev - current_rev

        # Priority
        if local_avg < 5 and uplift > 5000:
            priority = "HIGH"
        elif local_avg < 10 and uplift > 2000:
            priority = "HIGH"
        elif uplift > 3000:
            priority = "MED"
        else:
            priority = "LOW"

        # Reason
        reasons = []
        if city_avg and local_avg < city_avg * 0.5:
            reasons.append(f"{movie} avg {city_avg:.0f}% citywide but {local_avg:.0f}% here")
        cine_stats = cine_movie_stats.get((date, best["movie"]))
        if cine_stats and cine_stats["occ"]:
            ca = sum(cine_stats["occ"]) / len(cine_stats["occ"])
            if ca > 30:
                reasons.append(f"{best['movie']} at {ca:.0f}% in Cinepolis ({cine_stats['shows']} shows)")
        if best.get("pvr_avg_occ") and best["pvr_avg_occ"] > 40:
            reasons.append(f"{best['movie']} at {best['pvr_avg_occ']:.0f}% avg across PVR")
        reason = "; ".join(reasons) if reasons else f"{best['movie']} has {best['score']}% demand score"

        lang_str = "/".join(sorted(current_langs)) if current_langs else ""
        screen_str = ", ".join(sorted(d["screens"])) if d["screens"] else ""

        recommendations.append({
            "priority": priority,
            "date": date,
            "cinema": cinema,
            "zone": zone,
            "current_movie": movie,
            "language": lang_str,
            "screen": screen_str,
            "current_shows": d["shows"],
            "current_occ": round(local_avg, 1),
            "current_rev": current_rev,
            "suggested_movie": best["movie"],
            "suggested_occ": best["score"],
            "projected_rev": projected_rev,
            "uplift": uplift,
            "reason": reason,
        })

    # Sort: HIGH first, then by uplift
    priority_order = {"HIGH": 0, "MED": 1, "LOW": 2}
    recommendations.sort(key=lambda r: (priority_order.get(r["priority"], 3), -r["uplift"]))

    # --- Summary rows ---
    header = ["Priority", "Date", "Cinema", "Zone", "Current Movie", "Language",
              "Screen", "Shows", "Current Occ%", "Current Rev (est)",
              "Suggested Movie", "Expected Occ%", "Projected Rev", "Revenue Uplift", "Reason"]
    rows = [header]

    for r in recommendations:
        rows.append([
            r["priority"], r["date"], r["cinema"], r["zone"],
            r["current_movie"], r["language"], r["screen"],
            r["current_shows"], r["current_occ"],
            r["current_rev"], r["suggested_movie"], r["suggested_occ"],
            r["projected_rev"], r["uplift"], r["reason"],
        ])

    # Add summary section at bottom
    if recommendations:
        rows.append([])
        rows.append(["=== SUMMARY ==="])
        rows.append(["Total swap recommendations", len(recommendations)])
        total_uplift = sum(r["uplift"] for r in recommendations)
        rows.append(["Total potential revenue uplift", f"Rs {total_uplift:,}"])
        high_count = sum(1 for r in recommendations if r["priority"] == "HIGH")
        rows.append(["High priority swaps", high_count])

        # Over-screened movies (most recommendations to remove)
        over_screened = defaultdict(int)
        for r in recommendations:
            over_screened[r["current_movie"]] += r["current_shows"]
        rows.append([])
        rows.append(["=== OVER-SCREENED (reduce shows) ==="])
        rows.append(["Movie", "Excess Shows", "Avg Occ%"])
        for movie, shows in sorted(over_screened.items(), key=lambda x: -x[1])[:5]:
            occs = [r["current_occ"] for r in recommendations if r["current_movie"] == movie]
            rows.append([movie, shows, round(sum(occs) / len(occs), 1) if occs else ""])

        # Under-screened movies (most recommended as replacement)
        under_screened = defaultdict(int)
        for r in recommendations:
            under_screened[r["suggested_movie"]] += 1
        rows.append([])
        rows.append(["=== UNDER-SCREENED (add more shows) ==="])
        rows.append(["Movie", "Times Recommended", "Demand Score"])
        for movie, count in sorted(under_screened.items(), key=lambda x: -x[1])[:5]:
            scores = [r["suggested_occ"] for r in recommendations if r["suggested_movie"] == movie]
            rows.append([movie, count, round(sum(scores) / len(scores), 1) if scores else ""])

    return rows


def main():
    sessions = load_sessions()
    print(f"Loaded {len(sessions)} sessions (real occupancy, excluding demo)")

    gc = gspread.service_account(filename=SA_PATH)
    sh = gc.open_by_key(SHEET_KEY)

    sheets_data = [
        ("Cinepolis Data", sheet1_cinepolis_data(sessions)),
        ("PVR-INOX Shows", sheet_pvr_data(sessions)),
        ("Shows Split by Chain", sheet2_shows_split(sessions)),
        ("Hall Occupancy", sheet3_hall_occupancy(sessions)),
        ("Cinema Breakdown", sheet4_cinema_breakdown(sessions)),
        ("Movie Occupancy", sheet5_movie_occupancy(sessions)),
        ("PVR vs Cinepolis", sheet6_cinema_movie_occupancy(sessions)),
        ("Show Allocation by Brand", sheet7_show_allocation(sessions)),
        ("Competitor Map", sheet8_competitor_map(sessions)),
        ("Swap Recommendations", sheet9_swap_recommendations(sessions)),
    ]

    existing = {w.title: w for w in sh.worksheets()}

    for title, data in sheets_data:
        nrows = len(data)
        ncols = len(data[0]) if data else 1
        if title in existing:
            ws = existing[title]
            ws.clear()
            ws.resize(rows=max(nrows, 1), cols=max(ncols, 1))
        else:
            ws = sh.add_worksheet(title=title, rows=max(nrows, 100), cols=max(ncols, 20))
        ws.update(range_name="A1", values=data)
        ws.set_basic_filter()
        print(f"  Written: {title} ({nrows - 1} rows)")

    # Remove default Sheet1 if it exists and is empty
    if "Sheet1" in existing:
        try:
            sh.del_worksheet(existing["Sheet1"])
            print("  Removed empty Sheet1")
        except Exception:
            pass

    print(f"\nDone! Sheet: https://docs.google.com/spreadsheets/d/{SHEET_KEY}")


if __name__ == "__main__":
    main()
