"""Run the real parsers against the live APIs for the latest night with games.

    PYTHONPATH=src python scripts/check_sources.py [--source apinba]

Exits 1 if the source fails. Runs weekly in CI (source-contract.yml).
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heatcheck.dates import eastern_date, utc_now
from heatcheck.http import HttpClient
from heatcheck.sources import REQUEST_INTERVALS, build_sources, validate_day


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="espn", choices=["espn", "apinba"])
    parser.add_argument("--max-days-back", type=int, default=7, help="how far back to look for a night with games")
    args = parser.parse_args()

    import os

    http = HttpClient(min_interval=REQUEST_INTERVALS)
    sources = build_sources([args.source], http, apinba_key=os.environ.get("APINBA_KEY"))
    if not sources or sources[0].name != args.source:
        print(f"{args.source}: not configured (set APINBA_KEY for apinba)")
        return 1
    source = sources[0]

    today = eastern_date(utc_now())
    for back in range(1, args.max_days_back + 1):
        day = today - timedelta(days=back)
        try:
            result = source.fetch_day(day)
            validate_day(result)
        except Exception as err:  # noqa: BLE001 - report any failure
            print(f"FAIL {source.name} {day}: {type(err).__name__}: {err}")
            return 1
        if result.completed_games:
            sample = result.lines[0]
            print(
                f"OK   {source.name} {day}: {len(result.completed_games)} games, {len(result.lines)} lines; "
                f"e.g. {sample.player_name} ({sample.team}) {sample.pts} pts in {sample.minutes:g} min"
            )
            return 0
        print(f"..   {source.name} {day}: no finished games")
    print(f"No finished games in the last {args.max_days_back} days (off-season?); nothing to check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
