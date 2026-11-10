from datetime import date

import pytest

from heatcheck.analytics import (
    COLD,
    HOT,
    METRICS,
    NEUTRAL,
    StreakParams,
    _run_length,
    classify,
    compute_streaks,
    game_score,
)
from heatcheck.models import PlayerLine
from tests.conftest import make_line, series

AS_OF = date(2026, 11, 5)

# A steady 20-point scorer with realistic night-to-night noise (sd ~ 4.5).
STEADY = [18, 24, 15, 22, 27, 19, 14, 23, 21, 17, 25, 16, 20, 22, 18, 24, 13, 21, 19, 23]


def league(*players: list[PlayerLine]) -> list[PlayerLine]:
    """Background players so the league-typical spread is realistic."""
    background = []
    for i in range(12):
        rotated = STEADY[i:] + STEADY[:i]
        background += series(f"Bench Guy{i}", [v + 5 for v in rotated[:15]] + rotated[15:20])
    return background + [line for p in players for line in p]


def player(result, name):
    return next(p for p in result["players"] if p["name"] == name)


def test_game_score_matches_hollinger_formula():
    line = make_line(pts=30, reb=10, ast=8, fg3m=3)
    expected = (
        line.pts
        + 0.4 * line.fgm
        - 0.7 * line.fga
        - 0.4 * (line.fta - line.ftm)
        + 0.7 * line.oreb
        + 0.3 * line.dreb
        + line.stl
        + 0.7 * line.ast
        + 0.7 * line.blk
        - 0.4 * line.pf
        - line.tov
    )
    assert game_score(line) == pytest.approx(expected)


def test_game_score_without_rebound_split_uses_blended_weight():
    line = PlayerLine(**{**make_line(reb=10).to_dict(), "oreb": None, "dreb": None})
    with_split = make_line(reb=10)
    assert game_score(line) - game_score(with_split) == pytest.approx(0.4 * 10 - (0.7 * 2 + 0.3 * 8))


def test_hot_player_is_flagged():
    hot = series("Heater Hot", STEADY[:15] + [34, 31, 36, 33, 38])
    result = compute_streaks(league(hot), as_of=AS_OF)
    pts = player(result, "Heater Hot")["metrics"]["pts"]
    assert pts["status"] == HOT
    assert pts["recent"] == pytest.approx(34.4)
    assert pts["z"] > 3
    assert pts["run"] == 5


def test_cold_player_is_flagged():
    cold = series("Icy Cold", STEADY[:15] + [8, 11, 6, 9, 10])
    result = compute_streaks(league(cold), as_of=AS_OF)
    pts = player(result, "Icy Cold")["metrics"]["pts"]
    assert pts["status"] == COLD
    assert pts["z"] < -3
    # The last baseline game (18) was also under his ~20 PPG norm, so the run is 6.
    assert pts["run"] == -6


def test_steady_player_is_neutral():
    result = compute_streaks(league(series("Mister Steady", STEADY)), as_of=AS_OF)
    pts = player(result, "Mister Steady")["metrics"]["pts"]
    assert pts["status"] == NEUTRAL
    assert abs(pts["z"]) < 1.5


def test_shrinkage_tames_zero_variance_baselines():
    """A bench player who always scored exactly 4 has sd=0; one good stretch must not explode."""
    flat = series("Flat Line", [4] * 15 + [6, 5, 7, 6, 6], minutes=14.0)
    result = compute_streaks(league(flat), as_of=AS_OF)
    pts = player(result, "Flat Line")["metrics"]["pts"]
    assert pts["z"] < 10  # without shrinkage this would be infinite
    assert pts["status"] == NEUTRAL  # recent 6.0 PPG is below the 10-point relevance floor


def test_relevance_floor_for_cold_uses_baseline():
    # 6 -> 1 PPG is a big drop, but a 6-point scorer is not "cold" in any meaningful sense.
    low = series("Low Usage", [6, 7, 5, 6, 8, 5, 6, 7, 6, 5, 6, 7, 5, 6, 6] + [1, 0, 2, 1, 1])
    result = compute_streaks(league(low), as_of=AS_OF)
    pts = player(result, "Low Usage")["metrics"]["pts"]
    assert pts["z"] < -1.5
    assert pts["status"] == NEUTRAL


