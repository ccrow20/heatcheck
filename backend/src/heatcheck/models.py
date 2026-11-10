"""Source-independent data model. Every source converts to these."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class PlayerLine:
    """One player's box score line from one game."""

    date: str  # US Eastern game date, YYYY-MM-DD
    game_id: str  # "<source>:<source game id>"
    source: str
    player_key: str
    player_name: str
    source_player_id: str
    team: str
    opp: str
    home: bool
    minutes: float
    pts: int
    reb: int
    ast: int
    stl: int
    blk: int
    tov: int
    pf: int
    fgm: int
    fga: int
    fg3m: int
    fg3a: int
    ftm: int
    fta: int
    oreb: int | None = None
    dreb: int | None = None
    plus_minus: int | None = None
    starter: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlayerLine:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class GameSummary:
    """Scoreboard information for one game."""

    game_id: str
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    completed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameSummary:
        return cls(**data)


@dataclass
class DayResult:
    """Everything one source returned for one night of games."""

    date: str
    source: str
    fetched_at: str
    games: list[GameSummary] = field(default_factory=list)
    lines: list[PlayerLine] = field(default_factory=list)

    @property
    def completed_games(self) -> list[GameSummary]:
        return [g for g in self.games if g.completed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "date": self.date,
            "source": self.source,
            "fetched_at": self.fetched_at,
            "games": [g.to_dict() for g in self.games],
            "lines": [ln.to_dict() for ln in self.lines],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DayResult:
        return cls(
            date=data["date"],
            source=data["source"],
            fetched_at=data.get("fetched_at", ""),
            games=[GameSummary.from_dict(g) for g in data.get("games", [])],
            lines=[PlayerLine.from_dict(ln) for ln in data.get("lines", [])],
        )
