"""Player summaries and lookups built from the nfl_* tables, scored with league rules."""

import json
import sqlite3
from collections import defaultdict

from aiffl.config import load_league
from aiffl.scoring import fantasy_points

LAST_REGULAR_WEEK = 17  # fantasy season ends at week 17; week 18 is not used


def played_weeks(conn: sqlite3.Connection, season: int) -> set[int]:
    """Weeks whose games have all finished."""
    rows = conn.execute(
        "SELECT week, SUM(state != 'post') AS unfinished FROM nfl_games WHERE season = ? GROUP BY week",
        (season,)).fetchall()
    return {r["week"] for r in rows if r["unfinished"] == 0}


def bye_weeks(conn: sqlite3.Connection, season: int) -> dict[str, int]:
    playing = defaultdict(set)
    for r in conn.execute("SELECT week, home, away FROM nfl_games WHERE season = ?", (season,)):
        playing[r["week"]].update((r["home"], r["away"]))
    teams = set().union(*playing.values()) if playing else set()
    byes = {}
    for week in sorted(playing):
        for team in teams - playing[week]:
            byes.setdefault(team, week)
    return byes


def build_summary(conn: sqlite3.Connection, season: int, now: str) -> int:
    league = load_league()
    weights = league["scoring"]
    start_week = league["schedule"]["start_week"]
    done = played_weeks(conn, season)
    next_week = min((w for w in range(start_week, LAST_REGULAR_WEEK + 1) if w not in done), default=None)
    byes = bye_weeks(conn, season)
    players = {r["player_id"]: r for r in conn.execute("SELECT player_id, position, team FROM nfl_players")}

    season_pts, games = defaultdict(float), defaultdict(int)
    for r in conn.execute("SELECT week, player_id, stats FROM nfl_stats WHERE season = ?", (season,)):
        p = players.get(r["player_id"])
        if not p:
            continue
        stats = json.loads(r["stats"])
        if stats.get("gp"):
            games[r["player_id"]] += 1
            season_pts[r["player_id"]] += fantasy_points(p["position"], stats, weights)

    ros, nxt = defaultdict(float), {}
    for r in conn.execute("SELECT week, player_id, stats FROM nfl_projections WHERE season = ? AND week >= ?",
                          (season, start_week)):
        p = players.get(r["player_id"])
        if not p or r["week"] in done or r["week"] > LAST_REGULAR_WEEK:
            continue
        pts = fantasy_points(p["position"], json.loads(r["stats"]), weights, projected=True)
        ros[r["player_id"]] += pts
        if r["week"] == next_week:
            nxt[r["player_id"]] = pts

    rows = []
    for pid, p in players.items():
        g = games.get(pid, 0)
        bye = byes.get(p["team"]) if p["team"] else None
        remaining_bye = bye if bye and bye >= start_week else None  # past byes don't matter to managers
        rows.append((pid, g, round(season_pts.get(pid, 0.0), 2), round(season_pts[pid] / g, 2) if g else None,
                     round(ros.get(pid, 0.0), 2), nxt.get(pid), remaining_bye, now))
    conn.execute("DELETE FROM nfl_player_summary")
    conn.executemany("INSERT INTO nfl_player_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


PLAYER_COLUMNS = """
    p.player_id, p.full_name, p.position, p.team, p.status, p.injury_status, p.injury_body_part,
    p.practice_participation, p.depth_chart_position, p.depth_chart_order, p.age, p.years_exp,
    p.news_updated, s.games, s.season_points, s.points_per_game, s.ros_projection, s.next_week_proj,
    s.bye_week, r.team_id AS fantasy_team
"""
PLAYER_FROM = """
    FROM nfl_players p
    LEFT JOIN nfl_player_summary s USING (player_id)
    LEFT JOIN rosters r USING (player_id)
"""


def player_row(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in row.keys() if row[k] is not None}
    d["data_note"] = "Status fields come from Sleeper and can lag the news. Verify before acting."
    return d


def get_player(conn: sqlite3.Connection, player_id: str) -> dict | None:
    row = conn.execute(f"SELECT {PLAYER_COLUMNS} {PLAYER_FROM} WHERE p.player_id = ?", (player_id,)).fetchone()
    return player_row(row) if row else None


def search(conn: sqlite3.Connection, *, position: str | None = None, name: str | None = None,
           nfl_team: str | None = None, available_only: bool = True, limit: int = 40) -> list[dict]:
    where, params = ["(p.team IS NOT NULL OR s.season_points > 0)"], []
    if position:
        where.append("p.position = ?")
        params.append(position.upper())
    if name:
        where.append("p.full_name LIKE ?")
        params.append(f"%{name}%")
    if nfl_team:
        where.append("p.team = ?")
        params.append(nfl_team.upper())
    if available_only:
        where.append("r.team_id IS NULL")
    params.append(max(1, min(limit, 100)))
    rows = conn.execute(
        f"SELECT {PLAYER_COLUMNS} {PLAYER_FROM} WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(s.ros_projection, 0) DESC, COALESCE(s.season_points, 0) DESC LIMIT ?",
        params).fetchall()
    keep = ("player_id", "full_name", "position", "team", "injury_status", "bye_week", "games",
            "season_points", "points_per_game", "ros_projection", "next_week_proj", "fantasy_team")
    return [{k: r[k] for k in keep if r[k] is not None} for r in rows]


def weekly_log(conn: sqlite3.Connection, player_id: str, season: int) -> list[dict]:
    p = conn.execute("SELECT position FROM nfl_players WHERE player_id = ?", (player_id,)).fetchone()
    if not p:
        return []
    weights = load_league()["scoring"]
    interesting = ("off_snp", "tm_off_snp", "pass_att", "pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd",
                   "rush_td", "rec_tgt", "rec", "rec_yd", "rec_td", "fum_lost", "fga", "fgm", "xpm", "sack",
                   "int", "pts_allow")
    log = []
    for r in conn.execute("SELECT week, stats FROM nfl_stats WHERE season = ? AND player_id = ? ORDER BY week",
                          (season, player_id)):
        stats = json.loads(r["stats"])
        if not stats.get("gp"):
            continue
        log.append({"week": r["week"], "points": fantasy_points(p["position"], stats, weights),
                    **{k: stats[k] for k in interesting if stats.get(k)}})
    return log


def weekly_projections(conn: sqlite3.Connection, player_id: str, season: int, from_week: int) -> dict[int, float]:
    p = conn.execute("SELECT position FROM nfl_players WHERE player_id = ?", (player_id,)).fetchone()
    if not p:
        return {}
    weights = load_league()["scoring"]
    return {r["week"]: fantasy_points(p["position"], json.loads(r["stats"]), weights, projected=True)
            for r in conn.execute("SELECT week, stats FROM nfl_projections WHERE season = ? AND player_id = ? "
                                  "AND week >= ? AND week <= ? ORDER BY week",
                                  (season, player_id, from_week, LAST_REGULAR_WEEK))}
