import copy
from datetime import date

import pytest

from heatcheck.sources import (
    AllSourcesFailed,
    ApiNbaSource,
    EspnSource,
    SourceError,
    apinba,
    build_sources,
    espn,
    fetch_with_fallback,
    validate_day,
)
from heatcheck.sources.base import parse_minutes, split_made_attempted, to_int, to_optional_int
from tests.conftest import FakeHttp, day_result, full_game, load_fixture

NOV5 = date(2026, 11, 5)


# parsing helpers
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (36, 36.0),
        ("36", 36.0),
        ("36:30", 36.5),
        ("PT35M12.00S", 35.2),
        ("PT0M45S", 0.75),
        ("0", 0.0),
        ("--", None),
        ("", None),
        (None, None),
        ("DNP", None),
        (True, None),
        ("12:xx", None),
    ],
)
def test_parse_minutes(value, expected):
    assert parse_minutes(value) == expected


def test_int_helpers():
    assert to_int("+9") == 9
    assert to_int("-3") == -3
    assert to_int("7") == 7
    assert to_int(4.6) == 5
    assert to_int("--") == 0
    assert to_int(None, default=-1) == -1
    assert to_optional_int("--") is None
    assert to_optional_int("0") == 0
    assert split_made_attempted("10-21") == (10, 21)
    assert split_made_attempted("0-0") == (0, 0)
    assert split_made_attempted("--") == (0, 0)
    assert split_made_attempted(None) == (0, 0)


# ESPN
def espn_http(summary_overrides=None):
    http = FakeHttp()
    http.add(espn.SCOREBOARD_URL, {"dates": "20261105", "limit": "100"}, load_fixture("espn_scoreboard_20261105.json"))
    for event_id in ("401810001", "401810002"):
        payload = (summary_overrides or {}).get(event_id) or load_fixture(f"espn_summary_{event_id}.json")
        http.add(espn.SUMMARY_URL, {"event": event_id}, payload)
    return http


def test_espn_fetch_day_parses_games_and_lines():
    http = espn_http()
    result = EspnSource(http).fetch_day(NOV5)

    assert result.source == "espn"
    assert [(g.game_id, g.away, g.home, g.completed) for g in result.games] == [
        ("espn:401810001", "BOS", "NYK", True),
        ("espn:401810002", "GSW", "LAL", True),
        ("espn:401810003", "MIA", "ORL", False),
    ]
    # The postponed game's summary is never requested.
    assert [c[1] for c in http.calls if c[0] == espn.SUMMARY_URL] == [{"event": "401810001"}, {"event": "401810002"}]

    # 8 + 7 players logged minutes in game 1, 6 + 6 in game 2; DNPs are skipped.
    assert len(result.lines) == 27
    assert not any(ln.player_name in {"Rico Alvarez", "Victor Lindqvist"} for ln in result.lines)
    validate_day(result)

    hale = next(ln for ln in result.lines if ln.player_name == "Marcus Hale")
    assert (hale.team, hale.opp, hale.home) == ("BOS", "NYK", False)
    assert (hale.minutes, hale.pts, hale.fgm, hale.fga, hale.fg3m, hale.fg3a, hale.ftm, hale.fta) == (
        36.0,
        28,
        10,
        21,
        3,
        8,
        5,
        6,
    )
    assert (hale.reb, hale.oreb, hale.dreb, hale.ast, hale.plus_minus, hale.starter) == (8, 1, 7, 5, 9, True)
    assert hale.game_id == "espn:401810001"
    assert hale.source_player_id == "9000001"

    knick = next(ln for ln in result.lines if ln.player_name == "Jalen Rivers")
    assert (knick.team, knick.home, knick.plus_minus) == ("NYK", True, -6)

    accented = next(ln for ln in result.lines if ln.player_name == "Luka Dragović")
    assert accented.player_key == "luka-dragovic"


