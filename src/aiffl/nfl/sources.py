"""Fetchers for the public Sleeper and ESPN endpoints. No auth needed.

Sleeper asks that /players/nfl (about 14 MB) be fetched at most once a day.
The stats, projections, and ESPN scoreboard endpoints are undocumented but widely used;
if one changes shape, the sync fails loudly rather than writing bad data.
"""

import httpx

SLEEPER = "https://api.sleeper.app/v1"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# ESPN abbreviation -> Sleeper abbreviation, where they differ.
ESPN_TO_SLEEPER_TEAM = {"WSH": "WAS"}

_client = httpx.Client(timeout=60, headers={"User-Agent": "llm-ff (fantasy league sync)"})


def _get(url: str, **params) -> object:
    r = _client.get(url, params=params or None)
    r.raise_for_status()
    return r.json()


def nfl_state() -> dict:
    """Current season and week, e.g. {'season': '2026', 'week': 8, 'season_type': 'regular', ...}."""
    return _get(f"{SLEEPER}/state/nfl")


def players() -> list[dict]:
    """Fantasy-relevant players only (QB/RB/WR/TE/K/DEF)."""
    data = _get(f"{SLEEPER}/players/nfl")
    return [p for p in data.values() if p.get("position") in FANTASY_POSITIONS]


def weekly_stats(season: int, week: int) -> dict[str, dict]:
    """player_id -> raw stat line for one regular-season week."""
    return _get(f"{SLEEPER}/stats/nfl/regular/{season}/{week}")


def weekly_projections(season: int, week: int) -> dict[str, dict]:
    """player_id -> projected stat line for one regular-season week."""
    return _get(f"{SLEEPER}/projections/nfl/regular/{season}/{week}")


def week_games(season: int, week: int) -> list[dict]:
    """Games for one regular-season week with kickoff times, state, and scores."""
    data = _get(ESPN_SCOREBOARD, seasontype=2, week=week, dates=season)
    games = []
    for event in data["events"]:
        comp = event["competitions"][0]
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        team = lambda side: ESPN_TO_SLEEPER_TEAM.get(sides[side]["team"]["abbreviation"],
                                                     sides[side]["team"]["abbreviation"])
        score = lambda side: int(sides[side]["score"]) if sides[side].get("score") not in (None, "") else None
        games.append({
            "game_id": event["id"],
            "season": season,
            "week": week,
            "kickoff_utc": _normalize_utc(event["date"]),
            "home": team("home"),
            "away": team("away"),
            "state": event["status"]["type"]["state"],  # pre / in / post
            "home_score": score("home"),
            "away_score": score("away"),
        })
    return games


def _normalize_utc(espn_date: str) -> str:
    # ESPN gives "2026-11-06T01:15Z"; store with seconds so string comparison is safe.
    if len(espn_date) == len("2026-11-06T01:15Z"):
        return espn_date[:-1] + ":00Z"
    return espn_date