def test_short_minutes_games_are_ignored():
    lines = series("Garbage Time", STEADY[:15] + [30, 30, 30, 30, 30])
    cameos = [make_line("Garbage Time", day, pts=0, minutes=4.0) for day in ("2026-11-06",)]
    result = compute_streaks(league(lines + cameos), as_of=date(2026, 11, 6))
    assert player(result, "Garbage Time")["history"][-1]["pts"] == 30


def test_players_need_enough_games_and_recent_activity():
    params = StreakParams()
    too_few = series("Rookie New", [20] * (params.window + params.baseline_min - 1))
    inactive = series("Long Injury", STEADY, end=date(2026, 10, 20))
    result = compute_streaks(league(too_few, inactive), as_of=AS_OF)
    names = {p["name"] for p in result["players"]}
    assert "Rookie New" not in names
    assert "Long Injury" not in names


def test_future_lines_are_ignored():
    lines = series("Time Traveler", STEADY) + [make_line("Time Traveler", "2026-11-09", pts=60)]
    result = compute_streaks(league(lines), as_of=AS_OF)
    assert player(result, "Time Traveler")["last_game"] == "2026-11-05"


def test_role_change_flag():
    lines = series("Big Minutes", STEADY[:15], minutes=18.0, end=date(2026, 10, 31)) + series(
        "Big Minutes", STEADY[15:], minutes=34.0
    )
    result = compute_streaks(league(lines), as_of=AS_OF)
    p = player(result, "Big Minutes")
    assert p["role_change"] is True
    assert (p["min_baseline"], p["min_recent"]) == (18.0, 34.0)


def test_duplicate_dates_keep_the_bigger_role():
    lines = series("Name Twin", STEADY)
    lines.append(
        make_line("Name Twin", "2026-11-05", pts=2, minutes=11.0, source_player_id="999", game_id="espn:other")
    )
    result = compute_streaks(league(lines), as_of=AS_OF)
    assert player(result, "Name Twin")["history"][-1]["pts"] == STEADY[-1]


def test_output_shape():
    result = compute_streaks(league(series("Shape Check", STEADY)), as_of=AS_OF, params=StreakParams(history_games=10))
    assert result["as_of"] == "2026-11-05"
    assert [m["key"] for m in result["metrics"]] == [m.key for m in METRICS]
    assert set(result["summary"]["pts"]) == {HOT, COLD}
    p = player(result, "Shape Check")
    assert set(p["metrics"]) == {m.key for m in METRICS}
    assert len(p["history"]) == 10
    assert sum(1 for g in p["history"] if g["recent"]) == 5
    assert {"date", "opp", "home", "min", "recent", "gmsc", "pts"} <= set(p["history"][0])
    assert p["games_recent"] == 5 and p["games_baseline"] == 15


def test_empty_input():
    result = compute_streaks([], as_of=AS_OF)
    assert result["players"] == []
    assert result["summary"]["pts"] == {HOT: 0, COLD: 0}


def test_run_length():
    assert _run_length([1, 5, 6, 7], 4) == 3
    assert _run_length([9, 3, 2], 4) == -2
    assert _run_length([5, 4], 4) == 0
    assert _run_length([], 4) == 0


def test_classify():
    assert classify(2.0, 25, 18, floor=10, threshold=1.5) == HOT
    assert classify(2.0, 8, 4, floor=10, threshold=1.5) == NEUTRAL
    assert classify(-2.0, 10, 18, floor=10, threshold=1.5) == COLD
    assert classify(-2.0, 1, 6, floor=10, threshold=1.5) == NEUTRAL
    assert classify(1.0, 30, 10, floor=10, threshold=1.5) == NEUTRAL


def test_params_validation():
    with pytest.raises(ValueError):
        StreakParams(window=1)
    with pytest.raises(ValueError):
        StreakParams(baseline_min=10, baseline_max=5)
