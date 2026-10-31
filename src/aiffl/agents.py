"""Scheduled season runs: every team gets a fresh conversation that rebuilds context from its notes.

    python -m aiffl.agents "Why this run happens" [--teams team_01 team_05] [--minutes 20]
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor

from aiffl import clock, db, season
from aiffl.config import Team, load_league, load_teams, system_prompt
from aiffl.runners.sessions import make_session

SEASON_BUDGET = {"web_search": 25, "fetch_page": 15}


def run_message(conn, why: str) -> str:
    from aiffl import transactions
    week = season.current_week(conn)
    return f"""SCHEDULED RUN: {why}
Current time: {clock.eastern()}. Fantasy week {week}.

Start with get_briefing, then read your notes (summary.md first). Then act on whatever needs it:
- your lineup (each player locks at his own game's kickoff)
- free-agent bids (next waiver processing: {transactions.next_processing()})
- trade offers and messages from other teams
Verify before you act. Finish by updating summary.md."""


TIME_UP = ("Your time for this run has run out. In as few steps as possible: make sure your lineup has no "
           "empty, injured-out, or bye-week starters that can still be changed, then write summary.md. "
           "Do no new research.")


def finish_check(team: Team, started: float, started_iso: str, week: int):
    """Reminder if the run is ending with summary.md unwritten, nothing verified on the web, or an
    obviously broken lineup. Same check for every team; it fires at most once per run."""
    def check():
        problems = []
        summary = team.notes_dir / "summary.md"
        if not summary.exists() or summary.stat().st_mtime < started:
            problems.append("write summary.md (required every run)")
        conn = db.connect()
        searches = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE team_id = ? AND tool = 'web_search' "
                                "AND at >= ?", (team.id, started_iso)).fetchone()[0]
        if searches == 0:
            problems.append("you have not run any web_search this run, so nothing about your players' current "
                            "injuries, roles, or news has been verified")
        for slot, p in season.lineup_detail(conn, team.id, week)["starters"].items():
            if p is None:
                problems.append(f"{slot} is empty")
            elif not p["locked"] and (p["game"].startswith("BYE") or p["injury_status"] in season.UNAVAILABLE):
                problems.append(f"{slot} starts {p['name']} ({p['injury_status'] or 'no game this week'})")
        conn.close()
        return ("Before you finish: " + "; ".join(problems) + ".") if problems else None
    return check


def run_team(team: Team, why: str, minutes: int, num_teams: int) -> dict:
    conn = db.connect()
    week = season.current_week(conn)
    stamp = clock.now().strftime("%Y%m%d-%H%M")
    context = f"Scheduled season run: {why}"
    session = make_session(team, system_prompt(team, num_teams, context), "season",
                           log_name=f"runs/{stamp}_{team.id}.jsonl")
    session.budgets = SEASON_BUDGET
    started_iso, started = clock.now_iso(), time.time()
    try:
        r = session.send(run_message(conn, why), timeout_s=minutes * 60,
                         check_done=finish_check(team, started, started_iso, week))
        ok, error, cost, turns, secs = r.ok, r.error, r.cost_usd, r.turns, r.seconds
        if error == "timeout":
            # One short extra turn so a run that ran long still leaves its notes behind.
            extra = session.send(TIME_UP, timeout_s=4 * 60)
            cost, turns, secs = cost + extra.cost_usd, turns + extra.turns, secs + extra.seconds
            error = "timeout (wrap-up turn ok)" if extra.ok else f"timeout; wrap-up turn: {extra.error}"
    except Exception as e:
        ok, error, cost, turns, secs = False, f"{type(e).__name__}: {e}", 0.0, 0, time.time() - started
    with conn:
        conn.execute("INSERT INTO agent_runs (team_id, why, started_at, seconds, ok, error, cost_usd, turns) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (team.id, why, started_iso, secs, int(ok), error, cost, turns))
    conn.close()
    return {"team": team.id, "model": team.model, "ok": ok, "error": error, "minutes": round(secs / 60, 1),
            "cost_usd": round(cost, 4)}


def run_all(why: str, team_ids: list[str] | None = None, minutes: int | None = None) -> list[dict]:
    teams = [t for t in load_teams() if not team_ids or t.id in team_ids]
    minutes = minutes or load_league()["automation"]["agent_run_minutes"]
    with ThreadPoolExecutor(max_workers=len(teams)) as pool:
        return list(pool.map(lambda t: run_team(t, why, minutes, len(load_teams())), teams))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("why")
    ap.add_argument("--teams", nargs="*")
    ap.add_argument("--minutes", type=int)
    args = ap.parse_args()
    for r in run_all(args.why, args.teams, args.minutes):
        print(r)


if __name__ == "__main__":
    main()
