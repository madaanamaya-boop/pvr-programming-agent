"""Step 1 — discover every movie playing in Delhi-NCR.

BookMyShow's movies-listing page fetches its catalog from
    /api/explore/v1/discover/movies-<city-slug>
which returns `listings[].cards[]`. Real movie cards carry:
    card["id"]                     -> event GROUP code (EGxx…)
    card["analytics"]["event_code"]-> event code (ETxx…) used in showtimes URLs
    card["seoText"] / analytics.title -> title
    card["ctaUrl"]                 -> movie page URL (carries the slug)

We read that JSON straight off the wire (capture.py), so no HTML scraping and
no hardcoded field paths beyond these keys.
"""
from __future__ import annotations

import re

from src import config
from src.logger import get_logger

log = get_logger("discover")


def _is_discover(url: str, body) -> bool:
    return "/discover/movies-" in url and isinstance(body, dict) and "listings" in body


def discover_movies(crawler) -> list[dict]:
    url = f"{config.BMS_BASE}/explore/movies-{config.CITY_SLUG}"
    log.info("Discovering movies: %s", url)
    # Movie cards hydrate client-side and lazy-load on scroll, so wait + scroll.
    html = crawler.fetch_html(url, settle=6.0, scrolls=8)
    movies: dict[str, dict] = {}
    for mv in _parse_html(html):
        movies.setdefault(mv["id"], mv)
    log.info("Discovered %d movies in %s", len(movies), config.CITY_NAME)
    return list(movies.values())


def _parse_html(html: str) -> list[dict]:
    """Extract movie cards from the listing HTML (fresh SSR load).

    Titles + event codes come from each card's analytics block
    ("title":"…" … "event_code":"ET…"); slugs from the card's anchor href.
    """
    # Primary: anchor hrefs /movies/<city>/<slug>/<ETcode> — always present.
    pairs = re.findall(
        r"/movies/" + re.escape(config.CITY_SLUG) + r"/([a-z0-9-]+)/(ET\d+)", html)
    # Better titles from analytics blocks when they've hydrated.
    title_by_code = dict(
        (code, title.strip()) for title, code in re.findall(
            r'"title":"([^"]+)","screen_name":"movies_listing"[^}]*?"event_code":"(ET\d+)"',
            html))
    out, seen = [], set()
    for slug, code in pairs:
        if code in seen:
            continue
        seen.add(code)
        out.append({"id": code, "slug": slug,
                    "title": title_by_code.get(code) or _title_from_slug(slug),
                    "group_code": None})
    return out


def _title_from_slug(slug: str) -> str:
    return re.sub(r"-", " ", slug).title()


def _extract_cards(body: dict) -> list[dict]:
    out = []
    for listing in body.get("listings", []) or []:
        for card in listing.get("cards", []) or []:
            analytics = card.get("analytics") or {}
            event_code = analytics.get("event_code")
            title = card.get("seoText") or analytics.get("title")
            cta = card.get("ctaUrl") or ""
            # A real movie card has an ET event code and a movies/ CTA.
            if not event_code or not title:
                continue
            if "/movies/" not in cta and "movie" not in (card.get("styleId") or "").lower():
                continue
            out.append({
                "id": str(event_code),
                "group_code": card.get("id"),
                "title": str(title).strip(),
                "slug": _slug_from_cta(cta) or _slugify(title),
            })
    return out


def _slug_from_cta(cta: str) -> str | None:
    # .../movies/<city-slug>/<movie-slug>/ET00...
    m = re.search(r"/movies/[^/]+/([^/]+)/ET", cta)
    return m.group(1) if m else None


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "movie"
