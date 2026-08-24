"""SQLite persistence — reference tables plus the append-only snapshots series.

The `snapshots` table is the heart of the system: one row per (session, crawl
time) capturing occupancy at that moment, so the dashboard can show a show
filling up through the day.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from src.config import DB_PATH, classify_chain

SCHEMA = """
CREATE TABLE IF NOT EXISTS cinemas (
    id        TEXT PRIMARY KEY,   -- BMS venue code
    name      TEXT NOT NULL,
    chain     TEXT,               -- PVR / INOX / Cinepolis / ...
    area      TEXT,
    lat       REAL,
    lng       REAL,
    screens   INTEGER
);

CREATE TABLE IF NOT EXISTS movies (
    id          TEXT PRIMARY KEY, -- BMS event code
    title       TEXT NOT NULL,
    slug        TEXT,             -- BMS URL slug
    language    TEXT,
    format      TEXT,             -- 2D / 3D / IMAX / 4DX
    certificate TEXT,
    runtime_min INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,  -- BMS session id
    cinema_id     TEXT NOT NULL REFERENCES cinemas(id),
    movie_id      TEXT NOT NULL REFERENCES movies(id),
    show_date     TEXT NOT NULL,     -- YYYY-MM-DD
    show_time     TEXT,              -- HH:MM (24h)
    screen        TEXT,
    price_tiers   TEXT               -- JSON: [{category, price}]
);

CREATE TABLE IF NOT EXISTS snapshots (
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    crawl_ts      TEXT NOT NULL,     -- ISO8601 UTC
    avail_status  TEXT,              -- BMS bucket code (cheap signal, every show)
    fill_label    TEXT,              -- "Available" / "Fast Filling" / "Almost Full" / "Sold Out"
    occupancy_pct REAL,              -- exact from seat-map, else bucket proxy
    is_exact      INTEGER DEFAULT 0, -- 1 = from seat-map, 0 = availStatus proxy
    seats_total   INTEGER,           -- seat-map only
    seats_booked  INTEGER,           -- seat-map only
    est_revenue   REAL,              -- seat-map only
    PRIMARY KEY (session_id, crawl_ts)
);

CREATE INDEX IF NOT EXISTS idx_sessions_date   ON sessions(show_date);
CREATE INDEX IF NOT EXISTS idx_sessions_cinema ON sessions(cinema_id);
CREATE INDEX IF NOT EXISTS idx_sessions_movie  ON sessions(movie_id);
CREATE INDEX IF NOT EXISTS idx_snap_session    ON snapshots(session_id);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def upsert_cinema(conn, cinema: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO cinemas (id, name, chain, area, lat, lng, screens)
           VALUES (:id, :name, :chain, :area, :lat, :lng, :screens)
           ON CONFLICT(id) DO UPDATE SET
             name=excluded.name, chain=excluded.chain, area=excluded.area,
             lat=COALESCE(excluded.lat, cinemas.lat),
             lng=COALESCE(excluded.lng, cinemas.lng),
             screens=COALESCE(excluded.screens, cinemas.screens)""",
        {
            "id": cinema["id"],
            "name": cinema["name"],
            "chain": cinema.get("chain") or classify_chain(cinema["name"]),
            "area": cinema.get("area"),
            "lat": cinema.get("lat"),
            "lng": cinema.get("lng"),
            "screens": cinema.get("screens"),
        },
    )


def upsert_movie(conn, movie: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO movies (id, title, slug, language, format, certificate, runtime_min)
           VALUES (:id, :title, :slug, :language, :format, :certificate, :runtime_min)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title,
             slug=COALESCE(excluded.slug, movies.slug),
             language=COALESCE(excluded.language, movies.language),
             format=COALESCE(excluded.format, movies.format),
             certificate=COALESCE(excluded.certificate, movies.certificate),
             runtime_min=COALESCE(excluded.runtime_min, movies.runtime_min)""",
        {
            "id": movie["id"],
            "title": movie["title"],
            "slug": movie.get("slug"),
            "language": movie.get("language"),
            "format": movie.get("format"),
            "certificate": movie.get("certificate"),
            "runtime_min": movie.get("runtime_min"),
        },
    )


def upsert_session(conn, s: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO sessions (id, cinema_id, movie_id, show_date, show_time, screen, price_tiers)
           VALUES (:id, :cinema_id, :movie_id, :show_date, :show_time, :screen, :price_tiers)
           ON CONFLICT(id) DO UPDATE SET
             show_time=excluded.show_time, screen=excluded.screen,
             price_tiers=excluded.price_tiers""",
        {
            "id": s["id"],
            "cinema_id": s["cinema_id"],
            "movie_id": s["movie_id"],
            "show_date": s["show_date"],
            "show_time": s.get("show_time"),
            "screen": s.get("screen"),
            "price_tiers": json.dumps(s.get("price_tiers") or []),
        },
    )


def add_snapshot(conn, snap: dict[str, Any]) -> None:
    row = {
        "session_id": snap["session_id"],
        "crawl_ts": snap["crawl_ts"],
        "avail_status": snap.get("avail_status"),
        "fill_label": snap.get("fill_label"),
        "occupancy_pct": snap.get("occupancy_pct"),
        "is_exact": 1 if snap.get("is_exact") else 0,
        "seats_total": snap.get("seats_total"),
        "seats_booked": snap.get("seats_booked"),
        "est_revenue": snap.get("est_revenue"),
    }
    conn.execute(
        """INSERT OR REPLACE INTO snapshots
           (session_id, crawl_ts, avail_status, fill_label, occupancy_pct,
            is_exact, seats_total, seats_booked, est_revenue)
           VALUES (:session_id, :crawl_ts, :avail_status, :fill_label, :occupancy_pct,
            :is_exact, :seats_total, :seats_booked, :est_revenue)""",
        row,
    )


def sessions_for_refresh(conn, show_date: str) -> Iterable[sqlite3.Row]:
    """Sessions on the given date, with their latest snapshot time (if any)."""
    return conn.execute(
        """SELECT s.*, MAX(sn.crawl_ts) AS last_crawl,
                  (SELECT occupancy_pct FROM snapshots
                    WHERE session_id = s.id ORDER BY crawl_ts DESC LIMIT 1) AS last_occ
           FROM sessions s
           LEFT JOIN snapshots sn ON sn.session_id = s.id
           WHERE s.show_date = ?
           GROUP BY s.id""",
        (show_date,),
    ).fetchall()
