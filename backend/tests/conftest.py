from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from heatcheck.models import DayResult, GameSummary, PlayerLine

FIXTURES = Path(__file__).parent / "fixtures"

# Twenty nights from a steady ~20 PPG scorer with realistic noise.
STEADY_SCORES = [18, 24, 15, 22, 27, 19, 14, 23, 21, 17, 25, 16, 20, 22, 18, 24, 13, 21, 19, 23]


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeHttp:
    """Answers get_json() from a {(url, frozenset(params)): payload} table."""

    def __init__(self, routes: dict[tuple[str, tuple[tuple[str, str], ...]], Any] | None = None, headers=None):
        self.routes = routes or {}
        self.headers = headers or {}
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def add(self, url: str, params: dict[str, str], payload: Any) -> FakeHttp:
        self.routes[(url, tuple(sorted(params.items())))] = payload
        return self

    def get_json(self, url, *, params=None, headers=None):
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        key = (url, tuple(sorted(params.items())))
        if key not in self.routes:
            raise AssertionError(f"unexpected request {url} {params}")
        payload = self.routes[key]
        if isinstance(payload, Exception):
            raise payload
        return payload, dict(self.headers)


@pytest.fixture
def fixture():
    return load_fixture


def make_line(
    player: str = "Test Player",
    day: date | str = "2026-11-05",
    *,
    pts: int = 20,
    reb: int = 5,
    ast: int = 4,
    fg3m: int = 2,
    minutes: float = 32.0,
    team: str = "BOS",
    opp: str = "NYK",
    source: str = "espn",
    game_id: str | None = None,
    source_player_id: str = "1",
) -> PlayerLine:
    """A consistent box score line: FG/FT numbers are derived from points."""
    day_iso = day if isinstance(day, str) else day.isoformat()
    from heatcheck.names import player_key

    ftm = pts % 2
    two_pt_points = max(pts - 3 * fg3m - ftm, 0)
    fgm = fg3m + two_pt_points // 2
    ftm += two_pt_points % 2
    return PlayerLine(
        date=day_iso,
        game_id=game_id or f"{source}:{day_iso}-{team}",
        source=source,
        player_key=player_key(player),
        player_name=player,
        source_player_id=source_player_id,
        team=team,
        opp=opp,
        home=True,
        minutes=minutes,
        pts=pts,
        reb=reb,
        ast=ast,
        stl=1,
        blk=0,
        tov=2,
        pf=2,
        fgm=fgm,
        fga=fgm * 2,
        fg3m=fg3m,
        fg3a=fg3m * 2,
        ftm=ftm,
        fta=ftm,
        oreb=reb // 4,
        dreb=reb - reb // 4,
        plus_minus=0,
    )


def series(
    player: str, values: list[int], *, end: date = date(2026, 11, 5), stat: str = "pts", **kwargs
) -> list[PlayerLine]:
    """One line per day ending at ``end`` with ``stat`` taking ``values`` in order."""
    start = end - timedelta(days=len(values) - 1)
    return [make_line(player, start + timedelta(days=i), **{stat: v}, **kwargs) for i, v in enumerate(values)]


def day_result(day: str, lines: list[PlayerLine], source: str = "fake") -> DayResult:
    games = {}
    for line in lines:
        home, away = (line.team, line.opp) if line.home else (line.opp, line.team)
        games[line.game_id] = GameSummary(line.game_id, home, away, 100, 90, True)
    return DayResult(
        date=day, source=source, fetched_at="2026-11-06T10:00:00+00:00", games=list(games.values()), lines=lines
    )


def full_game(day: str, game_id: str, home: str = "BOS", away: str = "NYK", source: str = "fake") -> list[PlayerLine]:
    """Five players per team, enough to pass validate_day()."""
    lines = []
    for team, opp in ((home, away), (away, home)):
        for i in range(5):
            line = make_line(f"{team} Player {i}", day, team=team, opp=opp, source=source, game_id=game_id)
            lines.append(PlayerLine(**{**line.to_dict(), "home": team == home}))
    return lines
