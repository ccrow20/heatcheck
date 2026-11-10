"""Hot/cold scoring.

    z = (recent_mean - baseline_mean) / (sd_eff / sqrt(window))
    sd_eff^2 = (n * sd_player^2 + k * sd_league^2) / (n + k)

sd_eff is shrunk toward the league SD so players with tiny baseline spreads
don't blow up. Hot needs z >= threshold and recent mean >= floor; cold needs
z <= -threshold and baseline mean >= floor.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from .models import PlayerLine

log = logging.getLogger(__name__)

HOT, COLD, NEUTRAL = "hot", "cold", "neutral"


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    short: str
    floor: float  # minimum per-game average for the stat to count as relevant


METRICS: tuple[Metric, ...] = (
    Metric("gmsc", "Game Score", "GmSc", floor=8.0),
    Metric("pts", "Points", "PTS", floor=10.0),
    Metric("reb", "Rebounds", "REB", floor=4.0),
    Metric("ast", "Assists", "AST", floor=3.0),
    Metric("fg3m", "3-Pointers Made", "3PM", floor=1.5),
)


@dataclass(frozen=True)
class StreakParams:
    window: int = 5  # games in the "recent" window
    baseline_max: int = 30  # most games used for the baseline
    baseline_min: int = 8  # fewest baseline games before a player is ranked
    min_minutes: float = 10.0  # games under this are ignored (garbage time, early injury exits)
    max_days_inactive: int = 10  # players who haven't played in this long drop off the board
    threshold: float = 1.5  # |z| needed to be called hot or cold
    prior_strength: float = 5.0  # k in the shrinkage formula, in "games"
    history_games: int = 15  # games included per player for the website's sparklines

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be at least 2 games")
        if self.baseline_min < 2 or self.baseline_max < self.baseline_min:
            raise ValueError("need 2 <= baseline_min <= baseline_max")


def game_score(line: PlayerLine) -> float:
    """John Hollinger's Game Score, a one-number summary of a box score line.

    GmSc = PTS + 0.4 FGM - 0.7 FGA - 0.4 (FTA - FTM) + 0.7 ORB + 0.3 DRB
           + STL + 0.7 AST + 0.7 BLK - 0.4 PF - TOV

    When a source does not split rebounds, total rebounds are weighted 0.4,
    which equals the formula's weights at a typical 25% offensive share.
    """
    has_split = line.oreb is not None and line.dreb is not None
    rebounds = 0.7 * line.oreb + 0.3 * line.dreb if has_split else 0.4 * line.reb
    return (
        line.pts
        + 0.4 * line.fgm
        - 0.7 * line.fga
        - 0.4 * (line.fta - line.ftm)
        + rebounds
        + line.stl
        + 0.7 * line.ast
        + 0.7 * line.blk
        - 0.4 * line.pf
        - line.tov
    )


def metric_value(line: PlayerLine, key: str) -> float:
    if key == "gmsc":
        return game_score(line)
    return float(getattr(line, key))


def _dedupe(lines: list[PlayerLine]) -> list[PlayerLine]:
    """One line per date, oldest first. Two lines on one date means two
    different players share a name key, so keep the bigger role and warn."""
    by_date: dict[str, PlayerLine] = {}
    for line in lines:
        existing = by_date.get(line.date)
        if existing is None:
            by_date[line.date] = line
            continue
        if existing.game_id != line.game_id or existing.source_player_id != line.source_player_id:
            log.warning(
                "player key %s has two lines on %s (%s, %s); keeping the larger minutes",
                line.player_key,
                line.date,
                existing.game_id,
                line.game_id,
            )
        if line.minutes > existing.minutes:
            by_date[line.date] = line
    return [by_date[d] for d in sorted(by_date)]


def _run_length(values: list[float], baseline: float) -> int:
    """Consecutive most-recent games above (+) or below (-) the baseline."""
    if not values or values[-1] == baseline:
        return 0
    sign = 1 if values[-1] > baseline else -1
    run = 0
    for value in reversed(values):
        if (value - baseline) * sign > 0:
            run += 1
        else:
            break
    return sign * run


def classify(z: float, recent: float, baseline: float, floor: float, threshold: float) -> str:
    if z >= threshold and recent >= floor:
        return HOT
    if z <= -threshold and baseline >= floor:
        return COLD
    return NEUTRAL


def compute_streaks(
    lines: Iterable[PlayerLine],
    *,
    as_of: date,
    params: StreakParams | None = None,
) -> dict[str, Any]:
    """Score every eligible player. Returns the JSON-ready body of streaks.json."""
    params = params or StreakParams()
    as_of_iso = as_of.isoformat()

    by_player: dict[str, list[PlayerLine]] = defaultdict(list)
    for line in lines:
        if line.date <= as_of_iso and line.minutes >= params.min_minutes:
            by_player[line.player_key].append(line)

    needed = params.window + params.baseline_min
    candidates: list[tuple[list[PlayerLine], list[PlayerLine], list[PlayerLine]]] = []
    for player_lines in by_player.values():
        games = _dedupe(player_lines)
        if len(games) < needed:
            continue
        if (as_of - date.fromisoformat(games[-1].date)).days > params.max_days_inactive:
            continue
        recent = games[-params.window :]
        baseline = games[-(params.window + params.baseline_max) : -params.window]
        candidates.append((games, recent, baseline))

    # League-typical game-to-game spread for each stat (the shrinkage target).
    league_sd: dict[str, float] = {}
    for metric in METRICS:
        spreads = [statistics.stdev([metric_value(g, metric.key) for g in baseline]) for _, _, baseline in candidates]
        league_sd[metric.key] = statistics.median(spreads) if spreads else 1.0

    players = [_score_player(games, recent, baseline, league_sd, params) for games, recent, baseline in candidates]
    players.sort(key=lambda p: (p["name"].split()[-1].lower(), p["name"].lower()))

    summary = {
        metric.key: {
            HOT: sum(1 for p in players if p["metrics"][metric.key]["status"] == HOT),
            COLD: sum(1 for p in players if p["metrics"][metric.key]["status"] == COLD),
        }
        for metric in METRICS
    }
    return {
        "as_of": as_of_iso,
        "params": asdict(params),
        "metrics": [asdict(m) for m in METRICS],
        "league_sd": {k: round(v, 3) for k, v in league_sd.items()},
        "summary": summary,
        "players": players,
    }


def _score_player(
    games: list[PlayerLine],
    recent: list[PlayerLine],
    baseline: list[PlayerLine],
    league_sd: dict[str, float],
    params: StreakParams,
) -> dict[str, Any]:
    latest = games[-1]
    n_recent, n_base = len(recent), len(baseline)
    k = params.prior_strength

    metrics: dict[str, Any] = {}
    for metric in METRICS:
        recent_vals = [metric_value(g, metric.key) for g in recent]
        base_vals = [metric_value(g, metric.key) for g in baseline]
        recent_mean = statistics.fmean(recent_vals)
        base_mean = statistics.fmean(base_vals)
        player_sd = statistics.stdev(base_vals)
        prior_sd = league_sd[metric.key]
        sd_eff = math.sqrt((n_base * player_sd**2 + k * prior_sd**2) / (n_base + k))
        stderr = sd_eff / math.sqrt(n_recent)
        z = (recent_mean - base_mean) / stderr if stderr > 1e-9 else 0.0
        metrics[metric.key] = {
            "recent": round(recent_mean, 1),
            "baseline": round(base_mean, 1),
            "delta": round(recent_mean - base_mean, 1),
            "z": round(z, 2),
            "status": classify(z, recent_mean, base_mean, metric.floor, params.threshold),
            "run": _run_length([metric_value(g, metric.key) for g in games[-params.history_games :]], base_mean),
        }

    min_recent = statistics.fmean(g.minutes for g in recent)
    min_base = statistics.fmean(g.minutes for g in baseline)
    role_change = abs(min_recent - min_base) >= max(4.0, 0.2 * min_base)
    recent_dates = {g.date for g in recent}

    return {
        "key": latest.player_key,
        "name": latest.player_name,
        "team": latest.team,
        "last_game": latest.date,
        "games_recent": n_recent,
        "games_baseline": n_base,
        "min_recent": round(min_recent, 1),
        "min_baseline": round(min_base, 1),
        "role_change": role_change,
        "metrics": metrics,
        "history": [
            {
                "date": g.date,
                "opp": g.opp,
                "home": g.home,
                "min": round(g.minutes),
                "recent": g.date in recent_dates,
                **{m.key: round(metric_value(g, m.key), 1) for m in METRICS},
            }
            for g in games[-params.history_games :]
        ],
    }
