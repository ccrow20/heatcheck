"""ESPN's undocumented site API: scoreboard for game IDs, then a summary per game.

No key needed. Stats are read by key name rather than position, and anything
unexpected raises so the next source takes over.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from ..dates import utc_now
from ..models import DayResult, GameSummary, PlayerLine
from ..names import normalize_team, player_key
from .base import SourceError, parse_minutes, split_made_attempted, to_int, to_optional_int

log = logging.getLogger(__name__)

HOST = "site.api.espn.com"
SCOREBOARD_URL = f"https://{HOST}/apis/site/v2/sports/basketball/nba/scoreboard"
SUMMARY_URL = f"https://{HOST}/apis/site/v2/sports/basketball/nba/summary"

# ESPN season types: 1 preseason, 2 regular season, 3 postseason, 5 play-in.
INCLUDED_SEASON_TYPES = {2, 3, 5}

REQUIRED_KEYS = {
    "minutes",
    "points",
    "fieldGoalsMade-fieldGoalsAttempted",
    "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
    "freeThrowsMade-freeThrowsAttempted",
    "rebounds",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "fouls",
}

# Used only if a response lacks "keys" but has the short column labels.
LABEL_TO_KEY = {
    "MIN": "minutes",
    "PTS": "points",
    "FG": "fieldGoalsMade-fieldGoalsAttempted",
    "3PT": "threePointFieldGoalsMade-threePointFieldGoalsAttempted",
    "FT": "freeThrowsMade-freeThrowsAttempted",
    "REB": "rebounds",
    "OREB": "offensiveRebounds",
    "DREB": "defensiveRebounds",
    "AST": "assists",
    "STL": "steals",
    "BLK": "blocks",
    "TO": "turnovers",
    "PF": "fouls",
    "+/-": "plusMinus",
}


class EspnSource:
    name = "espn"

    def __init__(self, http: Any):
        self.http = http

    def fetch_day(self, day: date) -> DayResult:
        scoreboard, _ = self.http.get_json(SCOREBOARD_URL, params={"dates": day.strftime("%Y%m%d"), "limit": "100"})
        if not isinstance(scoreboard, dict) or "events" not in scoreboard:
            raise SourceError("ESPN scoreboard response has no 'events' field")

        games: list[GameSummary] = []
        lines: list[PlayerLine] = []
        for event in scoreboard.get("events") or []:
            game = parse_scoreboard_event(event)
            if game is None:
                continue
            games.append(game)
            if not game.completed:
                continue
            espn_id = game.game_id.split(":", 1)[1]
            summary, _ = self.http.get_json(SUMMARY_URL, params={"event": espn_id})
            lines.extend(parse_summary(summary, game=game, day=day))

        return DayResult(
            date=day.isoformat(),
            source=self.name,
            fetched_at=utc_now().isoformat(timespec="seconds"),
            games=games,
            lines=lines,
        )


def parse_scoreboard_event(event: dict[str, Any]) -> GameSummary | None:
    """Turn one scoreboard event into a GameSummary, or None if it should be skipped."""
    season_type = (event.get("season") or {}).get("type")
    if season_type is not None and to_int(season_type, default=-1) not in INCLUDED_SEASON_TYPES:
        return None

    competition = (event.get("competitions") or [None])[0]
    if not competition:
        raise SourceError(f"ESPN event {event.get('id')} has no competitions")

    sides: dict[str, tuple[str | None, int | None]] = {}
    for competitor in competition.get("competitors") or []:
        team = normalize_team((competitor.get("team") or {}).get("abbreviation"))
        sides[competitor.get("homeAway", "")] = (team, to_optional_int(competitor.get("score")))
    home, home_score = sides.get("home", (None, None))
    away, away_score = sides.get("away", (None, None))
    if home is None or away is None:
        return None  # All-Star game, exhibition vs. a non-NBA club, etc.

    status = ((event.get("status") or competition.get("status") or {}).get("type")) or {}
    completed = bool(status.get("completed")) or status.get("state") == "post"
    if status.get("name") in {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_SUSPENDED"}:
        completed = False

    return GameSummary(
        game_id=f"espn:{event['id']}",
        home=home,
        away=away,
        home_score=home_score,
        away_score=away_score,
        completed=completed,
    )


def parse_summary(summary: dict[str, Any], *, game: GameSummary, day: date) -> list[PlayerLine]:
    """Turn an ESPN game summary into player lines."""
    try:
        team_blocks = summary["boxscore"]["players"]
    except (KeyError, TypeError) as err:
        raise SourceError(f"ESPN summary for {game.game_id} has no boxscore.players") from err

    lines: list[PlayerLine] = []
    for block in team_blocks:
        team = normalize_team((block.get("team") or {}).get("abbreviation"))
        if team not in (game.home, game.away):
            raise SourceError(f"ESPN summary for {game.game_id} has unexpected team {team!r}")
        opp = game.away if team == game.home else game.home

        stat_groups = [g for g in block.get("statistics") or [] if g.get("athletes")]
        if not stat_groups:
            continue
        group = stat_groups[0]
        keys = list(group.get("keys") or [])
        if not keys:
            keys = [LABEL_TO_KEY.get(label, label) for label in group.get("labels") or group.get("names") or []]
        missing = REQUIRED_KEYS - set(keys)
        if missing:
            raise SourceError(f"ESPN box score format changed; missing stat keys {sorted(missing)}")

        for entry in group["athletes"]:
            line = _parse_athlete(entry, keys, game=game, day=day, team=team, opp=opp)
            if line is not None:
                lines.append(line)
    return lines


def _parse_athlete(
    entry: dict[str, Any], keys: list[str], *, game: GameSummary, day: date, team: str, opp: str
) -> PlayerLine | None:
    raw = entry.get("stats") or []
    if entry.get("didNotPlay") or not raw:
        return None
    stats = dict(zip(keys, raw, strict=True))  # a length mismatch means the format changed
    minutes = parse_minutes(stats.get("minutes"))
    if minutes is None or minutes <= 0:
        return None

    athlete = entry.get("athlete") or {}
    name = athlete.get("displayName") or athlete.get("shortName")
    if not name:
        log.warning("skipping unnamed athlete in %s", game.game_id)
        return None

    fgm, fga = split_made_attempted(stats["fieldGoalsMade-fieldGoalsAttempted"])
    fg3m, fg3a = split_made_attempted(stats["threePointFieldGoalsMade-threePointFieldGoalsAttempted"])
    ftm, fta = split_made_attempted(stats["freeThrowsMade-freeThrowsAttempted"])
    return PlayerLine(
        date=day.isoformat(),
        game_id=game.game_id,
        source="espn",
        player_key=player_key(name),
        player_name=name,
        source_player_id=str(athlete.get("id", "")),
        team=team,
        opp=opp,
        home=team == game.home,
        minutes=minutes,
        pts=to_int(stats["points"]),
        reb=to_int(stats["rebounds"]),
        ast=to_int(stats["assists"]),
        stl=to_int(stats["steals"]),
        blk=to_int(stats["blocks"]),
        tov=to_int(stats["turnovers"]),
        pf=to_int(stats["fouls"]),
        fgm=fgm,
        fga=fga,
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=ftm,
        fta=fta,
        oreb=to_optional_int(stats.get("offensiveRebounds")),
        dreb=to_optional_int(stats.get("defensiveRebounds")),
        plus_minus=to_optional_int(stats.get("plusMinus")),
        starter=entry.get("starter"),
    )
