"""League state: teams, the snake draft, rosters. All rules are enforced here, never in prompts."""

import random
import sqlite3
from collections import Counter

from aiffl import clock
from aiffl.config import Team, load_league


class LeagueError(Exception):
    """A rejected action. The message is shown to the agent, so make it actionable."""


def now_utc() -> str:
    return clock.now_iso()


# Roster requirements -----------------------------------------------------------------------

FLEX_POSITIONS = ("RB", "WR", "TE")


def starter_requirements() -> tuple[Counter, int]:
    """(required count per position, number of FLEX slots) from league.yaml."""
    req, flex = Counter(), 0
    for slot in load_league()["roster"]["starters"]:
        if slot == "FLEX":
            flex += 1
        else:
            req["DEF" if slot == "DST" else slot] += 1
    return req, flex


def missing_starters(counts: Counter) -> int:
    """How many more players this roster needs before it can field a full starting lineup."""
    req, flex = starter_requirements()
    missing = sum(max(0, n - counts[pos]) for pos, n in req.items())
    surplus = sum(max(0, counts[pos] - req[pos]) for pos in FLEX_POSITIONS)
    return missing + max(0, flex - surplus)


def roster_size() -> int:
    roster = load_league()["roster"]
    return len(roster["starters"]) + roster["bench"]


# Setup -------------------------------------------------------------------------------------

def init_league(conn: sqlite3.Connection, teams: list[Team], rounds: int | None = None,
                seed: int | None = None) -> list[str]:
    """Reset league state and create a randomized snake draft. Returns team IDs in draft order."""
    rounds = rounds or roster_size()
    order = [t.id for t in teams]
    random.Random(seed).shuffle(order)
    with conn:
        for table in ("jobs_done", "agent_runs", "messages", "trades", "transactions", "waiver_claims", "lineups",
                      "matchups", "tool_calls", "agent_sessions", "rosters", "draft_queue", "draft_picks",
                      "draft_state", "league_teams"):
            conn.execute(f"DELETE FROM {table}")
        by_id = {t.id: t for t in teams}
        conn.executemany("INSERT INTO league_teams (team_id, model, harness, draft_slot) VALUES (?, ?, ?, ?)",
                         [(tid, by_id[tid].model, by_id[tid].harness, i + 1) for i, tid in enumerate(order)])
        pick_no = 0
        for rnd in range(1, rounds + 1):
            for tid in (order if rnd % 2 else list(reversed(order))):
                pick_no += 1
                conn.execute("INSERT INTO draft_picks (pick_no, round, team_id) VALUES (?, ?, ?)",
                             (pick_no, rnd, tid))
        conn.execute("INSERT INTO draft_state (id, status, rounds) VALUES (1, 'pending', ?)", (rounds,))
    return order


def set_draft_status(conn: sqlite3.Connection, status: str) -> None:
    with conn:
        if status == "live":
            conn.execute("UPDATE draft_state SET status = ?, started_at = COALESCE(started_at, ?)", (status, now_utc()))
        elif status == "done":
            conn.execute("UPDATE draft_state SET status = ?, finished_at = ?", (status, now_utc()))
        else:
            conn.execute("UPDATE draft_state SET status = ?", (status,))


# Queries -----------------------------------------------------------------------------------

