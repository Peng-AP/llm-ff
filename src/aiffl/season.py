"""The season: weeks, per-game lineup locks, lineups, scoring, matchups, standings, playoffs."""

import json
import sqlite3
from collections import defaultdict

from aiffl import clock
from aiffl.config import load_league
from aiffl.league import LeagueError
from aiffl.players import LAST_REGULAR_WEEK
from aiffl.scoring import fantasy_points

UNAVAILABLE = {"Out", "IR", "PUP", "Sus", "NA", "DNR", "COV"}  # never auto-started


def cfg() -> dict:
    return load_league()


def season_year() -> int:
    return int(cfg()["season"])


# Weeks and locks -----------------------------------------------------------------------------

def current_week(conn: sqlite3.Connection) -> int:
    """The fantasy week in play: the first league week with a game not yet final."""
    start = cfg()["schedule"]["start_week"]
    last = max(cfg()["schedule"]["playoff_weeks"])
    row = conn.execute("SELECT MIN(week) FROM nfl_games WHERE season = ? AND week BETWEEN ? AND ? AND state != 'post'",
                       (season_year(), start, last)).fetchone()
    return row[0] if row[0] is not None else last


def game_for(conn: sqlite3.Connection, nfl_team: str | None, week: int) -> sqlite3.Row | None:
    if not nfl_team:
        return None
    return conn.execute("SELECT * FROM nfl_games WHERE season = ? AND week = ? AND (home = ? OR away = ?)",
                        (season_year(), week, nfl_team, nfl_team)).fetchone()


def is_locked(conn: sqlite3.Connection, player_id: str | None, week: int) -> bool:
    if not player_id:
        return False
    team = conn.execute("SELECT team FROM nfl_players WHERE player_id = ?", (player_id,)).fetchone()
    game = game_for(conn, team["team"] if team else None, week)
    return bool(game and clock.parse(game["kickoff_utc"]) <= clock.now())


def first_kickoff(conn: sqlite3.Connection, week: int) -> str | None:
    return conn.execute("SELECT MIN(kickoff_utc) FROM nfl_games WHERE season = ? AND week = ?",
                        (season_year(), week)).fetchone()[0]


# Slots ---------------------------------------------------------------------------------------

def slots() -> list[tuple[str, set[str]]]:
    """Lineup slots in order, with the positions each accepts."""
    out, seen = [], defaultdict(int)
    starters = cfg()["roster"]["starters"]
    for s in starters:
        pos = "DEF" if s == "DST" else s
        if s == "FLEX":
            out.append(("FLEX", {"RB", "WR", "TE"}))
            continue
        seen[pos] += 1
        name = f"{pos}{seen[pos]}" if starters.count(s) > 1 else pos
        out.append((name, {pos}))
    return out


def slot_positions() -> dict[str, set[str]]:
    return dict(slots())


# Roster helpers ------------------------------------------------------------------------------

