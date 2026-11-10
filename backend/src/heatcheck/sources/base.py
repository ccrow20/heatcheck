"""The data-source interface, shared parsing helpers and the fallback chain."""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date
from typing import Any, Protocol

from ..models import DayResult

log = logging.getLogger(__name__)

# A completed NBA game should have at least this many players who logged
# minutes for each team. Fewer means the box score is missing or partial.
MIN_PLAYERS_PER_TEAM = 5


class SourceError(Exception):
    """A source could not produce trustworthy data for a date."""


class DataSource(Protocol):
    name: str

    def fetch_day(self, day: date) -> DayResult:
        """Return every completed game and player line for one Eastern date."""
        ...


class AllSourcesFailed(Exception):
    def __init__(self, day: date, errors: dict[str, str]):
        self.day = day
        self.errors = errors
        detail = "; ".join(f"{name}: {msg}" for name, msg in errors.items()) or "no sources configured"
        super().__init__(f"every data source failed for {day.isoformat()} ({detail})")


def validate_day(result: DayResult) -> None:
    """Reject results that look incomplete so the next source gets a chance."""
    per_team: Counter[tuple[str, str]] = Counter((ln.game_id, ln.team) for ln in result.lines)
    for game in result.completed_games:
        for team in (game.home, game.away):
            count = per_team[(game.game_id, team)]
            if count < MIN_PLAYERS_PER_TEAM:
                raise SourceError(f"{result.source} box score for {game.game_id} has {count} player lines for {team}")


def fetch_with_fallback(day: date, sources: list[DataSource]) -> tuple[DayResult, list[str]]:
    """Try each source in order and return the first valid result.

    Any exception moves on to the next source. Also returns the names of
    sources that failed first.
    """
    errors: dict[str, str] = {}
    for source in sources:
        try:
            result = source.fetch_day(day)
            validate_day(result)
        except Exception as err:  # noqa: BLE001 - any failure means "try the next source"
            errors[source.name] = f"{type(err).__name__}: {err}"
            log.warning("source %s failed for %s: %s", source.name, day, errors[source.name])
            continue
        if errors:
            log.warning("used fallback source %s for %s after %s failed", source.name, day, list(errors))
        return result, list(errors)
    raise AllSourcesFailed(day, errors)


# parsing helpers

_ISO_DURATION = re.compile(r"^PT(?:(\d+)M)?(?:([\d.]+)S)?$")


def parse_minutes(value: Any) -> float | None:
    """Parse minutes played from the formats sources use.

    Handles ``36``, ``"36"``, ``"36:12"`` and ISO durations like ``"PT35M12.00S"``.
    Returns None for blanks and placeholders such as ``"--"`` or ``"DNP"``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = _ISO_DURATION.match(text)
    if match:
        mins = int(match.group(1) or 0)
        secs = float(match.group(2) or 0)
        return round(mins + secs / 60, 2)
    if ":" in text:
        mins, _, secs = text.partition(":")
        if mins.isdigit() and secs.isdigit():
            return round(int(mins) + int(secs) / 60, 2)
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any, default: int = 0) -> int:
    """Parse an integer stat (``"7"``, ``7``, ``"+9"``); blanks become ``default``."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def to_optional_int(value: Any) -> int | None:
    sentinel = -(10**9)
    parsed = to_int(value, default=sentinel)
    return None if parsed == sentinel else parsed


def split_made_attempted(value: Any) -> tuple[int, int]:
    """``"10-21"`` -> ``(10, 21)``. Blanks become ``(0, 0)``."""
    if value is None:
        return 0, 0
    made, sep, attempted = str(value).strip().partition("-")
    if not sep:
        return 0, 0
    return to_int(made), to_int(attempted)
