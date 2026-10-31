"""Run the live snake draft. Each team keeps one conversation for the whole draft.

    python -m aiffl.draft init [--seed N] [--rounds R]   # reset league, randomize order
    python -m aiffl.draft run                            # prep phase, then every pick, then wrap-up
    python -m aiffl.draft board                          # print the board so far

`run` is resumable: if it crashes or is stopped, run it again and it continues from the
current pick, resuming each team's existing conversation.
"""

import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiffl import db, league
from aiffl.config import LOGS_DIR, Team, load_league, load_teams, system_prompt
from aiffl.runners.sessions import Session, make_session

EASTERN = ZoneInfo("America/New_York")
PREP_BUDGET = {"web_search": 60, "fetch_page": 30}
PICK_BUDGET = {"web_search": 10, "fetch_page": 5}
WRAP_BUDGET = {"web_search": 10, "fetch_page": 5}

RUN_CONTEXT = (
    "Live snake draft. This one conversation lasts the whole draft: you will get a message each time "
    "you are on the clock. There is no lineup to set yet. On pick turns, make your pick with draft_player "
    "and keep set_draft_queue current; the end-of-run checklist applies at the end of the draft, when "
    "you will be asked to write summary.md. Save important reasoning to notes as you go: after the draft, "
    "future runs start fresh with only your notes."
)

print_lock = threading.Lock()