def roster_rows(conn: sqlite3.Connection, team_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.player_id, p.full_name, p.position, p.team, p.injury_status, p.status, r.on_ir
           FROM rosters r JOIN nfl_players p USING (player_id) WHERE r.team_id = ?""", (team_id,)).fetchall()


def week_projection(conn: sqlite3.Connection, player_id: str, position: str, week: int) -> float:
    row = conn.execute("SELECT stats FROM nfl_projections WHERE season = ? AND week = ? AND player_id = ?",
                       (season_year(), week, player_id)).fetchone()
    return fantasy_points(position, json.loads(row["stats"]), cfg()["scoring"], projected=True) if row else 0.0


def player_points(conn: sqlite3.Connection, player_id: str, week: int) -> float:
    row = conn.execute(
        "SELECT s.stats, p.position FROM nfl_stats s JOIN nfl_players p USING (player_id) "
        "WHERE s.season = ? AND s.week = ? AND s.player_id = ?", (season_year(), week, player_id)).fetchone()
    return fantasy_points(row["position"], json.loads(row["stats"]), cfg()["scoring"]) if row else 0.0


# Lineups -------------------------------------------------------------------------------------

def get_lineup(conn: sqlite3.Connection, team_id: str, week: int) -> dict[str, str | None]:
    rows = {r["slot"]: r["player_id"] for r in
            conn.execute("SELECT slot, player_id FROM lineups WHERE week = ? AND team_id = ?", (week, team_id))}
    return {name: rows.get(name) for name, _ in slots()}


def lineup_detail(conn: sqlite3.Connection, team_id: str, week: int) -> dict:
    """Lineup and bench with lock state, status, projection, and points so far."""
    lineup = get_lineup(conn, team_id, week)
    roster = {r["player_id"]: r for r in roster_rows(conn, team_id)}
    starters = set(filter(None, lineup.values()))

    def describe(pid):
        p = roster.get(pid)
        if not p:
            return None
        game = game_for(conn, p["team"], week)
        return {"player_id": pid, "name": p["full_name"], "position": p["position"], "nfl_team": p["team"],
                "injury_status": p["injury_status"], "on_ir": bool(p["on_ir"]),
                "game": (f"{game['away']} @ {game['home']} {clock.eastern(clock.parse(game['kickoff_utc']))}"
                         if game else "BYE / no game"),
                "locked": is_locked(conn, pid, week),
                "projected": round(week_projection(conn, pid, p["position"], week), 2),
                "points": round(player_points(conn, pid, week), 2)}

    return {"week": week,
            "starters": {slot: describe(pid) for slot, pid in lineup.items()},
            "bench": [describe(pid) for pid in roster if pid not in starters and not roster[pid]["on_ir"]],
            "ir": [describe(pid) for pid in roster if roster[pid]["on_ir"]]}


def _write_lineup(conn: sqlite3.Connection, team_id: str, week: int, lineup: dict, set_by: dict[str, str] | str):
    ts = clock.now_iso()
    with conn:
        for slot, pid in lineup.items():
            by = set_by if isinstance(set_by, str) else set_by.get(slot, "auto")
            conn.execute("""INSERT INTO lineups VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT (week, team_id, slot) DO UPDATE SET
                                player_id = excluded.player_id, set_by = excluded.set_by, set_at = excluded.set_at""",
                         (week, team_id, slot, pid, by, ts))


def set_lineup(conn: sqlite3.Connection, team_id: str, week: int, changes: dict[str, str | None]) -> dict:
    """Apply slot -> player_id changes (None empties a slot). Enforces positions and per-game locks."""
    current_week_no = current_week(conn)
    if week not in (current_week_no, current_week_no + 1):
        raise LeagueError(f"You can set lineups for week {current_week_no} or {current_week_no + 1}, not {week}.")
    allowed = slot_positions()
    unknown = [s for s in changes if s not in allowed]
    if unknown:
        raise LeagueError(f"Unknown slot(s) {unknown}. Slots: {list(allowed)}.")
    roster = {r["player_id"]: r for r in roster_rows(conn, team_id)}
    old = get_lineup(conn, team_id, week)
    new = dict(old)
    for slot, pid in changes.items():
        new[slot] = str(pid) if pid else None

    for slot, pid in new.items():
        if pid == old[slot]:
            continue
        if is_locked(conn, old[slot], week):
            raise LeagueError(f"{slot}: {roster[old[slot]]['full_name'] if old[slot] in roster else old[slot]} "
                              f"is locked (his game has started) and cannot be moved.")
        if pid is None:
            continue
        p = roster.get(pid)
        if not p:
            raise LeagueError(f"{slot}: player {pid} is not on your roster.")
        if p["on_ir"]:
            raise LeagueError(f"{slot}: {p['full_name']} is on your IR. Activate him first.")
        if p["position"] not in allowed[slot]:
            raise LeagueError(f"{slot}: {p['full_name']} is a {p['position']}; {slot} takes {sorted(allowed[slot])}.")
        if is_locked(conn, pid, week) and pid not in old.values():
            raise LeagueError(f"{slot}: {p['full_name']}'s game has started; he cannot be moved into the lineup.")
    used = [pid for pid in new.values() if pid]
    dupes = {pid for pid in used if used.count(pid) > 1}
    if dupes:
        raise LeagueError(f"Each player can fill only one slot: {[roster[d]['full_name'] for d in dupes]}.")

    _write_lineup(conn, team_id, week, new, {s: "agent" for s in new})
    warnings = []
    for slot, pid in new.items():
        if not pid:
            warnings.append(f"{slot} is empty.")
            continue
        p = roster[pid]
        if not game_for(conn, p["team"], week):
            warnings.append(f"{slot}: {p['full_name']} has no game in week {week} (bye or no team).")
        elif p["injury_status"] in UNAVAILABLE:
            warnings.append(f"{slot}: {p['full_name']} is listed {p['injury_status']}.")
    return {"ok": True, "week": week, "lineup": lineup_detail(conn, team_id, week)["starters"], "warnings": warnings}


def best_lineup(conn: sqlite3.Connection, team_id: str, week: int, keep: dict[str, str | None]) -> dict:
    """Fill every slot not in `keep` with the best available player by that week's projection,
    skipping players who are unavailable, on bye, on IR, or whose game has started."""
    lineup = dict(keep)
    used = set(filter(None, keep.values()))
    candidates = []
    for r in roster_rows(conn, team_id):
        if r["player_id"] in used or r["on_ir"] or r["injury_status"] in UNAVAILABLE:
            continue
        if not game_for(conn, r["team"], week) or is_locked(conn, r["player_id"], week):
            continue
        candidates.append((week_projection(conn, r["player_id"], r["position"], week), r["player_id"], r["position"]))
    candidates.sort(reverse=True)
    # Fixed-position slots first, FLEX last, so FLEX gets the best leftover.
    order = [s for s, pos in slots() if s != "FLEX" and s not in keep] + (["FLEX"] if "FLEX" not in keep else [])
    allowed = slot_positions()
    for slot in order:
        pick = next((c for c in candidates if c[2] in allowed[slot] and c[1] not in used), None)
        lineup[slot] = pick[1] if pick else None
        if pick:
            used.add(pick[1])
    return lineup


def ensure_lineups(conn: sqlite3.Connection, week: int) -> list[str]:
    """Safety net, run every scheduler tick. Locked slots are never touched. Otherwise:
    - slots the agent set this week are respected (only emptied or departed players get replaced);
    - with no lineup yet this week, slots the agent chose last week carry over if still playable;
    - every other slot is filled with the best available player by projection."""
    notes = []
    for tid in [r[0] for r in conn.execute("SELECT team_id FROM league_teams")]:
        roster = {r["player_id"]: r for r in roster_rows(conn, tid)}
        rows = conn.execute("SELECT slot, player_id, set_by FROM lineups WHERE week = ? AND team_id = ?",
                            (week, tid)).fetchall()
        source = rows or conn.execute("SELECT slot, player_id, set_by FROM lineups WHERE week = ? AND team_id = ?",
                                      (week - 1, tid)).fetchall()
        keep, agent_slots = {}, set()
        for r in source:
            pid, slot = r["player_id"], r["slot"]
            if not pid or pid not in roster or roster[pid]["on_ir"]:
                continue
            playable = roster[pid]["injury_status"] not in UNAVAILABLE and game_for(conn, roster[pid]["team"], week)
            if rows and is_locked(conn, pid, week):
                keep[slot] = pid
                if r["set_by"] == "agent":
                    agent_slots.add(slot)
            elif rows and r["set_by"] == "agent":
                keep[slot] = pid
                agent_slots.add(slot)
            elif not rows and r["set_by"] == "agent" and playable:
                keep[slot] = pid                      # carried over from last week; not re-confirmed
        new = best_lineup(conn, tid, week, keep)
        current = get_lineup(conn, tid, week)
        if new != current or not rows:
            changed = sorted(s for s in new if new[s] != current.get(s))
            _write_lineup(conn, tid, week, new, {s: ("agent" if s in agent_slots else "auto") for s in new})
            if not rows:
                how = "last week's agent choices carried over" if keep else "all slots by projection"
                notes.append(f"{tid}: week {week} lineup created ({how})")
            elif changed:
                notes.append(f"{tid}: auto-set {changed}")
    return notes


# Schedule, scoring, standings ---------------------------------------------------------------

def create_schedule(conn: sqlite3.Connection) -> None:
    """Round robin over the regular season (circle method), in draft order."""
    sched = cfg()["schedule"]
    teams = [r[0] for r in conn.execute("SELECT team_id FROM league_teams ORDER BY draft_slot")]
    if len(teams) % 2:
        raise LeagueError("Need an even number of teams.")
    weeks = list(range(sched["start_week"], sched["regular_season_end_week"] + 1))
    fixed, rotating = teams[0], teams[1:]
    with conn:
        conn.execute("DELETE FROM matchups")
        for i, week in enumerate(weeks):
            r = rotating[i % len(rotating):] + rotating[:i % len(rotating)]
            ring = [fixed] + r
            for k in range(len(ring) // 2):
                a, b = ring[k], ring[-1 - k]
                if i % 2:
                    a, b = b, a
                conn.execute("INSERT INTO matchups (week, team_a, team_b, kind) VALUES (?, ?, ?, 'regular')",
                             (week, a, b))


def lineup_points(conn: sqlite3.Connection, team_id: str, week: int) -> float:
    return round(sum(player_points(conn, pid, week) for pid in get_lineup(conn, team_id, week).values() if pid), 2)


def score_week(conn: sqlite3.Connection, week: int) -> None:
    started = first_kickoff(conn, week)
    live = bool(started and clock.parse(started) <= clock.now())
    with conn:
        for m in conn.execute("SELECT * FROM matchups WHERE week = ? AND status != 'final'", (week,)).fetchall():
            conn.execute("UPDATE matchups SET score_a = ?, score_b = ?, status = ? WHERE week = ? AND team_a = ?",
                         (lineup_points(conn, m["team_a"], week), lineup_points(conn, m["team_b"], week),
                          "live" if live else "scheduled", week, m["team_a"]))


def week_complete(conn: sqlite3.Connection, week: int) -> bool:
    row = conn.execute("SELECT COUNT(*), SUM(state = 'post') FROM nfl_games WHERE season = ? AND week = ?",
                       (season_year(), week)).fetchone()
    return row[0] > 0 and row[0] == row[1]


def finalize_week(conn: sqlite3.Connection, week: int) -> bool:
    if not week_complete(conn, week):
        return False
    score_week(conn, week)
    with conn:
        conn.execute("UPDATE matchups SET status = 'final' WHERE week = ?", (week,))
    sched = cfg()["schedule"]
    playoff_weeks = sched["playoff_weeks"]
    if week == sched["regular_season_end_week"]:
        seeds = [s["team_id"] for s in standings(conn)][: sched["playoff_teams"]]
        with conn:
            conn.execute("INSERT OR IGNORE INTO matchups (week, team_a, team_b, kind) VALUES (?, ?, ?, 'semifinal')",
                         (playoff_weeks[0], seeds[0], seeds[3]))
            conn.execute("INSERT OR IGNORE INTO matchups (week, team_a, team_b, kind) VALUES (?, ?, ?, 'semifinal')",
                         (playoff_weeks[0], seeds[1], seeds[2]))
    elif len(playoff_weeks) > 1 and week == playoff_weeks[0]:
        winners, losers = [], []
        for m in conn.execute("SELECT * FROM matchups WHERE week = ? AND kind = 'semifinal' ORDER BY rowid", (week,)):
            a_wins = m["score_a"] >= m["score_b"]  # a is the higher seed and keeps an exact tie
            winners.append(m["team_a"] if a_wins else m["team_b"])
            losers.append(m["team_b"] if a_wins else m["team_a"])
        with conn:
            conn.execute("INSERT OR IGNORE INTO matchups (week, team_a, team_b, kind) VALUES (?, ?, ?, 'final')",
                         (playoff_weeks[1], winners[0], winners[1]))
            conn.execute("INSERT OR IGNORE INTO matchups (week, team_a, team_b, kind) VALUES (?, ?, ?, 'third_place')",
                         (playoff_weeks[1], losers[0], losers[1]))
    return True


def standings(conn: sqlite3.Connection) -> list[dict]:
    table = {r[0]: {"team_id": r[0], "wins": 0, "losses": 0, "ties": 0, "points_for": 0.0, "points_against": 0.0}
             for r in conn.execute("SELECT team_id FROM league_teams")}
    for m in conn.execute("SELECT * FROM matchups WHERE kind = 'regular' AND status = 'final'"):
        for me, them, pf, pa in ((m["team_a"], m["team_b"], m["score_a"], m["score_b"]),
                                 (m["team_b"], m["team_a"], m["score_b"], m["score_a"])):
            t = table[me]
            t["points_for"] += pf
            t["points_against"] += pa
            t["wins" if pf > pa else "losses" if pf < pa else "ties"] += 1
    rows = sorted(table.values(), key=lambda t: (-(t["wins"] + 0.5 * t["ties"]), -t["points_for"]))
    for i, t in enumerate(rows):
        t["rank"] = i + 1
        t["points_for"], t["points_against"] = round(t["points_for"], 2), round(t["points_against"], 2)
    return rows


def matchup_for(conn: sqlite3.Connection, team_id: str, week: int) -> dict | None:
    m = conn.execute("SELECT * FROM matchups WHERE week = ? AND (team_a = ? OR team_b = ?)",
                     (week, team_id, team_id)).fetchone()
    if not m:
        return None
    mine_is_a = m["team_a"] == team_id
    return {"week": week, "kind": m["kind"], "status": m["status"],
            "opponent": m["team_b"] if mine_is_a else m["team_a"],
            "your_score": m["score_a"] if mine_is_a else m["score_b"],
            "opponent_score": m["score_b"] if mine_is_a else m["score_a"]}


def start_season(conn: sqlite3.Connection) -> None:
    """Run once after the draft: schedule, FAAB budgets, and week-one lineups."""
    budget = cfg()["waivers"]["budget"]
    with conn:
        conn.execute("UPDATE league_teams SET faab = ? WHERE faab IS NULL", (budget,))
    if not conn.execute("SELECT 1 FROM matchups LIMIT 1").fetchone():
        create_schedule(conn)
    ensure_lineups(conn, current_week(conn))


def last_week() -> int:
    return min(LAST_REGULAR_WEEK, max(cfg()["schedule"]["playoff_weeks"]))