def test_espn_reads_stats_by_key_not_position():
    """ESPN has reordered its box score columns before; the parser must not care."""
    summary = load_fixture("espn_summary_401810001.json")
    for block in summary["boxscore"]["players"]:
        group = block["statistics"][0]
        order = list(reversed(range(len(group["keys"]))))
        group["keys"] = [group["keys"][i] for i in order]
        group["names"] = group["labels"] = [group["labels"][i] for i in order]
        for athlete in group["athletes"]:
            if athlete["stats"]:
                athlete["stats"] = [athlete["stats"][i] for i in order]
    result = EspnSource(espn_http({"401810001": summary})).fetch_day(NOV5)
    hale = next(ln for ln in result.lines if ln.player_name == "Marcus Hale")
    assert (hale.pts, hale.fgm, hale.fga, hale.reb, hale.ast) == (28, 10, 21, 8, 5)


def test_espn_falls_back_to_labels_and_tolerates_missing_optional_stats():
    summary = load_fixture("espn_summary_401810001.json")
    for block in summary["boxscore"]["players"]:
        group = block["statistics"][0]
        drop = {group["keys"].index("offensiveRebounds"), group["keys"].index("defensiveRebounds")}
        del group["keys"]
        group["labels"] = [lab for i, lab in enumerate(group["labels"]) if i not in drop]
        for athlete in group["athletes"]:
            if athlete["stats"]:
                athlete["stats"] = [s for i, s in enumerate(athlete["stats"]) if i not in drop]
    result = EspnSource(espn_http({"401810001": summary})).fetch_day(NOV5)
    hale = next(ln for ln in result.lines if ln.player_name == "Marcus Hale")
    assert (hale.reb, hale.oreb, hale.dreb) == (8, None, None)


def test_espn_raises_when_required_stats_disappear():
    summary = load_fixture("espn_summary_401810001.json")
    for block in summary["boxscore"]["players"]:
        group = block["statistics"][0]
        idx = group["keys"].index("points")
        group["keys"][idx] = "pointsScored"
    with pytest.raises(SourceError, match="missing stat keys"):
        EspnSource(espn_http({"401810001": summary})).fetch_day(NOV5)


def test_espn_raises_without_boxscore():
    with pytest.raises(SourceError, match="boxscore"):
        EspnSource(espn_http({"401810001": {"header": {}}})).fetch_day(NOV5)


def test_espn_scoreboard_must_have_events():
    http = FakeHttp().add(espn.SCOREBOARD_URL, {"dates": "20261105", "limit": "100"}, {"leagues": []})
    with pytest.raises(SourceError, match="events"):
        EspnSource(http).fetch_day(NOV5)


