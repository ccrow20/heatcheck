"""Lambda entry point.

The schedule sends an empty event. For manual runs pass {"dates": [...]} or
{"lookback_days": N}, capped at MAX_DATES_PER_RUN. Use the CLI for backfills.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from .config import Config, resolve_apinba_key
from .dates import nightly_target_dates, utc_now
from .http import HttpClient
from .pipeline import PipelineError, RunReport, run_pipeline
from .security import install_redaction
from .sources import REQUEST_INTERVALS, build_sources
from .storage import S3Storage
from .telemetry import emit_metrics

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

MAX_DATES_PER_RUN = 14


def _dates_from_event(event: dict[str, Any], lookback_days: int) -> list[date]:
    if event.get("dates"):
        raw = event["dates"]
        if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
            raise ValueError('"dates" must be a list of YYYY-MM-DD strings')
        dates = sorted({date.fromisoformat(d) for d in raw})
        if len(dates) > MAX_DATES_PER_RUN:
            raise ValueError(f"at most {MAX_DATES_PER_RUN} dates per run; use the CLI's ingest command for backfills")
        return dates
    lookback = int(event.get("lookback_days", lookback_days))
    if not 1 <= lookback <= MAX_DATES_PER_RUN:
        raise ValueError(f"lookback_days must be between 1 and {MAX_DATES_PER_RUN}")
    return nightly_target_dates(utc_now(), lookback)


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    event = event or {}
    config = Config.from_env()
    dates = _dates_from_event(event, config.lookback_days)
    apinba_key = resolve_apinba_key(config)
    install_redaction(apinba_key)  # nothing logged from here on can contain the key
    http = HttpClient(min_interval=REQUEST_INTERVALS)
    sources = build_sources(config.sources, http, apinba_key=apinba_key)
    log.info("run starting: dates=%s sources=%s", [d.isoformat() for d in dates], [s.name for s in sources])

    try:
        report = run_pipeline(
            dates=dates,
            sources=sources,
            data_store=S3Storage(config.data_bucket),
            site_store=S3Storage(config.site_bucket),
            now=utc_now(),
        )
    except Exception:
        emit_metrics({"PipelineSuccess": 0, "DaysFailed": len(dates)}, project=config.project)
        raise

    _emit(report, config.project, http.request_count)
    if not report.ok:
        # Published from existing data, but surface the failure so the alarm fires.
        raise PipelineError(f"ingestion failed for {sorted(report.failures)}: {report.failures}")
    return report.to_dict()


def _emit(report: RunReport, project: str, requests: int) -> None:
    emit_metrics(
        {
            "PipelineSuccess": 1 if report.ok else 0,
            "DaysFailed": len(report.failures),
            "FallbackDays": report.fallback_days,
            "LinesIngested": sum(day["lines"] for day in report.ingested.values()),
            "PlayersRanked": report.players_ranked,
            "HttpRequests": requests,
        },
        project=project,
    )
