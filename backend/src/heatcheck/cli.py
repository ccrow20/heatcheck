"""Local runs and backfills. Run from backend/ with PYTHONPATH=src.

    python -m heatcheck.cli ingest --start 2025-10-21 --end 2026-11-05 --data s3://BUCKET --skip-existing
    python -m heatcheck.cli publish --data s3://DATA_BUCKET --site s3://SITE_BUCKET
    python -m heatcheck.cli run           # whole pipeline against local folders
    python -m heatcheck.cli set-api-key   # store the API-NBA key in SSM

--data/--site take a folder or s3://bucket (defaults: .localdata/ and frontend/).
The API key comes from APINBA_KEY or APINBA_KEY_PARAM, never from argv.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from datetime import date
from pathlib import Path

from .config import clean_secret, read_secret_parameter
from .dates import date_range, nightly_target_dates, utc_now
from .http import HttpClient
from .pipeline import RunReport, ingest_day, run_pipeline
from .security import install_redaction
from .sources import REQUEST_INTERVALS, AllSourcesFailed, build_sources
from .storage import gamelog_key, open_storage

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = str(REPO_ROOT / ".localdata")
DEFAULT_SITE = str(REPO_ROOT / "frontend")
DEFAULT_KEY_PARAM = "/heatcheck/apinba-api-key"


def _apinba_key() -> str | None:
    key = clean_secret(os.environ.get("APINBA_KEY"))
    if key is None and os.environ.get("APINBA_KEY_PARAM"):
        key = read_secret_parameter(os.environ["APINBA_KEY_PARAM"])
    install_redaction(key)
    return key


def _sources(args: argparse.Namespace, http: HttpClient):
    names = [s for s in args.sources.split(",") if s.strip()]
    wants_apinba = "apinba" in [n.strip().lower() for n in names]
    return build_sources(names, http, apinba_key=_apinba_key() if wants_apinba else None)


def _ssm_client(region: str | None):
    import boto3  # installed by `make install`

    return boto3.client("ssm", region_name=region) if region else boto3.client("ssm")


def cmd_set_api_key(args: argparse.Namespace) -> int:
    """Store the API-NBA key as an encrypted SecureString in SSM Parameter Store."""
    if args.from_env:
        key = clean_secret(os.environ.get("APINBA_KEY"))
    else:
        key = clean_secret(getpass.getpass("API-NBA key (typing is hidden): "))
    if not key:
        print("No key given; nothing was changed.", file=sys.stderr)
        return 1
    install_redaction(key)
    _ssm_client(args.region).put_parameter(
        Name=args.name,
        Value=key,
        Type="SecureString",  # encrypted at rest with KMS (the AWS managed aws/ssm key)
        Overwrite=True,
        Description="API-NBA key for Heat Check (read by the nightly Lambda)",
    )
    print(f"Saved the key to SSM parameter {args.name} as an encrypted SecureString.")
    print("The next pipeline run will use it. To remove it: aws ssm delete-parameter --name " + args.name)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = open_storage(args.data)
    http = HttpClient(min_interval=REQUEST_INTERVALS)
    sources = _sources(args, http)
    days = date_range(date.fromisoformat(args.start), date.fromisoformat(args.end))
    failures = 0
    print(f"Ingesting {len(days)} dates with sources {[s.name for s in sources]} into {args.data}")
    for day in days:
        if args.skip_existing and store.get_json(gamelog_key(day)) is not None:
            print(f"{day}  skipped (already stored)")
            continue
        try:
            result, failed_first = ingest_day(day, sources, store)
        except AllSourcesFailed as err:
            failures += 1
            print(f"{day}  FAILED  {err.errors}", file=sys.stderr)
            continue
        note = f"  (fallback after {', '.join(failed_first)} failed)" if failed_first else ""
        print(f"{day}  {result.source:<7} {len(result.completed_games):>2} games  {len(result.lines):>4} lines{note}")
    print(f"Done: {len(days) - failures} ok, {failures} failed, {http.request_count} HTTP requests")
    return 1 if failures else 0


def cmd_publish(args: argparse.Namespace) -> int:
    as_of = date.fromisoformat(args.as_of) if args.as_of else nightly_target_dates(utc_now(), 1)[0]
    report = run_pipeline(
        dates=[],
        sources=[],
        data_store=open_storage(args.data),
        site_store=open_storage(args.site),
        now=utc_now(),
        as_of=as_of,
    )
    _print_report(report)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    http = HttpClient(min_interval=REQUEST_INTERVALS)
    dates = [date.fromisoformat(args.date)] if args.date else nightly_target_dates(utc_now(), args.lookback)
    report = run_pipeline(
        dates=dates,
        sources=_sources(args, http),
        data_store=open_storage(args.data),
        site_store=open_storage(args.site),
        now=utc_now(),
    )
    _print_report(report)
    return 0 if report.ok else 1


def _print_report(report: RunReport) -> None:
    for day, info in sorted(report.ingested.items()):
        print(f"{day}  {info['source']:<7} {info['games']:>2} games  {info['lines']:>4} lines")
    for day, errors in sorted(report.failures.items()):
        print(f"{day}  FAILED  {errors}", file=sys.stderr)
    print(f"Published streaks for {report.players_ranked} players from {report.lines_loaded} stored lines")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="heatcheck", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, site: bool, sources: bool) -> None:
        p.add_argument("--data", default=DEFAULT_DATA, help="game log storage: folder or s3://bucket")
        if site:
            p.add_argument("--site", default=DEFAULT_SITE, help="website storage: folder or s3://bucket")
        if sources:
            p.add_argument("--sources", default="espn,apinba", help="comma-separated sources in priority order")

    p_ingest = sub.add_parser("ingest", help="fetch and store box scores for a date range (backfill)")
    p_ingest.add_argument("--start", required=True, help="first date, YYYY-MM-DD (US Eastern)")
    p_ingest.add_argument("--end", required=True, help="last date, YYYY-MM-DD (inclusive)")
    p_ingest.add_argument("--skip-existing", action="store_true", help="skip dates already stored (resume)")
    add_common(p_ingest, site=False, sources=True)
    p_ingest.set_defaults(func=cmd_ingest)

    p_publish = sub.add_parser("publish", help="recompute streaks from stored data and publish streaks.json")
    p_publish.add_argument("--as-of", help="date to compute streaks as of (default: yesterday, Eastern)")
    add_common(p_publish, site=True, sources=False)
    p_publish.set_defaults(func=cmd_publish)

    p_run = sub.add_parser("run", help="what the Lambda does: ingest recent nights, then publish")
    p_run.add_argument("--date", help="ingest just this date instead of the lookback window")
    p_run.add_argument("--lookback", type=int, default=2, help="nights to (re)ingest, ending yesterday")
    add_common(p_run, site=True, sources=True)
    p_run.set_defaults(func=cmd_run)

    p_key = sub.add_parser("set-api-key", help="store the API-NBA key in SSM (prompts with hidden input)")
    p_key.add_argument("--name", default=DEFAULT_KEY_PARAM, help=f"SSM parameter name (default {DEFAULT_KEY_PARAM})")
    p_key.add_argument("--region", help="AWS region (default: your AWS CLI configuration)")
    p_key.add_argument("--from-env", action="store_true", help="read the key from APINBA_KEY instead of prompting")
    p_key.set_defaults(func=cmd_set_api_key)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
