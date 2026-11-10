"""Ingest box scores, recompute streaks, publish streaks.json.

If every source fails for a night the site is still republished from stored
data, then the run raises so the alarm fires.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from .analytics import StreakParams, compute_streaks
from .dates import previous_season, season_for
from .models import DayResult, PlayerLine
from .names import NBA_TEAMS
from .sources import AllSourcesFailed, DataSource, fetch_with_fallback
from .storage import STREAKS_KEY, Storage, gamelog_key, season_prefix

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
STREAKS_CACHE_CONTROL = "public, max-age=300"


class PipelineError(Exception):
    """Raised after publishing when one or more nights could not be ingested."""


@dataclass
class RunReport:
    ingested: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[str, dict[str, str]] = field(default_factory=dict)
    fallback_days: int = 0
    lines_loaded: int = 0
    players_ranked: int = 0
    published: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ingested": self.ingested,
            "failures": self.failures,
            "fallback_days": self.fallback_days,
            "lines_loaded": self.lines_loaded,
            "players_ranked": self.players_ranked,
            "published": self.published,
        }


def ingest_day(day: date, sources: list[DataSource], store: Storage) -> tuple[DayResult, list[str]]:
    result, failed_first = fetch_with_fallback(day, sources)
    store.put_json(gamelog_key(day), result.to_dict())
    log.info(
        "ingested %s from %s: %d completed games, %d player lines",
        day,
        result.source,
        len(result.completed_games),
        len(result.lines),
    )
    return result, failed_first


def ingest(dates: list[date], sources: list[DataSource], store: Storage, report: RunReport) -> None:
    for day in dates:
        try:
            result, failed_first = ingest_day(day, sources, store)
        except AllSourcesFailed as err:
            log.error("%s", err)
            report.failures[day.isoformat()] = err.errors
            continue
        report.ingested[day.isoformat()] = {
            "source": result.source,
            "games": len(result.completed_games),
            "lines": len(result.lines),
            "failed_sources": failed_first,
        }
        if failed_first:
            report.fallback_days += 1


def load_lines(store: Storage, seasons: list[str]) -> list[PlayerLine]:
    """Every stored player line for the given seasons."""
    keys = [key for season in seasons for key in store.list_keys(season_prefix(season)) if key.endswith(".json")]
    lines: list[PlayerLine] = []
    for key, doc in store.get_many_json(keys).items():
        if not doc:
            continue
        try:
            lines.extend(DayResult.from_dict(doc).lines)
        except (KeyError, TypeError) as err:
            log.warning("skipping unreadable game log %s: %s", key, err)
    _warn_on_name_collisions(lines)
    return lines


def _warn_on_name_collisions(lines: list[PlayerLine]) -> None:
    ids: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for line in lines:
        ids[(line.date, line.source, line.player_key)].add(line.source_player_id)
    for (day, source, key), source_ids in ids.items():
        if len(source_ids) > 1:
            log.warning("name collision on %s (%s): %s maps to ids %s", day, source, key, sorted(source_ids))


def build_payload(
    lines: list[PlayerLine], *, as_of: date, now: datetime, report: RunReport, params: StreakParams
) -> dict[str, Any]:
    body = compute_streaks(lines, as_of=as_of, params=params)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "last_ingest": report.ingested,
        "teams": NBA_TEAMS,
        **body,
    }


def run_pipeline(
    *,
    dates: list[date],
    sources: list[DataSource],
    data_store: Storage,
    site_store: Storage,
    now: datetime,
    params: StreakParams | None = None,
    as_of: date | None = None,
) -> RunReport:
    """Ingest ``dates``, then recompute and publish ``data/streaks.json``."""
    params = params or StreakParams()
    report = RunReport()
    if dates:
        ingest(sorted(dates), sources, data_store, report)

    as_of = as_of or (max(dates) if dates else now.date())
    season = season_for(as_of)
    lines = load_lines(data_store, [previous_season(season), season])
    report.lines_loaded = len(lines)

    payload = build_payload(lines, as_of=as_of, now=now, report=report, params=params)
    site_store.put_json(STREAKS_KEY, payload, cache_control=STREAKS_CACHE_CONTROL)
    report.published = True
    report.players_ranked = len(payload["players"])
    log.info("published %s: %d players from %d lines", STREAKS_KEY, report.players_ranked, report.lines_loaded)
    return report
