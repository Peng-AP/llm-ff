"""One scheduler tick. Run it every 15 minutes (Windows Task Scheduler: scripts/install_scheduler.ps1).

    python -m aiffl.scheduler tick
    python -m aiffl.scheduler status

Each tick does whatever is due (times in league.yaml `automation`, US Eastern) and records it in
jobs_done, so a missed or repeated tick never runs a job twice. Agent runs and waivers that were
missed while the computer was asleep still run if the delay is within their grace period.
"""

import os
import sys
import traceback
from datetime import timedelta

from aiffl import clock, db, league, season, transactions
from aiffl.config import LOGS_DIR, ROOT, load_league
from aiffl.nfl import sync as nfl_sync
from aiffl.players import build_summary

GRACE = {"agents": timedelta(hours=2), "waivers": timedelta(hours=12), "players": timedelta(hours=8),
         "finalize": timedelta(hours=30), "recap": timedelta(hours=30)}


def log(line: str) -> None:
    stamp = clock.eastern()
    print(f"[{stamp}] {line}", flush=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with (LOGS_DIR / "scheduler.log").open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {line}\n")


def due(conn, job: str, spec: str) -> str | None:
    """The job key if `spec`'s latest occurrence is within the grace period and not yet done."""
    occ = clock.last_occurrence(spec)
    if clock.now() - occ > GRACE[job]:
        return None
    key = f"{job}:{occ.strftime(clock.ISO)}"
    return None if conn.execute("SELECT 1 FROM jobs_done WHERE key = ?", (key,)).fetchone() else key


def mark(conn, key: str, detail: str = "") -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO jobs_done VALUES (?, ?, ?)", (key, clock.now_iso(), detail[:1000]))


def acquire_lock():
    """Hold an OS-level lock for the life of this process so ticks never overlap."""
    path = ROOT / "data" / "scheduler.lock"
    path.parent.mkdir(exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def tick() -> None:
    lock = acquire_lock()
    if lock is None:
        log("another tick is still running; skipping")
        return
    conn = db.connect()
    cfg = load_league()
    auto = cfg["automation"]
    year = int(cfg["season"])
    state = league.draft_state(conn)
    if not state or state["status"] != "done":
        log("draft not finished; nothing to do")
        return
    if not conn.execute("SELECT 1 FROM matchups LIMIT 1").fetchone():
        season.start_season(conn)
        log("season started: schedule created, FAAB set, lineups filled")

    # 1. Data ----------------------------------------------------------------------------
    spec = auto["sync_players_sunday"] if clock.now().astimezone(clock.EASTERN).weekday() == 6 else auto["sync_players"]
    if key := due(conn, "players", spec):
        nfl_sync.sync_players(conn)
        build_summary(conn, year, clock.now_iso())
        conn.commit()
        mark(conn, key)
        log("synced players and injuries")

    week = season.current_week(conn)
    open_weeks = sorted({week} | {r[0] for r in conn.execute(
        "SELECT DISTINCT week FROM matchups WHERE status != 'final' AND week < ?", (week,))})
    for w in open_weeks:
        kickoff = season.first_kickoff(conn, w)
        if kickoff and clock.now() >= clock.parse(kickoff) - timedelta(hours=3):
            nfl_sync.sync_week_games(conn, year, w)
            nfl_sync.sync_week_stats(conn, year, w)
    # Commit now: an open write transaction would lock out every agent's MCP server during runs.
    conn.commit()
    week = season.current_week(conn)  # a just-finished game can roll the week over

    # 2. League housekeeping -------------------------------------------------------------
    if n := transactions.expire_trades(conn):
        log(f"expired {n} trade offer(s)")
    for note in transactions.auto_approve_trades(conn):
        log(note)
    if pending := transactions.pending_trades(conn):
        log(f"{len(pending)} trade(s) awaiting the commissioner: run python -m aiffl.commish")
    for spec in cfg["waivers"]["process"]:
        if key := due(conn, "waivers", spec):
            results = transactions.process_waivers(conn)
            mark(conn, key, "; ".join(results))
            log(f"waivers processed: {'; '.join(results) or 'no successful claims'}")
    for note in season.ensure_lineups(conn, week):
        log(f"lineup safety net: {note}")
    season.score_week(conn, week)

    # 3. Finalize finished weeks ---------------------------------------------------------
    if key := due(conn, "finalize", auto["finalize_week"]):
        finished = []
        for (w,) in conn.execute("SELECT DISTINCT week FROM matchups WHERE status != 'final' AND week < ?",
                                 (week,)).fetchall():
            nfl_sync.sync_week_stats(conn, year, w)  # pick up stat corrections
            if season.finalize_week(conn, w):
                finished.append(w)
        build_summary(conn, year, clock.now_iso())
        conn.commit()
        mark(conn, key, f"finalized {finished}")
        log(f"finalized weeks {finished}" if finished else "finalize: nothing to finalize")

    # 4. Recap ---------------------------------------------------------------------------
    if key := due(conn, "recap", auto["recap"]):
        last = conn.execute("SELECT MAX(week) FROM matchups WHERE status = 'final'").fetchone()[0]
        if last and not conn.execute("SELECT 1 FROM jobs_done WHERE key = ?", (f"recap-week:{last}",)).fetchone():
            from aiffl import recap
            path = recap.write_recap(conn, last)
            mark(conn, f"recap-week:{last}", str(path))
            log(f"recap written: {path}")
        mark(conn, key)

    # 5. Agent runs ----------------------------------------------------------------------
    slots = [(s, due(conn, "agents", s["at"])) for s in auto["agent_runs"]]
    slots = [(s, k) for s, k in slots if k]
    if slots and os.environ.get("AIFFL_NO_AGENTS"):
        log(f"agent run due ({slots[-1][0]['why']}) but AIFFL_NO_AGENTS is set; not running")
        slots = []
    if slots:
        # If several were missed, run only the latest; mark the rest done.
        slots.sort(key=lambda sk: sk[1])
        for _, k in slots[:-1]:
            mark(conn, k, "skipped: superseded by a later run")
        slot, key = slots[-1]
        mark(conn, key, "started")  # mark first so a crash mid-run doesn't relaunch 8 agents
        log(f"agent runs: {slot['why']}")
        from aiffl import agents
        for r in agents.run_all(slot["why"]):
            log(f"   {r['team']} [{r['model']}]: {'ok' if r['ok'] else r['error']}, {r['minutes']} min, ${r['cost_usd']}")
        for note in season.ensure_lineups(conn, season.current_week(conn)):
            log(f"lineup safety net: {note}")
    elif not os.environ.get("AIFFL_NO_AGENTS"):
        # No scheduled run this tick: wake any team with a trade offer or message waiting for it.
        from aiffl import agents
        if wakes := agents.due_wakes(conn):
            for tid, items in wakes.items():
                log(f"waking {tid}: {'; '.join(items)}")
            for r in agents.run_wakes(wakes):
                log(f"   {r['team']} [{r['model']}]: {'ok' if r['ok'] else r['error']}, {r['minutes']} min")
            for note in season.ensure_lineups(conn, season.current_week(conn)):
                log(f"lineup safety net: {note}")

    # 6. Public site ---------------------------------------------------------------------
    publish_site(conn, cfg)
    conn.close()


def publish_site(conn, cfg: dict) -> None:
    from aiffl import publish
    digest = publish.build(conn)
    site = cfg.get("site", {})
    if not site.get("deploy"):
        return
    last = conn.execute("SELECT done_at, detail FROM jobs_done WHERE key = 'site:last-deploy'").fetchone()
    if last and last["detail"] == digest:
        return  # nothing changed
    week = season.current_week(conn)
    live = conn.execute("SELECT 1 FROM nfl_games WHERE season = ? AND week = ? AND state = 'in' LIMIT 1",
                        (int(cfg["season"]), week)).fetchone()
    gap = site["min_minutes_between_deploys_live" if live else "min_minutes_between_deploys"]
    if last and clock.now() - clock.parse(last["done_at"]) < timedelta(minutes=gap):
        return
    try:
        publish.deploy()
        mark(conn, "site:last-deploy", digest)
        log("site deployed")
    except Exception as e:  # a failed deploy must never break the league
        log(f"site deploy failed: {e}")


def status() -> None:
    conn = db.connect()
    week = season.current_week(conn)
    print(f"now: {clock.eastern()}   week: {week}")
    for s in season.standings(conn):
        print(f"  {s['rank']}. {s['team_id']}  {s['wins']}-{s['losses']}-{s['ties']}  PF {s['points_for']}")
    for m in conn.execute("SELECT * FROM matchups WHERE week = ?", (week,)):
        print(f"  {m['team_a']} {m['score_a']:.1f} - {m['score_b']:.1f} {m['team_b']}  ({m['status']})")
    print("recent jobs:")
    for r in conn.execute("SELECT * FROM jobs_done ORDER BY done_at DESC LIMIT 10"):
        print(f"  {r['done_at']}  {r['key']}  {r['detail'] or ''}"[:160])


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        try:
            tick()
        except Exception:
            log("TICK FAILED:\n" + traceback.format_exc())
            raise
    elif cmd == "status":
        status()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
