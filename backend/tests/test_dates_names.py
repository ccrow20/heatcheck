from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from heatcheck import dates
from heatcheck.names import normalize_team, player_key


@pytest.mark.parametrize("year", [2025, 2026, 2027, 2028])
def test_rule_based_eastern_offset_matches_zoneinfo(year):
    eastern = ZoneInfo("America/New_York")
    moment = datetime(year, 1, 1, tzinfo=UTC)
    while moment.year == year:
        expected = moment.astimezone(eastern).utcoffset()
        assert dates.eastern_offset_rule_based(moment) == expected, moment
        moment += timedelta(hours=1)


def test_eastern_date_handles_late_games():
    # 10:30 PM ET tip-off on Nov 5 is 03:30 UTC on Nov 6.
    assert dates.eastern_date(datetime(2026, 11, 6, 3, 30, tzinfo=UTC)) == date(2026, 11, 5)
    # 7:00 PM ET on Nov 4 is midnight UTC on Nov 5.
    assert dates.eastern_date(datetime(2026, 11, 5, 0, 0, tzinfo=UTC)) == date(2026, 11, 4)


def test_to_eastern_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        dates.to_eastern(datetime(2026, 11, 5, 12, 0))


@pytest.mark.parametrize(
    ("day", "season"),
    [
        (date(2026, 10, 21), "2026-27"),
        (date(2026, 11, 5), "2026-27"),
        (date(2027, 6, 15), "2026-27"),
        (date(2026, 8, 1), "2026-27"),
        (date(2026, 7, 31), "2025-26"),
        (date(1999, 12, 1), "1999-00"),
    ],
)
def test_season_for(day, season):
    assert dates.season_for(day) == season


def test_previous_season():
    assert dates.previous_season("2026-27") == "2025-26"
    assert dates.previous_season("2000-01") == "1999-00"


def test_parse_iso_datetime_formats():
    assert dates.parse_iso_datetime("2025-10-07T23:00:00.000Z") == datetime(2025, 10, 7, 23, tzinfo=UTC)
    assert dates.parse_iso_datetime("2026-01-21T00:00Z") == datetime(2026, 1, 21, tzinfo=UTC)
    assert dates.parse_iso_datetime("2026-01-21T00:30:00").tzinfo == UTC


def test_nightly_target_dates_uses_eastern_yesterday():
    # 05:30 AM ET on Nov 6 -> last night was Nov 5
    now = datetime(2026, 11, 6, 10, 30, tzinfo=UTC)
    assert dates.nightly_target_dates(now, 1) == [date(2026, 11, 5)]
    assert dates.nightly_target_dates(now, 3) == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    # 10 PM ET on Nov 5 is already Nov 6 in UTC, but "yesterday" is still Nov 4
    assert dates.nightly_target_dates(datetime(2026, 11, 6, 3, 0, tzinfo=UTC), 1) == [date(2026, 11, 4)]
    with pytest.raises(ValueError):
        dates.nightly_target_dates(now, 0)


def test_date_range():
    assert dates.date_range(date(2026, 11, 1), date(2026, 11, 3)) == [date(2026, 11, d) for d in (1, 2, 3)]
    with pytest.raises(ValueError):
        dates.date_range(date(2026, 11, 3), date(2026, 11, 1))


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Nikola Jokić", "nikola-jokic"),
        ("Luka Dončić", "luka-doncic"),
        ("Jaren Jackson Jr.", "jaren-jackson"),
        ("Gary Payton II", "gary-payton"),
        ("De'Aaron Fox", "deaaron-fox"),
        ("De’Aaron Fox", "deaaron-fox"),
        ("P.J. Washington", "pj-washington"),
        ("Karl-Anthony Towns", "karl-anthony-towns"),
        ("Alperen Şengün", "alperen-sengun"),
        ("  Marcus   Hale ", "marcus-hale"),
        ("Nicolas Claxton", "nic-claxton"),  # alias
        ("J.R. Smith", "jr-smith"),  # a two-token name keeps its "suffix"
    ],
)
def test_player_key(name, key):
    assert player_key(name) == key


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NY", "NYK"),
        ("GS", "GSW"),
        ("SA", "SAS"),
        ("NO", "NOP"),
        ("UTAH", "UTA"),
        ("WSH", "WAS"),
        ("bos", "BOS"),
        ("PHO", "PHX"),
        ("LEB", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_team(code, expected):
    assert normalize_team(code) == expected