def test_espn_skips_preseason_and_non_nba_events():
    board = load_fixture("espn_scoreboard_20261105.json")
    preseason = copy.deepcopy(board["events"][0])
    preseason["season"]["type"] = 1
    assert espn.parse_scoreboard_event(preseason) is None

    all_star = copy.deepcopy(board["events"][0])
    all_star["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "LEB"
    assert espn.parse_scoreboard_event(all_star) is None

    playoff = copy.deepcopy(board["events"][0])
    playoff["season"]["type"] = 3
    assert espn.parse_scoreboard_event(playoff) is not None


def test_espn_off_night_returns_no_games():
    http = FakeHttp().add(espn.SCOREBOARD_URL, {"dates": "20261105", "limit": "100"}, {"events": []})
    result = EspnSource(http).fetch_day(NOV5)
    assert result.games == [] and result.lines == []
    validate_day(result)


# API-NBA
def apinba_http(**overrides):
    http = FakeHttp(headers={"x-ratelimit-requests-remaining": "90"})
    base = apinba.BASE_URL
    http.add(
        f"{base}/games", {"date": "2026-11-05"}, overrides.get("d0") or load_fixture("apinba_games_2026-11-05.json")
    )
    http.add(
        f"{base}/games", {"date": "2026-11-06"}, overrides.get("d1") or load_fixture("apinba_games_2026-11-06.json")
    )
    for gid in ("16010", "16011"):
        http.add(f"{base}/players/statistics", {"game": gid}, load_fixture(f"apinba_stats_{gid}.json"))
    return http


def test_apinba_keeps_only_games_on_the_eastern_date():
    http = apinba_http()
    result = ApiNbaSource(http, "secret").fetch_day(NOV5)

    assert [(g.game_id, g.away, g.home, g.completed) for g in result.games] == [
        ("apinba:16010", "BOS", "NYK", True),  # 6:30 PM ET, queried under Nov 5 UTC
        ("apinba:16011", "GSW", "LAL", True),  # 10:30 PM ET, queried under Nov 6 UTC
        ("apinba:16012", "MIA", "ORL", False),  # not finished: no stats request
    ]
    stats_calls = [c[1]["game"] for c in http.calls if c[0].endswith("/players/statistics")]
    assert stats_calls == ["16010", "16011"]
    assert all(c[2] == {"x-apisports-key": "secret"} for c in http.calls)


def test_apinba_parses_player_rows():
    result = ApiNbaSource(apinba_http(), "secret").fetch_day(NOV5)
    validate_day(result)
    # Same players as the ESPN fixture: DNP rows (min null) and the duplicate row are dropped.
    assert len(result.lines) == 27
    hale = next(ln for ln in result.lines if ln.player_name == "Marcus Hale")
    assert hale.minutes == pytest.approx(36 + ((1000 * 7) % 60) / 60, abs=0.01)  # "36:40" clock format
    assert (hale.pts, hale.fgm, hale.fga, hale.fg3m, hale.reb, hale.oreb, hale.dreb, hale.plus_minus) == (
        28,
        10,
        21,
        3,
        8,
        1,
        7,
        9,
    )
    assert (hale.team, hale.opp, hale.home) == ("BOS", "NYK", False)
    assert hale.player_key == "marcus-hale"


def test_apinba_matches_espn_player_keys():
    """Both sources must agree on identity so histories stay joined after a fallback."""
    espn_keys = {ln.player_key for ln in EspnSource(espn_http()).fetch_day(NOV5).lines}
    apinba_keys = {ln.player_key for ln in ApiNbaSource(apinba_http(), "k").fetch_day(NOV5).lines}
    assert espn_keys == apinba_keys


def test_apinba_errors_field_raises():
    with pytest.raises(SourceError, match="plan"):
        ApiNbaSource(apinba_http(d0=load_fixture("apinba_error_plan.json")), "k").fetch_day(NOV5)


def test_apinba_requires_a_key():
    with pytest.raises(ValueError):
        ApiNbaSource(FakeHttp(), "")


# fallback chain
class StubSource:
    def __init__(self, name, result=None, error=None):
        self.name, self.result, self.error, self.calls = name, result, error, 0

    def fetch_day(self, day):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_fallback_uses_next_source_when_first_fails():
    good = day_result("2026-11-05", full_game("2026-11-05", "g1"), source="second")
    first = StubSource("first", error=KeyError("boxscore"))
    second = StubSource("second", result=good)
    result, failed = fetch_with_fallback(NOV5, [first, second])
    assert result is good
    assert failed == ["first"]


def test_fallback_rejects_incomplete_box_scores():
    partial = day_result("2026-11-05", full_game("2026-11-05", "g1")[:7], source="first")
    good = day_result("2026-11-05", full_game("2026-11-05", "g1"), source="second")
    result, failed = fetch_with_fallback(NOV5, [StubSource("first", result=partial), StubSource("second", result=good)])
    assert result.source == "second"
    assert failed == ["first"]


def test_fallback_stops_at_first_success():
    good = day_result("2026-11-05", full_game("2026-11-05", "g1"), source="first")
    second = StubSource("second", error=RuntimeError("should not be called"))
    _result, failed = fetch_with_fallback(NOV5, [StubSource("first", result=good), second])
    assert failed == [] and second.calls == 0


def test_all_sources_failing_reports_each_error():
    with pytest.raises(AllSourcesFailed) as excinfo:
        fetch_with_fallback(NOV5, [StubSource("a", error=SourceError("down")), StubSource("b", error=TimeoutError())])
    assert set(excinfo.value.errors) == {"a", "b"}
    assert "down" in excinfo.value.errors["a"]


def test_build_sources():
    http = FakeHttp()
    assert [s.name for s in build_sources(["espn", "apinba"], http, apinba_key="k")] == ["espn", "apinba"]
    assert [s.name for s in build_sources(["apinba", "espn"], http, apinba_key=None)] == ["espn"]
    with pytest.raises(ValueError, match="unknown"):
        build_sources(["espn", "nope"], http)
    with pytest.raises(ValueError, match="no usable"):
        build_sources(["apinba"], http, apinba_key=None)
