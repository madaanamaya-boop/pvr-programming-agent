"""Browser crawler with a fresh context per fetch.

BookMyShow challenges the *second* full-page navigation inside any single
browser session (first load = full 2.8 MB HTML, second = a 4 KB stripped shell),
regardless of what you did in between. The block is keyed to the session
fingerprint, not the IP — so the fix is a **fresh browser context per fetch**:
one long-lived browser, a throwaway context (new fingerprint/cookies) for each
page we load. Verified to work across many sequential fetches on one IP.

Usage:
    with Crawler() as cr:
        html = cr.fetch_html(url, marker="venueName")   # SSR pages
        hits = cr.capture_json(url, predicate)           # XHR JSON pages
"""
from __future__ import annotations

import random
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

from src import config
from src.crawler.capture import ResponseCollector
from src.logger import get_logger

log = get_logger("session")

_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"


class Crawler:
    def __init__(self, headless: bool | None = None):
        self.headless = config.HEADLESS if headless is None else headless
        self._pw = None
        self._browser = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    @contextmanager
    def _context(self):
        ctx = self._browser.new_context(
            user_agent=random.choice(config.USER_AGENTS),
            viewport={"width": 1440, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        ctx.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)
        ctx.add_init_script(_STEALTH)
        # Pin the region so we never hit the "select your city" interstitial.
        import urllib.parse
        ctx.add_cookies([{
            "name": "rgn",
            "value": urllib.parse.quote(config.REGION_COOKIE_VALUE),
            "domain": ".bookmyshow.com",
            "path": "/",
        }])
        try:
            yield ctx
        finally:
            ctx.close()

    def fetch_html(self, url: str, marker: str | None = None, settle: float = 3.0,
                   scrolls: int = 0) -> str:
        """Load an SSR/hydrating page in a fresh context and return rendered HTML.
        `marker` waits until that text appears; `scrolls` pulls in lazy content."""
        with self._context() as ctx:
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")
                if marker:
                    try:
                        page.wait_for_function(
                            f"document.documentElement.outerHTML.includes({marker!r})",
                            timeout=15000)
                    except Exception:
                        pass  # page may legitimately have no shows
                time.sleep(settle)
                for _ in range(scrolls):
                    page.mouse.wheel(0, 4000)
                    time.sleep(1.0)
                return page.content()
            except Exception as exc:
                log.warning("fetch_html failed for %s: %s", url[:80], exc)
                return ""

    def capture_json(self, url: str, predicate, scrolls: int = 4) -> list[dict]:
        """Load a page in a fresh context, snoop JSON responses matching predicate,
        and also return the final HTML so callers can fall back to SSR parsing."""
        with self._context() as ctx:
            page = ctx.new_page()
            collector = ResponseCollector(page, predicate)
            try:
                page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                log.warning("capture_json nav warning: %s", exc)
            for _ in range(scrolls):
                page.mouse.wheel(0, 3500)
                time.sleep(1.0)
            hits = list(collector.hits)
            html = page.content()
            collector.detach()
            return hits, html


def seed_region_interactive() -> None:
    """Kept for parity; the fresh-context crawler needs no seeded region because
    the city is pinned in the URL slug. Left as a no-op helper."""
    print("Region is pinned via the URL slug (config.CITY_SLUG); no seeding needed.")
