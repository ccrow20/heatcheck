"""Eastern game dates and season labels.

Falls back to hardcoded US DST rules if zoneinfo has no tz database (some
Lambda images don't ship one).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

try:  # pragma: no cover - depends on the host
    from zoneinfo import ZoneInfo

    _EASTERN = ZoneInfo("America/New_York")
except (ImportError, KeyError, OSError):  # pragma: no cover - no tz database on this host
    _EASTERN = None

_EST = timedelta(hours=-5)
_EDT = timedelta(hours=-4)


def _nth_sunday(year: int, month: int, n: int) -> date:
    """Return the n-th Sunday (1-based) of a month."""
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7  # Monday=0 ... Sunday=6
    return first + timedelta(days=offset + 7 * (n - 1))


def eastern_offset_rule_based(moment: datetime) -> timedelta:
    """UTC offset for US Eastern time at ``moment`` using post-2007 DST rules.

    DST starts on the second Sunday of March at 02:00 EST (07:00 UTC) and ends
    on the first Sunday of November at 02:00 EDT (06:00 UTC).
    """
    utc = moment.astimezone(UTC)
    year = utc.year
    dst_start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time(), UTC) + timedelta(hours=7)
    dst_end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time(), UTC) + timedelta(hours=6)
    return _EDT if dst_start <= utc < dst_end else _EST


def to_eastern(moment: datetime) -> datetime:
    """Convert an aware datetime to US Eastern time."""
    if moment.tzinfo is None:
        raise ValueError("to_eastern() needs a timezone-aware datetime")
    if _EASTERN is not None:
        return moment.astimezone(_EASTERN)
    return moment.astimezone(timezone(eastern_offset_rule_based(moment)))


def eastern_date(moment: datetime) -> date:
    """The US Eastern calendar date of an aware datetime."""
    return to_eastern(moment).date()


def parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO-8601 timestamp such as ``2025-10-07T23:00:00.000Z``."""
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def utc_now() -> datetime:
    return datetime.now(UTC)


def season_start_year(day: date) -> int:
    """NBA seasons start in October, so Aug-Dec belongs to the season starting that year."""
    return day.year if day.month >= 8 else day.year - 1


def season_for(day: date) -> str:
    """Season label for a date, e.g. ``2026-11-05`` -> ``"2026-27"``."""
    start = season_start_year(day)
    return f"{start}-{(start + 1) % 100:02d}"


def previous_season(season: str) -> str:
    """``"2026-27"`` -> ``"2025-26"``."""
    start = int(season.split("-")[0]) - 1
    return f"{start}-{(start + 1) % 100:02d}"


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end``."""
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def nightly_target_dates(now: datetime, lookback_days: int) -> list[date]:
    """Dates the nightly job should (re)ingest, oldest first.

    lookback_days > 1 re-fetches earlier nights to pick up stat corrections.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    yesterday = eastern_date(now) - timedelta(days=1)
    return [yesterday - timedelta(days=i) for i in reversed(range(lookback_days))]
