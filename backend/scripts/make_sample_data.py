"""Build a fictional league and run it through the pipeline so the site has data.

    cd backend && PYTHONPATH=src python scripts/make_sample_data.py

Output is flagged "sample": true.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from heatcheck.analytics import StreakParams
from heatcheck.models import DayResult, GameSummary, PlayerLine
from heatcheck.names import NBA_TEAMS, player_key
from heatcheck.pipeline import RunReport, build_payload, load_lines
from heatcheck.storage import STREAKS_KEY, LocalStorage, gamelog_key

REPO = Path(__file__).resolve().parents[2]

FIRST = [
    "Aaron",
    "Adrian",
    "Alec",
    "Andre",
    "Asher",
    "Bennett",
    "Blake",
    "Brandon",
    "Brooks",
    "Caleb",
    "Cameron",
    "Carter",
    "Chase",
    "Cole",
    "Colin",
    "Damian",
    "Dante",
    "Darius",
    "Davion",
    "Declan",
    "Desmond",
    "Dominic",
    "Elias",
    "Emmett",
    "Ezra",
    "Felix",
    "Gavin",
    "Grant",
    "Hayden",
    "Isaac",
    "Isaiah",
    "Jalen",
    "Jamal",
    "Jasper",
    "Javon",
    "Jonah",
    "Jordan",
    "Julian",
    "Kai",
    "Kendrick",
    "Keon",
    "Landon",
    "Leon",
    "Levi",
    "Malik",
    "Marcus",
    "Mason",
    "Mateo",
    "Miles",
    "Nico",
    "Noah",
    "Omar",
    "Oscar",
    "Quentin",
    "Reggie",
    "Rhys",
    "Roman",
    "Rowan",
    "Silas",
    "Sterling",
    "Terrence",
    "Theo",
    "Tobias",
    "Trey",
    "Tristan",
    "Tyrese",
    "Victor",
    "Wesley",
    "Xavier",
    "Zane",
]
LAST = [
    "Abara",
    "Ashford",
    "Bellamy",
    "Blackwood",
    "Calloway",
    "Castellano",
    "Danforth",
    "Delacroix",
    "Ellison",
    "Fairbanks",
    "Fontaine",
    "Gallagher",
    "Garrity",
    "Hargrove",
    "Holloway",
    "Iverson",
    "Jablonski",
    "Kessler",
    "Kowalski",
    "Langford",
    "Lindqvist",
    "Mbeki",
    "Montague",
    "Navarro",
    "Okafor",
    "Oyelaran",
    "Pemberton",
    "Quarles",
    "Radcliffe",
    "Ramsey",
    "Rourke",
    "Santangelo",
    "Sorensen",
    "Stroud",
    "Thibodeau",
    "Underwood",
    "Vance",
    "Valdivia",
    "Whitlock",
    "Winslow",
    "Yarbrough",
    "Zielinski",
    "Achterberg",
    "Brennan",
    "Cordero",
    "Dragovic",
    "Esposito",
    "Ferreira",
    "Grantham",
    "Haddad",
    "Ibarra",
    "Jovanovic",
    "Kaminski",
    "Lockhart",
    "Moreau",
    "Nakamura",
    "Ostrowski",
    "Petrakis",
    "Quintero",
    "Rosales",
    "Szabo",
    "Taveras",
    "Ueda",
    "Vukovic",
    "Weatherby",
]

ROLES = [  # (label, points mean range, minutes mean, rebounds, assists, threes share)
    ("star", (23, 31), 35, (5, 9), (4, 8), 0.35),
    ("starter", (13, 21), 31, (4, 8), (2, 5), 0.35),
    ("starter", (11, 17), 29, (5, 10), (1, 4), 0.25),
    ("rotation", (7, 13), 23, (3, 6), (1, 4), 0.35),
    ("bench", (4, 9), 16, (2, 5), (1, 3), 0.30),
]


def game_dates() -> list[date]:
    """25 nights from the end of last season plus this season's first 7."""
    last_season = [date(2026, 3, 1) + timedelta(days=int(i * 1.75)) for i in range(25)]
    this_season = [date(2026, 10, 22) + timedelta(days=int(i * 2.3)) for i in range(7)]
    return last_season + this_season


def make_players(rng: random.Random) -> list[dict]:
    names = set()
    players = []
    for team in sorted(NBA_TEAMS):
        for role, pts_range, minutes, reb_range, ast_range, three_share in ROLES:
            while True:
                name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                if player_key(name) not in names:
                    names.add(player_key(name))
                    break
            players.append(
                {
                    "name": name,
                    "id": str(100000 + len(players)),
                    "team": team,
                    "role": role,
                    "pts": rng.uniform(*pts_range),
                    "min": minutes + rng.uniform(-2, 2),
                    "reb": rng.uniform(*reb_range),
                    "ast": rng.uniform(*ast_range),
                    "three_share": three_share,
                    "streak": rng.choices(["hot", "cold", "role", None], weights=[12, 10, 4, 74])[0],
                    "streak_size": rng.uniform(0.35, 0.65),
                }
            )
    return players