def say(line: str) -> None:
    with print_lock:
        print(line, flush=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with (LOGS_DIR / "draft_board.txt").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def now_eastern() -> str:
    return datetime.now(timezone.utc).astimezone(EASTERN).strftime("%A %Y-%m-%d %H:%M %Z")


# Sessions ------------------------------------------------------------------------------------

def load_sessions(conn: sqlite3.Connection, teams: dict[str, Team]) -> dict[str, Session]:
    saved = {r["team_id"]: r["session_id"]
             for r in conn.execute("SELECT team_id, session_id FROM agent_sessions WHERE phase = 'draft'")}
    prompt = lambda t: system_prompt(t, len(teams), RUN_CONTEXT)
    return {tid: make_session(t, prompt(t), "draft", saved.get(tid)) for tid, t in teams.items()}


def save_session(conn: sqlite3.Connection, s: Session, prepped: bool | None = None) -> None:
    with conn:
        conn.execute("""INSERT INTO agent_sessions (team_id, phase, harness, session_id) VALUES (?, 'draft', ?, ?)
                        ON CONFLICT (team_id, phase) DO UPDATE SET session_id = excluded.session_id""",
                     (s.team.id, s.harness, s.session_id))
        if prepped is not None:
            conn.execute("UPDATE agent_sessions SET prepped = ? WHERE team_id = ? AND phase = 'draft'",
                         (int(prepped), s.team.id))


# Messages ------------------------------------------------------------------------------------

def prep_message(conn: sqlite3.Connection, team_id: str, prep_minutes: int) -> str:
    cfg = load_league()
    order = league.draft_order(conn)
    rounds = league.draft_state(conn)["rounds"]
    mine = league.team_pick_numbers(conn, team_id, remaining_only=False)
    sched = cfg["schedule"]
    return f"""DRAFT PREP. Current time: {now_eastern()}.

This is a {len(order)}-team, {rounds}-round snake draft, and this conversation lasts the entire draft.
Round 1 order: {", ".join(f"{i + 1}. {t}" for i, t in enumerate(order))} (the order reverses every round).
You are {team_id}. Your overall pick numbers: {mine}.

Your {rounds} picks are your whole roster: starters {cfg["roster"]["starters"]} plus {cfg["roster"]["bench"]} bench.
draft_player rejects any pick that would make a full starting lineup impossible.
The league's first scored week is Week {sched["start_week"]} (kickoff Thursday Nov 5, 2026, 8:15pm ET).
Regular season: weeks {sched["start_week"]}-{sched["regular_season_end_week"]}. Playoffs: weeks {sched["playoff_weeks"]}.
Free-agent bidding (FAAB, ${cfg["waivers"]["budget"]}) opens after the draft.

Now, before the draft starts:
1. Read the rules (get_league_info) and research the player pool (search_players, get_player).
2. Verify current injuries, roles, and news on the web. This is mid-season: a lot has changed.
3. Call set_draft_queue with your ranked board (at least 40 players). If you ever time out on the
   clock, the first available player in your queue is drafted for you.
4. Save your board and reasoning to a note (for example draft_board.md).

You have about {prep_minutes} minutes and {PREP_BUDGET["web_search"]} web searches for prep. Do not try to draft yet;
you will get a message when you are on the clock."""


def on_clock_message(conn: sqlite3.Connection, team_id: str, pick: sqlite3.Row, since_pick: int,
                     pick_minutes: int) -> str:
    recent = league.picks_made(conn, after_pick=since_pick)
    counts = league.position_counts(conn, team_id)
    mine = [n for n in league.team_pick_numbers(conn, team_id) if n != pick["pick_no"]]
    teams = len(league.draft_order(conn))
    q = league.queue(conn, team_id)[:8]
    lines = [f"YOU ARE ON THE CLOCK: pick {pick['pick_no']} (round {pick['round']}, "
             f"pick {(pick['pick_no'] - 1) % teams + 1} of the round). Current time: {now_eastern()}.", ""]
    lines.append("Picks since your last turn:" if recent else "No picks have been made since your last turn.")
    lines += [f"  {p['pick_no']}. {p['team_id']}: {p['full_name']} ({p['position']}, {p['team']})" for p in recent]
    lines += ["", "Your roster: " + (", ".join(f"{p['full_name']} ({p['position']})"
                                               for p in league.roster(conn, team_id)) or "empty"),
              f"Starting-lineup needs: {league.describe_needs(counts)}",
              "Your next pick after this one: " + (
                  "none, this is your last pick" if not mine else
                  f"#{mine[0]}, immediately after this one (you may make both picks this turn)"
                  if mine[0] == pick["pick_no"] + 1 else f"#{mine[0]} ({mine[0] - pick['pick_no']} picks from now)"),
              "Top of your queue still available: " + (", ".join(f"{p['full_name']} ({p['position']})" for p in q)
                                                       or "your queue is empty"),
              "",
              f"You have {pick_minutes} minutes and {PICK_BUDGET['web_search']} web searches. Re-verify anything that "
              f"may have changed, then call draft_player. Afterward, update set_draft_queue if your plans changed."]
    return "\n".join(lines)


def wrap_message(conn: sqlite3.Connection, team_id: str) -> str:
    roster = league.roster(conn, team_id)
    return f"""THE DRAFT IS COMPLETE. Current time: {now_eastern()}.

Your roster: {", ".join(f"{p['full_name']} ({p['position']}, {p.get('team')})" for p in roster)}

This is the last message in this conversation. Future runs start fresh and see only your notes.
Write summary.md now: your roster assessment, your strategy for the season, free-agent targets,
and what to verify before Week 9 lineups lock (the first game is Thursday Nov 5, 8:15pm ET).
Record the source and time for facts your plans rest on."""


# Phases --------------------------------------------------------------------------------------

def run_prep(conn: sqlite3.Connection, sessions: dict[str, Session], prep_minutes: int) -> None:
    done = {r["team_id"] for r in conn.execute("SELECT team_id FROM agent_sessions WHERE phase = 'draft' AND prepped = 1")}
    todo = [tid for tid in league.draft_order(conn) if tid not in done]
    if not todo:
        return
    say(f"== Prep phase: {len(todo)} teams researching in parallel (up to {prep_minutes} min) ==")
    messages = {tid: prep_message(conn, tid, prep_minutes) for tid in todo}

    def prep(tid):
        s = sessions[tid]
        s.budgets = PREP_BUDGET
        return tid, s.send(messages[tid], timeout_s=prep_minutes * 60)

    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        for tid, r in pool.map(prep, todo):
            save_session(conn, sessions[tid], prepped=True)
            queued = len(league.queue(conn, tid))
            say(f"   prep {tid} [{sessions[tid].team.model}]: {'ok' if r.ok else r.error} in {r.seconds / 60:.1f} min, "
                f"{queued} players queued")


def run_picks(conn: sqlite3.Connection, sessions: dict[str, Session], pick_minutes: int) -> None:
    def last_made(tid):
        return max([0] + [n for n in league.team_pick_numbers(conn, tid, remaining_only=False)
                          if n not in league.team_pick_numbers(conn, tid)])

    last_seen = {tid: last_made(tid) for tid in sessions}
    printed_through = max([0] + [p["pick_no"] for p in league.picks_made(conn)])
    say("== Draft is live ==")
    while (pick := league.current_pick(conn)) is not None:
        tid, s = pick["team_id"], sessions[pick["team_id"]]
        s.budgets = PICK_BUDGET
        msg = on_clock_message(conn, tid, pick, last_seen[tid], pick_minutes)

        def still_on_clock(n=pick["pick_no"]):
            row = conn.execute("SELECT player_id FROM draft_picks WHERE pick_no = ?", (n,)).fetchone()
            return None if row["player_id"] else (
                "You are still on the clock and have not drafted. Call draft_player now with your choice.")

        start = time.monotonic()
        r = s.send(msg, timeout_s=pick_minutes * 60, check_done=still_on_clock)
        save_session(conn, s)
        made = conn.execute("SELECT * FROM draft_picks WHERE pick_no = ?", (pick["pick_no"],)).fetchone()
        if not made["player_id"]:
            league.autopick(conn, tid)
        last_seen[tid] = last_made(tid)
        # A team on the clock twice in a row (the snake turn) may make both picks in one turn.
        note = "" if r.ok else f" ({r.error})"
        for p in league.picks_made(conn, after_pick=printed_through):
            say(f"{p['pick_no']:>4}. R{p['round']:<2} {p['team_id']} [{sessions[p['team_id']].team.model}]  "
                f"{p['full_name']} ({p['position']}, {p['team']})  - {p['method']}, "
                f"{time.monotonic() - start:.0f}s turn{note}")
            printed_through = p["pick_no"]


def run_wrap(conn: sqlite3.Connection, sessions: dict[str, Session]) -> None:
    say("== Wrap-up: every team writes summary.md ==")
    messages = {tid: wrap_message(conn, tid) for tid in sessions}

    def wrap(tid):
        s = sessions[tid]
        s.budgets = WRAP_BUDGET
        return tid, s.send(messages[tid], timeout_s=15 * 60)

    with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        for tid, r in pool.map(wrap, list(sessions)):
            wrote = (sessions[tid].team.notes_dir / "summary.md").exists()
            say(f"   wrap {tid}: {'ok' if r.ok else r.error}, summary.md {'written' if wrote else 'MISSING'}")


def print_board(conn: sqlite3.Connection) -> None:
    for p in league.picks_made(conn):
        print(f"{p['pick_no']:>4}. R{p['round']:<2} {p['team_id']}  {p['full_name']} ({p['position']}, {p['team']})"
              f"  - {p['method']}")
    pick = league.current_pick(conn)
    print(f"on the clock: {pick['team_id']} (pick {pick['pick_no']})" if pick else "draft complete")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("--seed", type=int)
    init.add_argument("--rounds", type=int)
    run = sub.add_parser("run")
    run.add_argument("--prep-minutes", type=int, default=30)
    run.add_argument("--pick-minutes", type=int, default=10)
    sub.add_parser("board")
    args = ap.parse_args()

    conn = db.connect()
    if args.cmd == "init":
        order = league.init_league(conn, load_teams(), rounds=args.rounds, seed=args.seed)
        print("draft order:", ", ".join(f"{i + 1}. {t}" for i, t in enumerate(order)))
        return
    if args.cmd == "board":
        print_board(conn)
        return

    state = league.draft_state(conn)
    if not state:
        raise SystemExit("No draft set up. Run: python -m aiffl.draft init")
    if state["status"] == "done":
        raise SystemExit("The draft is already complete.")
    teams = {t.id: t for t in load_teams()}
    sessions = load_sessions(conn, teams)
    if state["status"] in ("pending", "prep"):
        league.set_draft_status(conn, "prep")
        run_prep(conn, sessions, args.prep_minutes)
        league.set_draft_status(conn, "live")
    run_picks(conn, sessions, args.pick_minutes)
    league.set_draft_status(conn, "done")
    run_wrap(conn, sessions)
    say("== Draft complete ==")
    print_board(conn)


if __name__ == "__main__":
    main()
