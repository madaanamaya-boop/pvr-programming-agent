"""PVR Programming Intelligence — CLI entrypoint.

Usage:
    python main.py --login                 # seed Delhi-NCR region session (headed, once)
    python main.py --pass full             # full discovery + occupancy pass
    python main.py --pass full --limit 3   # limit to 3 movies (smoke test)
    python main.py --pass refresh          # re-poll due seat maps (velocity)
    python main.py --demo                  # seed synthetic data to exercise dashboard
"""
from __future__ import annotations

import argparse
import sys

from src.logger import get_logger

log = get_logger("main")


def main() -> int:
    ap = argparse.ArgumentParser(description="PVR Programming Intelligence agent")
    ap.add_argument("--login", action="store_true", help="seed region session (headed)")
    ap.add_argument("--pass", dest="pass_kind", choices=["full", "refresh", "cinepolis"])
    ap.add_argument("--demo", action="store_true", help="seed synthetic data")
    ap.add_argument("--date", help="show date YYYY-MM-DD (default today)")
    ap.add_argument("--limit", type=int, help="limit number of movies (smoke test)")
    ap.add_argument("--budget", type=int, default=0,
                    help="exact seat-map fetches this pass (0 = availStatus-only, the cheap path)")
    args = ap.parse_args()

    if args.login:
        from src.crawler.session import seed_region_interactive
        seed_region_interactive()
        return 0

    if args.demo:
        from src.pipeline import seed_demo
        print(seed_demo())
        return 0

    if args.pass_kind == "full":
        from src.pipeline import run_full
        print(run_full(show_date=args.date, limit_movies=args.limit, seatmap_budget=args.budget))
        return 0

    if args.pass_kind == "refresh":
        from src.pipeline import run_refresh
        print(run_refresh(show_date=args.date, seatmap_budget=args.budget))
        return 0

    if args.pass_kind == "cinepolis":
        from src.pipeline import run_cinepolis
        print(run_cinepolis(show_date=args.date))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
