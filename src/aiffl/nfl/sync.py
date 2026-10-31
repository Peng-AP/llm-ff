"""Pull NFL data into SQLite.

Usage:
    python -m aiffl.nfl.sync all                # players + schedule + all stats/projections + summary
    python -m aiffl.nfl.sync players
    python -m aiffl.nfl.sync schedule           # all 18 weeks: kickoffs, game state, scores
    python -m aiffl.nfl.sync stats 9 10         # stats + projections for specific weeks
    python -m aiffl.nfl.sync summary            # rebuild per-player season / rest-of-season numbers
"""

import json
import sys
from datetime import datetime, timezone

from aiffl import db
from aiffl.nfl import sources
from aiffl.players import LAST_REGULAR_WEEK, build_summary

REGULAR_SEASON_WEEKS = range(1, 19)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(conn, kind: str, detail: str) -> None:
    conn.execute("INSERT INTO sync_runs (kind, ran_at, detail) VALUES (?, ?, ?)", (kind, now_utc(), detail))
    print(f"[{kind}] {detail}")


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_players(conn) -> None:
    rows = sources.players()
    ts = now_utc()
    conn.executemany(
        """INSERT INTO nfl_players VALUES
               (:player_id, :full_name, :position, :team, :status, :injury_status, :injury_body_part,
                :injury_notes, :practice_participation, :depth_chart_position, :depth_chart_order,
                :age, :years_exp, :news_updated, :updated_at)
           ON CONFLICT (player_id) DO UPDATE SET
               full_name=excluded.full_name, position=excluded.position, team=excluded.team,
               status=excluded.status, injury_status=excluded.injury_status,
               injury_body_part=excluded.injury_body_part, injury_notes=excluded.injury_notes,
               practice_participation=excluded.practice_participation,
               depth_chart_position=excluded.depth_chart_position,
               depth_chart_order=excluded.depth_chart_order, age=excluded.age,
               years_exp=excluded.years_exp, news_updated=excluded.news_updated,
               updated_at=excluded.updated_at""",
        [{
            "player_id": p["player_id"],
            "full_name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
            "position": p["position"],
            "team": p.get("team"),
            "status": p.get("status"),
            "injury_status": p.get("injury_status"),
            "injury_body_part": p.get("injury_body_part"),
            "injury_notes": p.get("injury_notes"),
            "practice_participation": p.get("practice_participation"),
            "depth_chart_position": p.get("depth_chart_position"),
            "depth_chart_order": p.get("depth_chart_order"),
            "age": p.get("age"),
            "years_exp": p.get("years_exp"),
            "news_updated": _ms_to_iso(p.get("news_updated")),
            "updated_at": ts,
        } for p in rows],
    )
    _log(conn, "players", f"{len(rows)} fantasy-position players")


def sync_schedule(conn, season: int) -> None:
    ts = now_utc()
    total = 0
    for week in REGULAR_SEASON_WEEKS:
        games = sources.week_games(season, week)
        conn.executemany(
            """INSERT INTO nfl_games VALUES
                   (:game_id, :season, :week, :kickoff_utc, :home, :away, :state,
                    :home_score, :away_score, :updated_at)
               ON CONFLICT (game_id) DO UPDATE SET
                   kickoff_utc=excluded.kickoff_utc, state=excluded.state,
                   home_score=excluded.home_score, away_score=excluded.away_score,
                   updated_at=excluded.updated_at""",
            [{**g, "updated_at": ts} for g in games],
        )
        total += len(games)
    _log(conn, "schedule", f"{total} games, season {season}")


def sync_week_stats(conn, season: int, week: int) -> None:
    known = {r[0] for r in conn.execute("SELECT player_id FROM nfl_players")}
    ts = now_utc()
    for table, fetch in (("nfl_stats", sources.weekly_stats), ("nfl_projections", sources.weekly_projections)):
        lines = {pid: s for pid, s in fetch(season, week).items() if pid in known and s}
        conn.execute(f"DELETE FROM {table} WHERE season = ? AND week = ?", (season, week))
        conn.executemany(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)",
            [(season, week, pid, json.dumps(s), ts) for pid, s in lines.items()],
        )
        _log(conn, table.removeprefix("nfl_"), f"week {week}: {len(lines)} players")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        sys.exit(1)
    state = sources.nfl_state()
    season, current_week = int(state["season"]), int(state["week"])
    cmd, args = argv[0], argv[1:]

    with db.connect() as conn:
        if cmd in ("players", "all"):
            sync_players(conn)
        if cmd in ("schedule", "all"):
            sync_schedule(conn, season)
        if cmd == "stats":
            weeks = [int(a) for a in args] or [current_week]
        elif cmd == "all":
            # Stats for every week played so far; projections for the rest of the fantasy season.
            weeks = list(range(1, LAST_REGULAR_WEEK + 1))
        else:
            weeks = []
        for week in weeks:
            sync_week_stats(conn, season, week)
        if cmd in ("all", "stats", "summary"):
            _log(conn, "summary", f"{build_summary(conn, season, now_utc())} players")
    print(f"done. season {season}, current week {current_week}")


if __name__ == "__main__":
    main(sys.argv[1:])
