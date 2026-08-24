"""Live BMS capture probe + payload dumper.

Drives a real Playwright browser to a BookMyShow showtimes URL, records EVERY
JSON response the page fetches, and writes them to tools/dumps/ so we can see the
real field names and tune the parsers. Also prints a compact structural summary.

Usage:
    python tools/capture_dump.py                       # uses a default NCR movie listing
    python tools/capture_dump.py --url "<buytickets url>"
    python tools/capture_dump.py --headed              # watch it / pass a bot check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import config  # noqa: E402

DUMP_DIR = Path(__file__).resolve().parent / "dumps"
DUMP_DIR.mkdir(exist_ok=True)


def summarize(obj, depth=0, max_depth=4):
    """Return a shallow shape summary: keys + value types, recursing a little."""
    pad = "  " * depth
    if depth > max_depth:
        return pad + "…"
    if isinstance(obj, dict):
        lines = []
        for k, v in list(obj.items())[:40]:
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(summarize(v, depth + 1, max_depth))
            else:
                lines.append(f"{pad}{k}: {type(v).__name__}={str(v)[:40]}")
        return "\n".join(lines)
    if isinstance(obj, list):
        return f"{pad}[{len(obj)} items]\n" + (summarize(obj[0], depth + 1, max_depth) if obj else "")
    return pad + type(obj).__name__


def main():
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="BMS URL to load")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    url = args.url or f"{config.BMS_BASE}/explore/movies-{config.CITY_SLUG}"
    hits = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed,
                                    args=["--disable-blink-features=AutomationControlled"])
        ctx_kw = {"user_agent": config.USER_AGENTS[0], "locale": "en-IN",
                  "timezone_id": "Asia/Kolkata", "viewport": {"width": 1440, "height": 900}}
        if config.STORAGE_STATE.exists():
            ctx_kw["storage_state"] = str(config.STORAGE_STATE)
        ctx = browser.new_context(**ctx_kw)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()

        def on_resp(resp):
            try:
                if "json" not in resp.headers.get("content-type", ""):
                    return
                body = resp.json()
            except Exception:
                return
            if isinstance(body, (dict, list)):
                hits.append((resp.url, body))

        page.on("response", on_resp)
        print(f"Loading {url}")
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except Exception as exc:
            print("nav warning:", exc)
        for _ in range(5):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1200)
        print(f"\nCaptured {len(hits)} JSON responses:\n")
        for i, (u, body) in enumerate(hits):
            fn = DUMP_DIR / f"resp_{i:02d}.json"
            fn.write_text(json.dumps(body, ensure_ascii=False, indent=2)[:2_000_000])
            interesting = any(k in str(body)[:3000] for k in
                              ("Venue", "Session", "ShowTime", "Categor", "Seat", "Event"))
            flag = "  <-- looks relevant" if interesting else ""
            print(f"[{i:02d}] {u[:90]}{flag}")
        # Print summary of the most relevant dumps.
        for i, (u, body) in enumerate(hits):
            if any(k in str(body)[:3000] for k in ("VenueCode", "SessionId", "Categor", "SeatsAvail")):
                print(f"\n===== resp_{i:02d} shape ({u[:70]}) =====")
                print(summarize(body)[:4000])
        ctx.storage_state(path=str(config.STORAGE_STATE))
        ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