def box_line(
    rng: random.Random, p: dict, day: date, game_id: str, opp: str, home: bool, boost: float, minutes_boost: float
):
    minutes = max(8.0, rng.gauss(p["min"] * minutes_boost, 3.5))
    scale = minutes / p["min"]
    pts = max(0, round(rng.gauss(p["pts"] * scale * boost, 0.28 * p["pts"] + 2)))
    fg3m = min(pts // 3, max(0, round(rng.gauss(pts * p["three_share"] / 3, 0.8))))
    ftm = min(pts - 3 * fg3m, max(0, round(rng.gauss(pts * 0.17, 1.5))))
    if (pts - 3 * fg3m - ftm) % 2:
        ftm += 1 if ftm + 1 <= pts - 3 * fg3m else -1
    fg2m = (pts - 3 * fg3m - ftm) // 2
    fgm = fg2m + fg3m
    fg3a = fg3m + max(0, round(rng.gauss(fg3m * 1.7 + 1, 1)))
    fga = fgm + max(0, round(rng.gauss(fgm * 1.1, 2))) + (fg3a - fg3m)
    fta = ftm + max(0, round(rng.gauss(ftm * 0.25, 0.8)))
    reb = max(0, round(rng.gauss(p["reb"] * scale * (boost**0.5), 2)))
    oreb = min(reb, max(0, round(reb * rng.uniform(0.1, 0.35))))
    ast = max(0, round(rng.gauss(p["ast"] * scale * (boost**0.5), 1.6)))
    return PlayerLine(
        date=day.isoformat(),
        game_id=game_id,
        source="sample",
        player_key=player_key(p["name"]),
        player_name=p["name"],
        source_player_id=p["id"],
        team=p["team"],
        opp=opp,
        home=home,
        minutes=round(minutes, 1),
        pts=pts,
        reb=reb,
        ast=ast,
        stl=max(0, round(rng.gauss(1.0, 0.9))),
        blk=max(0, round(rng.gauss(0.6, 0.8))),
        tov=max(0, round(rng.gauss(1.6 * scale, 1.1))),
        pf=max(0, min(6, round(rng.gauss(2.2, 1.1)))),
        fgm=fgm,
        fga=max(fga, fgm),
        fg3m=fg3m,
        fg3a=fg3a,
        ftm=ftm,
        fta=fta,
        oreb=oreb,
        dreb=reb - oreb,
        plus_minus=round(rng.gauss(0, 8)),
        starter=p["role"] in {"star", "starter"},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=str(REPO / ".localdata" / "sample"), help="where to write game logs")
    parser.add_argument("--site", default=str(REPO / "frontend"), help="where to write data/streaks.json")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    players = make_players(rng)
    by_team: dict[str, list[dict]] = {}
    for p in players:
        by_team.setdefault(p["team"], []).append(p)

    data = LocalStorage(args.data)
    dates = game_dates()
    teams = sorted(NBA_TEAMS)
    for index, day in enumerate(dates):
        recent = index >= len(dates) - 5
        rng.shuffle(teams)
        games, lines = [], []
        for home, away in zip(teams[0::2], teams[1::2], strict=True):
            game_id = f"sample:{day.isoformat()}-{away}-{home}"
            games.append(GameSummary(game_id, home, away, rng.randint(98, 128), rng.randint(98, 128), True))
            for team, opp in ((home, away), (away, home)):
                for p in by_team[team]:
                    boost = 1.0
                    minutes_boost = 1.0
                    if recent and p["streak"] == "hot":
                        boost = 1 + p["streak_size"]
                    elif recent and p["streak"] == "cold":
                        boost = 1 - p["streak_size"]
                    elif recent and p["streak"] == "role":
                        minutes_boost = 1.45
                    if rng.random() < 0.06:  # occasional night off
                        continue
                    lines.append(box_line(rng, p, day, game_id, opp, team == home, boost, minutes_boost))
        result = DayResult(date=day.isoformat(), source="sample", fetched_at="", games=games, lines=lines)
        data.put_json(gamelog_key(day), result.to_dict())

    as_of = dates[-1]
    report = RunReport(
        ingested={as_of.isoformat(): {"source": "sample", "games": 15, "lines": len(lines), "failed_sources": []}}
    )
    lines_all = load_lines(data, ["2025-26", "2026-27"])
    payload = build_payload(lines_all, as_of=as_of, now=datetime.now(UTC), report=report, params=StreakParams())
    payload["sample"] = True
    LocalStorage(args.site).put_json(STREAKS_KEY, payload)
    hot = payload["summary"]["gmsc"]["hot"]
    cold = payload["summary"]["gmsc"]["cold"]
    out = Path(args.site) / STREAKS_KEY
    print(f"Wrote {out}: {len(payload['players'])} players, {hot} hot / {cold} cold by Game Score")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