def draft_state(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM draft_state WHERE id = 1").fetchone()


def draft_order(conn: sqlite3.Connection) -> list[str]:
    return [r["team_id"] for r in conn.execute("SELECT team_id FROM league_teams ORDER BY draft_slot")]


def current_pick(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM draft_picks WHERE player_id IS NULL ORDER BY pick_no LIMIT 1").fetchone()


def team_pick_numbers(conn: sqlite3.Connection, team_id: str, remaining_only: bool = True) -> list[int]:
    sql = "SELECT pick_no FROM draft_picks WHERE team_id = ?" + (" AND player_id IS NULL" if remaining_only else "")
    return [r[0] for r in conn.execute(sql + " ORDER BY pick_no", (team_id,))]


def picks_made(conn: sqlite3.Connection, after_pick: int = 0) -> list[dict]:
    rows = conn.execute(
        """SELECT d.pick_no, d.round, d.team_id, d.player_id, d.method, p.full_name, p.position, p.team
           FROM draft_picks d JOIN nfl_players p USING (player_id)
           WHERE d.player_id IS NOT NULL AND d.pick_no > ? ORDER BY d.pick_no""", (after_pick,)).fetchall()
    return [dict(r) for r in rows]


def roster(conn: sqlite3.Connection, team_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT p.player_id, p.full_name, p.position, p.team, p.injury_status,
                  s.bye_week, s.ros_projection, s.points_per_game, r.how
           FROM rosters r JOIN nfl_players p USING (player_id) LEFT JOIN nfl_player_summary s USING (player_id)
           WHERE r.team_id = ? ORDER BY p.position, s.ros_projection DESC""", (team_id,)).fetchall()
    return [{k: r[k] for k in r.keys() if r[k] is not None} for r in rows]


def position_counts(conn: sqlite3.Connection, team_id: str) -> Counter:
    return Counter(r[0] for r in conn.execute(
        "SELECT p.position FROM rosters r JOIN nfl_players p USING (player_id) WHERE r.team_id = ?", (team_id,)))


def queue(conn: sqlite3.Connection, team_id: str, available_only: bool = True) -> list[dict]:
    rows = conn.execute(
        """SELECT q.rank, q.player_id, p.full_name, p.position, p.team, r.team_id AS fantasy_team
           FROM draft_queue q JOIN nfl_players p USING (player_id) LEFT JOIN rosters r USING (player_id)
           WHERE q.team_id = ? ORDER BY q.rank""", (team_id,)).fetchall()
    return [dict(r) for r in rows if not (available_only and r["fantasy_team"])]


# Actions -----------------------------------------------------------------------------------

def _check_draftable(conn: sqlite3.Connection, team_id: str, player_id: str) -> sqlite3.Row:
    player = conn.execute("SELECT * FROM nfl_players WHERE player_id = ?", (player_id,)).fetchone()
    if not player:
        raise LeagueError(f"No player with player_id {player_id!r}. Use search_players to find IDs.")
    owner = conn.execute("SELECT team_id FROM rosters WHERE player_id = ?", (player_id,)).fetchone()
    if owner:
        raise LeagueError(f"{player['full_name']} is already on {owner['team_id']}.")
    counts = position_counts(conn, team_id)
    counts[player["position"]] += 1
    picks_left_after = len(team_pick_numbers(conn, team_id)) - 1
    need = missing_starters(counts)
    full_draft = draft_state(conn)["rounds"] >= len(load_league()["roster"]["starters"])  # short test drafts skip this
    if full_draft and need > picks_left_after:
        raise LeagueError(
            f"Drafting {player['full_name']} ({player['position']}) would leave {need} required starting slots "
            f"unfilled with only {picks_left_after} picks left. Needs: {describe_needs(counts)}.")
    return player


def describe_needs(counts: Counter) -> str:
    req, flex = starter_requirements()
    parts = [f"{pos} x{n - counts[pos]}" for pos, n in req.items() if counts[pos] < n]
    surplus = sum(max(0, counts[pos] - req[pos]) for pos in FLEX_POSITIONS)
    if surplus < flex:
        parts.append(f"FLEX (RB/WR/TE) x{flex - surplus}")
    return ", ".join(parts) or "none: every starting slot can be filled"


def make_pick(conn: sqlite3.Connection, team_id: str, player_id: str, method: str = "agent") -> dict:
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")  # serialize against other agents' processes
    try:
        state = draft_state(conn)
        if not state or state["status"] != "live":
            raise LeagueError(f"The draft is not live (status: {state['status'] if state else 'not set up'}).")
        pick = current_pick(conn)
        if pick is None:
            raise LeagueError("The draft is complete.")
        if pick["team_id"] != team_id:
            raise LeagueError(f"You are not on the clock. Pick {pick['pick_no']} belongs to {pick['team_id']}.")
        player = _check_draftable(conn, team_id, player_id)
        ts = now_utc()
        conn.execute("UPDATE draft_picks SET player_id = ?, made_at = ?, method = ? WHERE pick_no = ?",
                     (player_id, ts, method, pick["pick_no"]))
        conn.execute("INSERT INTO rosters (player_id, team_id, acquired_at, how) VALUES (?, ?, ?, 'draft')",
                     (player_id, team_id, ts))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"pick_no": pick["pick_no"], "round": pick["round"], "player_id": player_id,
            "full_name": player["full_name"], "position": player["position"], "team": player["team"]}


def autopick(conn: sqlite3.Connection, team_id: str) -> dict:
    """Pick for a team that ran out of time: first valid player in its own queue, else the best
    rest-of-season projection that keeps its roster valid."""
    for q in queue(conn, team_id):
        try:
            return make_pick(conn, team_id, q["player_id"], method="autopick_queue")
        except LeagueError:
            continue
    rows = conn.execute(
        """SELECT p.player_id FROM nfl_players p JOIN nfl_player_summary s USING (player_id)
           LEFT JOIN rosters r USING (player_id)
           WHERE r.team_id IS NULL AND p.team IS NOT NULL ORDER BY s.ros_projection DESC LIMIT 300""").fetchall()
    for r in rows:
        try:
            return make_pick(conn, team_id, r["player_id"], method="autopick_projection")
        except LeagueError:
            continue
    raise LeagueError(f"No valid player found to autopick for {team_id}.")


def set_queue(conn: sqlite3.Connection, team_id: str, player_ids: list[str]) -> dict:
    seen, valid, problems = set(), [], []
    for pid in player_ids:
        pid = str(pid)
        if pid in seen:
            continue
        seen.add(pid)
        row = conn.execute("SELECT p.full_name, r.team_id FROM nfl_players p LEFT JOIN rosters r USING (player_id) "
                           "WHERE p.player_id = ?", (pid,)).fetchone()
        if not row:
            problems.append(f"{pid}: unknown player_id")
        elif row["team_id"]:
            problems.append(f"{pid} ({row['full_name']}): already drafted by {row['team_id']}")
        else:
            valid.append(pid)
    with conn:
        conn.execute("DELETE FROM draft_queue WHERE team_id = ?", (team_id,))
        conn.executemany("INSERT INTO draft_queue VALUES (?, ?, ?)",
                         [(team_id, i + 1, pid) for i, pid in enumerate(valid)])
    return {"queued": len(valid), "skipped": problems}


def log_tool_call(conn: sqlite3.Connection, team_id: str, tool: str, args: str, ok: bool, summary: str) -> None:
    with conn:
        conn.execute("INSERT INTO tool_calls (team_id, at, tool, args, ok, summary) VALUES (?, ?, ?, ?, ?, ?)",
                     (team_id, now_utc(), tool, args, int(ok), summary[:500]))
