"""Team codes and player name keys.

Sources use different player IDs, so players are keyed by normalized name
("Nikola Jokić" -> nikola-jokic). Two players with the same name would merge;
the pipeline warns when it sees that and NAME_ALIASES can split them.
"""

from __future__ import annotations

import re
import unicodedata

# The 30 NBA teams, keyed by the league's standard three-letter codes.
NBA_TEAMS: dict[str, str] = {
    "ATL": "Atlanta Hawks",
    "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",
    "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",
    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",
    "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",
    "IND": "Indiana Pacers",
    "LAC": "LA Clippers",
    "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",
    "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",
    "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans",
    "NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder",
    "ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",
    "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers",
    "SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",
    "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",
    "WAS": "Washington Wizards",
}

# Codes other sources use for the same teams (ESPN uses NY, GS, SA, NO, UTAH, WSH).
TEAM_ALIASES: dict[str, str] = {
    "NY": "NYK",
    "GS": "GSW",
    "SA": "SAS",
    "NO": "NOP",
    "NOR": "NOP",
    "UTAH": "UTA",
    "WSH": "WAS",
    "PHO": "PHX",
    "BRK": "BKN",
    "BRO": "BKN",
    "CHO": "CHA",
    "GOS": "GSW",
    "SAN": "SAS",
    "NOH": "NOP",
}

# Players whose names are spelled differently across sources (variant -> keep).
NAME_ALIASES: dict[str, str] = {
    "nicolas-claxton": "nic-claxton",
    "cameron-thomas": "cam-thomas",
    "herbert-jones": "herb-jones",
    "moritz-wagner": "moe-wagner",
    "sviatoslav-mykhailiuk": "svi-mykhailiuk",
    "kenyon-martin": "kj-martin",
    "patrick-mills": "patty-mills",
}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_team(code: str | None) -> str | None:
    """Return the standard code for an NBA team, or None for anything else.

    Returning None for unknown teams is deliberate: it filters out All-Star
    games, exhibitions against international clubs and other non-NBA matchups.
    """
    if not code:
        return None
    upper = code.strip().upper()
    upper = TEAM_ALIASES.get(upper, upper)
    return upper if upper in NBA_TEAMS else None


def player_key(name: str) -> str:
    """Stable, source-independent key for a player name.

    >>> player_key("Nikola Jokić")
    'nikola-jokic'
    >>> player_key("Jaren Jackson Jr.")
    'jaren-jackson'
    >>> player_key("De'Aaron Fox")
    'deaaron-fox'
    """
    # NFKD splits accented letters ("ć" -> "c" + accent) and the ASCII step drops the
    # accents along with curly apostrophes; straight apostrophes and periods go next.
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.lower().replace("'", "").replace(".", "")
    tokens = [t for t in re.split(r"[^a-z0-9]+", ascii_name) if t]
    while len(tokens) > 2 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    key = "-".join(tokens)
    return NAME_ALIASES.get(key, key)
