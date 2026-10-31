"""Season runs: every team gets a fresh conversation that rebuilds context from its notes.

Scheduled runs wake every team at fixed times. Event wakes wake one team early when something is
addressed to it that it hasn't seen: a trade offer, a direct message, or a board post naming it.

    python -m aiffl.agents "Why this run happens" [--teams team_01 team_05] [--minutes 20]
    python -m aiffl.agents --pending      # what would wake each team right now
"""

import argparse
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from aiffl import clock, db, season
from aiffl.config import Team, load_league, load_teams, system_prompt
from aiffl.runners.sessions import make_session

SEASON_BUDGET = {"web_search": 25, "fetch_page": 15}
RUN_STALE_MINUTES = 60  # a run row with no end after this long is treated as dead, not in progress


def run_message(conn, why: str, header: str = "SCHEDULED RUN") -> str:
    from aiffl import transactions
    week = season.current_week(conn)
    return f"""{header}: {why}
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


def run_team(team: Team, why: str, minutes: int, num_teams: int, header: str = "SCHEDULED RUN") -> dict:
    conn = db.connect()
    week = season.current_week(conn)
    stamp = clock.now().strftime("%Y%m%d-%H%M")
    context = f"Season run: {why}"
    session = make_session(team, system_prompt(team, num_teams, context), "season",
                           log_name=f"runs/{stamp}_{team.id}.jsonl")
    session.budgets = SEASON_BUDGET
    started_iso, started = clock.now_iso(), time.time()
    # Recorded at the start (seconds stays NULL until it ends) so the scheduler can see it in progress.
    with conn:
        run_id = conn.execute("INSERT INTO agent_runs (team_id, why, started_at) VALUES (?, ?, ?)",
                              (team.id, why, started_iso)).lastrowid
    try:
        r = session.send(run_message(conn, why, header), timeout_s=minutes * 60,
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
        conn.execute("UPDATE agent_runs SET seconds = ?, ok = ?, error = ?, cost_usd = ?, turns = ? WHERE id = ?",
                     (secs, int(ok), error, cost, turns, run_id))
    conn.close()
    return {"team": team.id, "model": team.model, "ok": ok, "error": error, "minutes": round(secs / 60, 1),
            "cost_usd": round(cost, 4)}


def run_all(why: str, team_ids: list[str] | None = None, minutes: int | None = None) -> list[dict]:
    teams = [t for t in load_teams() if not team_ids or t.id in team_ids]
    minutes = minutes or load_league()["automation"]["agent_run_minutes"]
    with ThreadPoolExecutor(max_workers=len(teams)) as pool:
        return list(pool.map(lambda t: run_team(t, why, minutes, len(load_teams())), teams))


# Event wakes ----------------------------------------------------------------------------------

def last_run_start(conn, team_id: str) -> str:
    return conn.execute("SELECT COALESCE(MAX(started_at), '') FROM agent_runs WHERE team_id = ?",
                        (team_id,)).fetchone()[0]


def running(conn, team_id: str) -> bool:
    stale = (clock.now() - timedelta(minutes=RUN_STALE_MINUTES)).strftime(clock.ISO)
    return bool(conn.execute("SELECT 1 FROM agent_runs WHERE team_id = ? AND seconds IS NULL AND started_at > ?",
                             (team_id, stale)).fetchone())


def pending_items(conn, team_id: str) -> list[str]:
    """What is addressed to this team that arrived after its last run started."""
    since = last_run_start(conn, team_id)
    items = [f"trade offer #{t['id']} from {t['from_team']} (expires {t['expires_at']})" for t in conn.execute(
        "SELECT * FROM trades WHERE to_team = ? AND status = 'open' AND created_at > ?", (team_id, since))]
    for m in conn.execute("SELECT * FROM messages WHERE to_team = ? AND at > ?", (team_id, since)):
        items.append(f"direct message from {m['from_team']} at {m['at']}")
    mention = re.compile(rf"\b{re.escape(team_id)}\b")
    for m in conn.execute("SELECT * FROM messages WHERE to_team IS NULL AND from_team != ? AND at > ?",
                          (team_id, since)):
        if mention.search(m["text"]):
            items.append(f"message-board post from {m['from_team']} naming you, at {m['at']}")
    return items


def due_wakes(conn) -> dict[str, list[str]]:
    """Teams to wake now, with what they are woken for. Applies the cooldown and daily cap."""
    cfg = load_league()["automation"].get("event_wakes", {})
    if not cfg.get("enabled"):
        return {}
    now = clock.now()
    day_start = now.astimezone(clock.EASTERN).replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_start.astimezone(clock.now().tzinfo).strftime(clock.ISO)
    wakes = {}
    for t in load_teams():
        items = pending_items(conn, t.id)
        if not items or running(conn, t.id):
            continue
        last = last_run_start(conn, t.id)
        if last and now - clock.parse(last) < timedelta(minutes=cfg.get("cooldown_minutes", 60)):
            continue
        woken_today = conn.execute("SELECT COUNT(*) FROM agent_runs WHERE team_id = ? AND why LIKE 'Woken for:%' "
                                   "AND started_at >= ?", (t.id, day_start)).fetchone()[0]
        if woken_today >= cfg.get("max_per_day", 6):
            continue
        wakes[t.id] = items
    return wakes


def run_wakes(wakes: dict[str, list[str]]) -> list[dict]:
    minutes = load_league()["automation"].get("event_wakes", {}).get("minutes", 12)
    teams = {t.id: t for t in load_teams()}

    def wake(tid):
        why = "Woken for: " + "; ".join(wakes[tid]) + ". Deal with these first; anything else is optional."
        return run_team(teams[tid], why, minutes, len(teams), header="WOKEN UP")

    with ThreadPoolExecutor(max_workers=len(wakes)) as pool:
        return list(pool.map(wake, list(wakes)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("why", nargs="?")
    ap.add_argument("--teams", nargs="*")
    ap.add_argument("--minutes", type=int)
    ap.add_argument("--pending", action="store_true", help="show what would wake each team now")
    args = ap.parse_args()
    if args.pending:
        conn = db.connect()
        for t in load_teams():
            print(f"{t.id}: running={running(conn, t.id)} items={pending_items(conn, t.id)}")
        print("would wake now:", due_wakes(conn))
        return
    if not args.why:
        ap.error("give a reason for the run, or --pending")
    for r in run_all(args.why, args.teams, args.minutes):
        print(r)


if __name__ == "__main__":
    main()
