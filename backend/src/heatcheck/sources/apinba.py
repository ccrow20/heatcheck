"""API-NBA (api-sports.io). Free plan: 100 requests/day, 10/min, resets 00:00 UTC.

/games?date= filters on UTC start time, so this fetches the Eastern date plus
the next day and keeps games that start on the Eastern date.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from ..dates import eastern_date, parse_iso_datetime, utc_now
from ..models import DayResult, GameSummary, PlayerLine
from ..names import normalize_team, player_key
from .base import SourceError, parse_minutes, to_int, to_optional_int

log = logging.getLogger(__name__)

HOST = "v2.nba.api-sports.io"
BASE_URL = f"https://{HOST}"
FINISHED = 3  # status.short: 1 not started, 2 live, 3 finished


class ApiNbaSource:
    name = "apinba"

    def __init__(self, http: Any, api_key: str, *, league: str = "standard"):
        if not api_key:
            raise ValueError("ApiNbaSource needs an API key")
        self.http = http
        self.api_key = api_key
        self.league = league

    def _get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        data, headers = self.http.get_json(
            f"{BASE_URL}{path}", params=params, headers={"x-apisports-key": self.api_key}
        )
        if not isinstance(data, dict):
            raise SourceError(f"API-NBA {path} returned a non-object response")
        errors = data.get("errors")
        if errors:  # [] or {} when everything is fine
            raise SourceError(f"API-NBA {path} error: {errors}")
        remaining = headers.get("x-ratelimit-requests-remaining")
        if remaining is not None and to_int(remaining, default=1) <= 0:
            log.warning("API-NBA daily quota is used up")
        response = data.get("response")
        if not isinstance(response, list):
            raise SourceError(f"API-NBA {path} response has no 'response' list")
        return response

    def fetch_day(self, day: date) -> DayResult:
        raw_games: dict[int, dict[str, Any]] = {}
        for utc_day in (day, day + timedelta(days=1)):
            for game in self._get("/games", {"date": utc_day.isoformat()}):
                raw_games[game["id"]] = game

        games: list[GameSummary] = []
        lines: list[PlayerLine] = []
        for raw in sorted(raw_games.values(), key=lambda g: g["id"]):
            game = parse_game(raw, day=day, league=self.league)
            if game is None:
                continue
            games.append(game)
            if not game.completed:
                continue
            rows = self._get("/players/statistics", {"game": game.game_id.split(":", 1)[1]})
            lines.extend(parse_player_rows(rows, game=game, day=day))

        return DayResult(
            date=day.isoformat(),
            source=self.name,
            fetched_at=utc_now().isoformat(timespec="seconds"),
            games=games,
            lines=lines,
        )


def parse_game(raw: dict[str, Any], *, day: date, league: str = "standard") -> GameSummary | None:
    """GameSummary for a game on the Eastern date ``day``, or None to skip it."""
    if raw.get("league") not in (None, league):
        return None
    start = (raw.get("date") or {}).get("start")
    if not start:
        raise SourceError(f"API-NBA game {raw.get('id')} has no date.start")
    if eastern_date(parse_iso_datetime(start)) != day:
        return None

    teams = raw.get("teams") or {}
    home = normalize_team((teams.get("home") or {}).get("code"))
    away = normalize_team((teams.get("visitors") or {}).get("code"))
    if home is None or away is None:
        return None

    scores = raw.get("scores") or {}
    status = raw.get("status") or {}
    return GameSummary(
        game_id=f"apinba:{raw['id']}",
        home=home,
        away=away,
        home_score=to_optional_int((scores.get("home") or {}).get("points")),
        away_score=to_optional_int((scores.get("visitors") or {}).get("points")),
        completed=to_int(status.get("short"), default=0) == FINISHED,
    )


def parse_player_rows(rows: list[dict[str, Any]], *, game: GameSummary, day: date) -> list[PlayerLine]:
    lines: list[PlayerLine] = []
    seen: set[str] = set()
    for row in rows:
        minutes = parse_minutes(row.get("min"))
        if minutes is None or minutes <= 0:
            continue
        team = normalize_team((row.get("team") or {}).get("code"))
        if team not in (game.home, game.away):
            raise SourceError(f"API-NBA stats for {game.game_id} include unexpected team {team!r}")
        player = row.get("player") or {}
        name = " ".join(p for p in (player.get("firstname"), player.get("lastname")) if p).strip()
        if not name:
            continue
        player_id = str(player.get("id", ""))
        if player_id and player_id in seen:  # the API occasionally repeats a row
            continue
        seen.add(player_id)
        lines.append(
            PlayerLine(
                date=day.isoformat(),
                game_id=game.game_id,
                source="apinba",
                player_key=player_key(name),
                player_name=name,
                source_player_id=player_id,
                team=team,
                opp=game.away if team == game.home else game.home,
                home=team == game.home,
                minutes=minutes,
                pts=to_int(row.get("points")),
                reb=to_int(row.get("totReb")),
                ast=to_int(row.get("assists")),
                stl=to_int(row.get("steals")),
                blk=to_int(row.get("blocks")),
                tov=to_int(row.get("turnovers")),
                pf=to_int(row.get("pFouls")),
                fgm=to_int(row.get("fgm")),
                fga=to_int(row.get("fga")),
                fg3m=to_int(row.get("tpm")),
                fg3a=to_int(row.get("tpa")),
                ftm=to_int(row.get("ftm")),
                fta=to_int(row.get("fta")),
                oreb=to_optional_int(row.get("offReb")),
                dreb=to_optional_int(row.get("defReb")),
                plus_minus=to_optional_int(row.get("plusMinus")),
                starter=None,
            )
        )
    return lines
