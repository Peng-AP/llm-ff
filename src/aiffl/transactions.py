"""Roster moves: blind FAAB waivers, drops, IR, trades, and league messages."""

import json
import sqlite3
from datetime import timedelta

from aiffl import clock, season
from aiffl.config import load_league
from aiffl.league import LeagueError, roster_size

MAX_PENDING_CLAIMS = 15
MAX_MESSAGE_CHARS = 2000


def _log(conn: sqlite3.Connection, team_id: str, kind: str, player_id: str | None, detail: str = "") -> None:
    conn.execute("INSERT INTO transactions (at, team_id, kind, player_id, detail) VALUES (?, ?, ?, ?, ?)",
                 (clock.now_iso(), team_id, kind, player_id, detail))


def _name(conn: sqlite3.Connection, pid: str) -> str:
    row = conn.execute("SELECT full_name, position, team FROM nfl_players WHERE player_id = ?", (pid,)).fetchone()
    return f"{row['full_name']} ({row['position']}, {row['team']})" if row else pid


def owner(conn: sqlite3.Connection, pid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT team_id, on_ir FROM rosters WHERE player_id = ?", (pid,)).fetchone()


def active_count(conn: sqlite3.Connection, team_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM rosters WHERE team_id = ? AND on_ir = 0", (team_id,)).fetchone()[0]


def faab(conn: sqlite3.Connection, team_id: str) -> int:
    return conn.execute("SELECT faab FROM league_teams WHERE team_id = ?", (team_id,)).fetchone()[0] or 0


def _clear_from_lineups(conn: sqlite3.Connection, team_id: str, pid: str) -> None:
    week = season.current_week(conn)
    conn.execute("UPDATE lineups SET player_id = NULL, set_by = 'auto' WHERE team_id = ? AND player_id = ? "
                 "AND week >= ?", (team_id, pid, week))


def _check_movable(conn: sqlite3.Connection, pid: str, what: str) -> None:
    if season.is_locked(conn, pid, season.current_week(conn)):
        raise LeagueError(f"{_name(conn, pid)}'s game this week has kicked off, so he is locked and cannot be "
                          f"{what} until the fantasy week ends (after Monday's games).")


# Drops and IR --------------------------------------------------------------------------------

def drop_player(conn: sqlite3.Connection, team_id: str, pid: str) -> dict:
    o = owner(conn, pid)
    if not o or o["team_id"] != team_id:
        raise LeagueError(f"{_name(conn, pid)} is not on your roster.")
    _check_movable(conn, pid, "dropped")
    with conn:
        _clear_from_lineups(conn, team_id, pid)
        conn.execute("DELETE FROM rosters WHERE player_id = ?", (pid,))
        _log(conn, team_id, "drop", pid)
    return {"ok": True, "dropped": _name(conn, pid),
            "note": "He is now a free agent; any team (including you) can bid on him at the next waiver run."}


def move_to_ir(conn: sqlite3.Connection, team_id: str, pid: str) -> dict:
    o = owner(conn, pid)
    if not o or o["team_id"] != team_id:
        raise LeagueError(f"{_name(conn, pid)} is not on your roster.")
    if o["on_ir"]:
        raise LeagueError(f"{_name(conn, pid)} is already on IR.")
    eligible = load_league()["roster"]["ir_eligible"]
    status = conn.execute("SELECT injury_status FROM nfl_players WHERE player_id = ?", (pid,)).fetchone()[0]
    if status not in eligible:
        raise LeagueError(f"{_name(conn, pid)} has injury status {status!r}; IR needs one of {eligible}.")
    slots = load_league()["roster"]["ir"]
    used = conn.execute("SELECT COUNT(*) FROM rosters WHERE team_id = ? AND on_ir = 1", (team_id,)).fetchone()[0]
    if used >= slots:
        raise LeagueError(f"Your IR is full ({slots} slot).")
    _check_movable(conn, pid, "moved")
    with conn:
        _clear_from_lineups(conn, team_id, pid)
        conn.execute("UPDATE rosters SET on_ir = 1 WHERE player_id = ?", (pid,))
        _log(conn, team_id, "ir", pid)
    return {"ok": True, "moved_to_ir": _name(conn, pid), "roster_spots_used": active_count(conn, team_id)}


def activate_from_ir(conn: sqlite3.Connection, team_id: str, pid: str) -> dict:
    o = owner(conn, pid)
    if not o or o["team_id"] != team_id or not o["on_ir"]:
        raise LeagueError(f"{_name(conn, pid)} is not on your IR.")
    if active_count(conn, team_id) >= roster_size():
        raise LeagueError(f"Your active roster is full ({roster_size()}). Drop someone first.")
    with conn:
        conn.execute("UPDATE rosters SET on_ir = 0 WHERE player_id = ?", (pid,))
        _log(conn, team_id, "activate", pid)
    return {"ok": True, "activated": _name(conn, pid)}


# Waivers -------------------------------------------------------------------------------------

def submit_claim(conn: sqlite3.Connection, team_id: str, add: str, drop: str | None, bid: int) -> dict:
    cfg = load_league()["waivers"]
    if not conn.execute("SELECT 1 FROM nfl_players WHERE player_id = ?", (add,)).fetchone():
        raise LeagueError(f"No player with player_id {add!r}.")
    if owner(conn, add):
        raise LeagueError(f"{_name(conn, add)} is on {owner(conn, add)['team_id']}'s roster. Try a trade.")
    if drop:
        o = owner(conn, drop)
        if not o or o["team_id"] != team_id:
            raise LeagueError(f"{_name(conn, drop)} is not on your roster.")
    bid = int(bid)
    if bid < cfg["min_bid"] or bid > faab(conn, team_id):
        raise LeagueError(f"Bid must be between {cfg['min_bid']} and your remaining budget ({faab(conn, team_id)}).")
    pending = conn.execute("SELECT COUNT(*) FROM waiver_claims WHERE team_id = ? AND status = 'pending'",
                           (team_id,)).fetchone()[0]
    if pending >= MAX_PENDING_CLAIMS:
        raise LeagueError(f"You already have {MAX_PENDING_CLAIMS} pending claims. Cancel some first.")
    with conn:
        conn.execute("UPDATE waiver_claims SET status = 'cancelled', note = 'replaced' WHERE team_id = ? "
                     "AND status = 'pending' AND add_player = ? AND COALESCE(drop_player, '') = COALESCE(?, '')",
                     (team_id, add, drop))
        priority = conn.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM waiver_claims WHERE team_id = ? "
                                "AND status = 'pending'", (team_id,)).fetchone()[0]
        cur = conn.execute("INSERT INTO waiver_claims (team_id, add_player, drop_player, bid, priority, created_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (team_id, add, drop, bid, priority, clock.now_iso()))
    warn = None
    if not drop and active_count(conn, team_id) >= roster_size():
        warn = "Your roster is full, so this claim will fail unless you drop someone by processing time."
    return {"ok": True, "claim_id": cur.lastrowid, "add": _name(conn, add), "drop": _name(conn, drop) if drop else None,
            "bid": bid, "priority": priority, "next_processing": next_processing(), "warning": warn}


def cancel_claim(conn: sqlite3.Connection, team_id: str, claim_id: int) -> dict:
    with conn:
        n = conn.execute("UPDATE waiver_claims SET status = 'cancelled' WHERE id = ? AND team_id = ? "
                         "AND status = 'pending'", (claim_id, team_id)).rowcount
    if not n:
        raise LeagueError(f"No pending claim {claim_id} of yours.")
    return {"ok": True, "cancelled": claim_id}


def claims(conn: sqlite3.Connection, team_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM waiver_claims WHERE team_id = ? ORDER BY (status = 'pending') DESC, id DESC "
                        "LIMIT ?", (team_id, limit)).fetchall()
    return [{"claim_id": r["id"], "status": r["status"], "add": _name(conn, r["add_player"]),
             "drop": _name(conn, r["drop_player"]) if r["drop_player"] else None, "bid": r["bid"],
             "priority": r["priority"], "note": r["note"], "processed_at": r["processed_at"]} for r in rows]


def next_processing() -> str:
    times = load_league()["waivers"]["process"]
    nxt = min(clock.next_occurrence(t) for t in times)
    return clock.eastern(nxt)


def process_waivers(conn: sqlite3.Connection) -> list[str]:
    """Blind FAAB: highest bid first. Ties go to the team lower in the standings, then the team's
    own priority, then the earlier claim."""
    rank = {s["team_id"]: s["rank"] for s in season.standings(conn)}
    pending = conn.execute("SELECT * FROM waiver_claims WHERE status = 'pending'").fetchall()
    order = sorted(pending, key=lambda c: (-c["bid"], -rank.get(c["team_id"], 0), c["priority"], c["id"]))
    ts, log = clock.now_iso(), []
    for c in order:
        status, note = "won", None
        try:
            o = owner(conn, c["add_player"])
            if o:
                won_by = conn.execute("SELECT team_id, bid FROM waiver_claims WHERE add_player = ? AND status = 'won' "
                                      "AND processed_at = ?", (c["add_player"], ts)).fetchone()
                raise LeagueError(f"lost to {won_by['team_id']} (${won_by['bid']})" if won_by
                                  else f"already on {o['team_id']}")
            if faab(conn, c["team_id"]) < c["bid"]:
                raise LeagueError("not enough FAAB left after earlier wins")
            if c["drop_player"]:
                d = owner(conn, c["drop_player"])
                if not d or d["team_id"] != c["team_id"]:
                    raise LeagueError("the player to drop is no longer on your roster")
                _check_movable(conn, c["drop_player"], "dropped")
            room = active_count(conn, c["team_id"]) - (1 if c["drop_player"] and not owner(conn, c["drop_player"])["on_ir"]
                                                       else 0)
            if room >= roster_size():
                raise LeagueError("roster full and no player to drop")
            with conn:
                if c["drop_player"]:
                    _clear_from_lineups(conn, c["team_id"], c["drop_player"])
                    conn.execute("DELETE FROM rosters WHERE player_id = ?", (c["drop_player"],))
                    _log(conn, c["team_id"], "drop", c["drop_player"], f"for waiver claim {c['id']}")
                conn.execute("INSERT INTO rosters (player_id, team_id, acquired_at, how) VALUES (?, ?, ?, 'waiver')",
                             (c["add_player"], c["team_id"], ts))
                conn.execute("UPDATE league_teams SET faab = faab - ? WHERE team_id = ?", (c["bid"], c["team_id"]))
                _log(conn, c["team_id"], "add", c["add_player"], f"waiver ${c['bid']} (claim {c['id']})")
            log.append(f"{c['team_id']} won {_name(conn, c['add_player'])} for ${c['bid']}")
        except LeagueError as e:
            status, note = ("lost" if "lost to" in str(e) or "already on" in str(e) else "invalid"), str(e)
        with conn:
            conn.execute("UPDATE waiver_claims SET status = ?, note = ?, processed_at = ? WHERE id = ?",
                         (status, note, ts, c["id"]))
    return log


# Trades --------------------------------------------------------------------------------------

def trade_window_open(conn: sqlite3.Connection) -> bool:
    deadline_week = load_league()["trades"]["deadline_week"]
    kickoff = season.first_kickoff(conn, deadline_week + 1)
    return not kickoff or clock.now() < clock.parse(kickoff)


def _trade_view(conn: sqlite3.Connection, t: sqlite3.Row) -> dict:
    return {"trade_id": t["id"], "from_team": t["from_team"], "to_team": t["to_team"],
            "from_team_gives": [_name(conn, p) for p in json.loads(t["give"])],
            "from_team_receives": [_name(conn, p) for p in json.loads(t["receive"])],
            "give_ids": json.loads(t["give"]), "receive_ids": json.loads(t["receive"]),
            "message": t["message"], "status": t["status"], "created_at": t["created_at"],
            "expires_at": t["expires_at"], "responded_at": t["responded_at"], "response": t["response"]}


def _validate_sides(conn, from_team, to_team, give, receive):
    for pid in give:
        o = owner(conn, pid)
        if not o or o["team_id"] != from_team:
            raise LeagueError(f"{_name(conn, pid)} is not on {from_team}'s roster.")
        if o["on_ir"]:
            raise LeagueError(f"{_name(conn, pid)} is on IR; activate him before trading him.")
    for pid in receive:
        o = owner(conn, pid)
        if not o or o["team_id"] != to_team:
            raise LeagueError(f"{_name(conn, pid)} is not on {to_team}'s roster.")
        if o["on_ir"]:
            raise LeagueError(f"{_name(conn, pid)} is on IR and cannot be traded.")


def propose_trade(conn: sqlite3.Connection, from_team: str, to_team: str, give: list[str], receive: list[str],
                  message: str = "") -> dict:
    cfg = load_league()["trades"]
    give, receive = [str(p) for p in give], [str(p) for p in receive]
    if not trade_window_open(conn):
        raise LeagueError("The trade deadline has passed.")
    if to_team == from_team or not conn.execute("SELECT 1 FROM league_teams WHERE team_id = ?", (to_team,)).fetchone():
        raise LeagueError(f"{to_team!r} is not another team in this league.")
    if not give or not receive:
        raise LeagueError("A trade needs at least one player going each way.")
    _validate_sides(conn, from_team, to_team, give, receive)
    open_count = conn.execute("SELECT COUNT(*) FROM trades WHERE from_team = ? AND status = 'open'",
                              (from_team,)).fetchone()[0]
    if open_count >= cfg["max_open_proposals"]:
        raise LeagueError(f"You already have {open_count} open proposals (max {cfg['max_open_proposals']}).")
    expires = (clock.now() + timedelta(hours=cfg["expires_hours"])).strftime(clock.ISO)
    with conn:
        cur = conn.execute("INSERT INTO trades (from_team, to_team, give, receive, message, created_at, expires_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (from_team, to_team, json.dumps(give), json.dumps(receive),
                                                           message[:MAX_MESSAGE_CHARS], clock.now_iso(), expires))
    t = conn.execute("SELECT * FROM trades WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"ok": True, "trade": _trade_view(conn, t),
            "note": f"{to_team} is woken to answer it, usually within the hour. It expires at {expires} "
                    f"unless answered."}


def respond_to_trade(conn: sqlite3.Connection, team_id: str, trade_id: int, accept: bool, message: str = "") -> dict:
    t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not t or t["to_team"] != team_id:
        raise LeagueError(f"No trade {trade_id} addressed to you.")
    if t["status"] != "open":
        raise LeagueError(f"Trade {trade_id} is {t['status']}, not open.")
    if clock.parse(t["expires_at"]) <= clock.now():
        expire_trades(conn)
        raise LeagueError(f"Trade {trade_id} has expired.")
    if not accept:
        with conn:
            conn.execute("UPDATE trades SET status = 'rejected', responded_at = ?, response = ? WHERE id = ?",
                         (clock.now_iso(), message[:MAX_MESSAGE_CHARS], trade_id))
        return {"ok": True, "rejected": trade_id}
    _check_trade_executable(conn, t)
    if load_league()["trades"].get("review") == "commissioner":
        with conn:
            conn.execute("UPDATE trades SET status = 'pending_review', responded_at = ?, response = ? WHERE id = ?",
                         (clock.now_iso(), message[:MAX_MESSAGE_CHARS], trade_id))
        hours = load_league()["trades"].get("auto_approve_hours", 24)
        return {"ok": True, "status": "pending_review",
                "note": f"Accepted. The commissioner reviews every trade before it executes (it goes through "
                        f"automatically after {hours} hours without a decision). Until then, rosters are unchanged."}
    return execute_trade(conn, trade_id)


def _check_trade_executable(conn: sqlite3.Connection, t: sqlite3.Row) -> None:
    if not trade_window_open(conn):
        raise LeagueError("The trade deadline has passed.")
    give, receive = json.loads(t["give"]), json.loads(t["receive"])
    _validate_sides(conn, t["from_team"], t["to_team"], give, receive)
    for pid in give + receive:
        _check_movable(conn, pid, "traded")
    limit = roster_size()
    after_from = active_count(conn, t["from_team"]) - len(give) + len(receive)
    after_to = active_count(conn, t["to_team"]) - len(receive) + len(give)
    if after_to > limit:
        raise LeagueError(f"Accepting would put {t['to_team']} at {after_to} players (max {limit}). Drop "
                          f"{after_to - limit} first, then accept.")
    if after_from > limit:
        raise LeagueError(f"{t['from_team']} would end up over the roster limit; they must drop a player first.")


def execute_trade(conn: sqlite3.Connection, trade_id: int) -> dict:
    """Move the players. Re-checks everything, since rosters may have changed since acceptance."""
    t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    _check_trade_executable(conn, t)
    give, receive = json.loads(t["give"]), json.loads(t["receive"])
    ts = clock.now_iso()
    with conn:
        for pid in give:
            _clear_from_lineups(conn, t["from_team"], pid)
            conn.execute("UPDATE rosters SET team_id = ?, acquired_at = ?, how = 'trade' WHERE player_id = ?",
                         (t["to_team"], ts, pid))
            _log(conn, t["to_team"], "trade", pid, f"from {t['from_team']} (trade {trade_id})")
        for pid in receive:
            _clear_from_lineups(conn, t["to_team"], pid)
            conn.execute("UPDATE rosters SET team_id = ?, acquired_at = ?, how = 'trade' WHERE player_id = ?",
                         (t["from_team"], ts, pid))
            _log(conn, t["from_team"], "trade", pid, f"from {t['to_team']} (trade {trade_id})")
        # response stays the other team's reply; a commissioner's note goes to both teams as a DM.
        conn.execute("UPDATE trades SET status = 'accepted', responded_at = COALESCE(responded_at, ?) WHERE id = ?",
                     (ts, trade_id))
        # Other open offers involving these players can no longer go through.
        moved = set(give + receive)
        for other in conn.execute("SELECT * FROM trades WHERE status IN ('open', 'pending_review') AND id != ?",
                                  (trade_id,)).fetchall():
            if moved & set(json.loads(other["give"]) + json.loads(other["receive"])):
                conn.execute("UPDATE trades SET status = 'failed', response = ? WHERE id = ?",
                             (f"a player in it was traded in trade {trade_id}", other["id"]))
    return {"ok": True, "accepted": _trade_view(conn, conn.execute("SELECT * FROM trades WHERE id = ?",
                                                                   (trade_id,)).fetchone())}


def cancel_trade(conn: sqlite3.Connection, team_id: str, trade_id: int) -> dict:
    with conn:
        n = conn.execute("UPDATE trades SET status = 'cancelled' WHERE id = ? AND from_team = ? AND status = 'open'",
                         (trade_id, team_id)).rowcount
    if not n:
        raise LeagueError(f"No open trade {trade_id} proposed by you.")
    return {"ok": True, "cancelled": trade_id}


def expire_trades(conn: sqlite3.Connection) -> int:
    with conn:
        return conn.execute("UPDATE trades SET status = 'expired' WHERE status = 'open' AND expires_at <= ?",
                            (clock.now_iso(),)).rowcount


def trades_for(conn: sqlite3.Connection, team_id: str, limit: int = 15) -> list[dict]:
    rows = conn.execute("SELECT * FROM trades WHERE from_team = ? OR to_team = ? ORDER BY (status = 'open') DESC, "
                        "id DESC LIMIT ?", (team_id, team_id, limit)).fetchall()
    return [_trade_view(conn, t) for t in rows]


# Commissioner (a person) ------------------------------------------------------------------

COMMISSIONER = "commissioner"


def pending_trades(conn: sqlite3.Connection) -> list[dict]:
    return [_trade_view(conn, t) for t in
            conn.execute("SELECT * FROM trades WHERE status = 'pending_review' ORDER BY id").fetchall()]


def _tell(conn: sqlite3.Connection, teams: list[str], text: str) -> None:
    with conn:
        for tid in teams:
            conn.execute("INSERT INTO messages (at, from_team, to_team, text) VALUES (?, ?, ?, ?)",
                         (clock.now_iso(), COMMISSIONER, tid, text))


def approve_trade(conn: sqlite3.Connection, trade_id: int, note: str = "approved by the commissioner") -> dict:
    t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not t or t["status"] != "pending_review":
        raise LeagueError(f"Trade {trade_id} is not awaiting review.")
    teams = [t["from_team"], t["to_team"]]
    try:
        result = execute_trade(conn, trade_id)
    except LeagueError as e:
        with conn:
            conn.execute("UPDATE trades SET status = 'failed', response = ? WHERE id = ?",
                         (f"could not execute at approval: {e}", trade_id))
        _tell(conn, teams, f"Trade {trade_id} could not go through when approved: {e}")
        raise
    _tell(conn, teams, f"Trade {trade_id} was approved and has executed" + (f" ({note})." if note else "."))
    return result


def veto_trade(conn: sqlite3.Connection, trade_id: int, reason: str) -> dict:
    t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not t or t["status"] != "pending_review":
        raise LeagueError(f"Trade {trade_id} is not awaiting review.")
    with conn:
        conn.execute("UPDATE trades SET status = 'vetoed', response = ? WHERE id = ?", (f"vetoed: {reason}", trade_id))
    _tell(conn, [t["from_team"], t["to_team"]], f"The commissioner vetoed trade {trade_id}: {reason}")
    return {"ok": True, "vetoed": trade_id}


def auto_approve_trades(conn: sqlite3.Connection) -> list[str]:
    hours = load_league()["trades"].get("auto_approve_hours", 24)
    cutoff = (clock.now() - timedelta(hours=hours)).strftime(clock.ISO)
    done = []
    for t in conn.execute("SELECT id FROM trades WHERE status = 'pending_review' AND responded_at <= ?",
                          (cutoff,)).fetchall():
        try:
            approve_trade(conn, t["id"], f"auto-approved after {hours}h without a commissioner decision")
            done.append(f"trade {t['id']} auto-approved")
        except LeagueError as e:
            done.append(f"trade {t['id']} failed at auto-approval: {e}")
    return done


def undo_transaction(conn: sqlite3.Connection, tx_id: int, reason: str) -> dict:
    """Reverse a waiver add (refunding the bid and restoring the player dropped for it, if still
    free) or a plain drop (re-adding the player, if still free)."""
    tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not tx:
        raise LeagueError(f"No transaction {tx_id}.")
    team, pid = tx["team_id"], tx["player_id"]
    ts = clock.now_iso()
    claim = None
    if tx["kind"] == "add":
        claim = conn.execute("SELECT * FROM waiver_claims WHERE team_id = ? AND add_player = ? AND status = 'won' "
                             "AND processed_at <= ? ORDER BY id DESC LIMIT 1", (team, pid, tx["at"])).fetchone()
    if claim:
        claim_id = claim["id"]
        o = owner(conn, pid)
        if not o or o["team_id"] != team:
            raise LeagueError(f"{_name(conn, pid)} is no longer on {team}.")
        _check_movable(conn, pid, "removed")
        with conn:
            _clear_from_lineups(conn, team, pid)
            conn.execute("DELETE FROM rosters WHERE player_id = ?", (pid,))
            conn.execute("UPDATE league_teams SET faab = faab + ? WHERE team_id = ?", (claim["bid"], team))
            conn.execute("UPDATE waiver_claims SET status = 'reversed', note = ? WHERE id = ?",
                         (f"reversed by commissioner: {reason}", claim_id))
            _log(conn, team, "commissioner_undo", pid, f"reversed add (tx {tx_id}), refunded ${claim['bid']}: {reason}")
            restored = None
            if claim["drop_player"] and not owner(conn, claim["drop_player"]):
                conn.execute("INSERT INTO rosters (player_id, team_id, acquired_at, how) VALUES (?, ?, ?, 'restored')",
                             (claim["drop_player"], team, ts))
                restored = _name(conn, claim["drop_player"])
        _tell(conn, [team], f"The commissioner reversed your waiver pickup of {_name(conn, pid)} and refunded "
                            f"${claim['bid']}" + (f"; {restored} is back on your roster" if restored else "")
                            + f". Reason: {reason}")
        return {"ok": True, "reversed": _name(conn, pid), "refunded": claim["bid"], "restored": restored}
    if tx["kind"] == "drop":
        if owner(conn, pid):
            raise LeagueError(f"{_name(conn, pid)} has since been picked up by {owner(conn, pid)['team_id']}.")
        if active_count(conn, team) >= roster_size():
            raise LeagueError(f"{team}'s roster is full, so {_name(conn, pid)} can't be restored.")
        with conn:
            conn.execute("INSERT INTO rosters (player_id, team_id, acquired_at, how) VALUES (?, ?, ?, 'restored')",
                         (pid, team, ts))
            _log(conn, team, "commissioner_undo", pid, f"reversed drop (tx {tx_id}): {reason}")
        _tell(conn, [team], f"The commissioner reversed your drop of {_name(conn, pid)}; he is back on your "
                            f"roster. Reason: {reason}")
        return {"ok": True, "restored": _name(conn, pid)}
    if tx["kind"] == "add":
        raise LeagueError(f"Transaction {tx_id} has no matching won waiver claim; it may already be reversed.")
    raise LeagueError(f"Transaction {tx_id} is a {tx['kind']!r}; only waiver adds and drops can be reversed "
                      f"(veto trades before they execute instead).")


# Messages and the transaction log --------------------------------------------------------

def send_message(conn: sqlite3.Connection, from_team: str, to_team: str | None, text: str) -> dict:
    if to_team and not conn.execute("SELECT 1 FROM league_teams WHERE team_id = ?", (to_team,)).fetchone():
        raise LeagueError(f"{to_team!r} is not a team in this league. Use null to message everyone.")
    if not text.strip():
        raise LeagueError("Message is empty.")
    with conn:
        conn.execute("INSERT INTO messages (at, from_team, to_team, text) VALUES (?, ?, ?, ?)",
                     (clock.now_iso(), from_team, to_team, text[:MAX_MESSAGE_CHARS]))
    return {"ok": True, "to": to_team or "everyone"}


def messages_for(conn: sqlite3.Connection, team_id: str, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT * FROM messages WHERE to_team IS NULL OR to_team = ? OR from_team = ? "
                        "ORDER BY id DESC LIMIT ?", (team_id, team_id, limit)).fetchall()
    return [{"at": r["at"], "from": r["from_team"], "to": r["to_team"] or "everyone", "text": r["text"]}
            for r in reversed(rows)]


def recent_transactions(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    rows = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [f"{r['at']} {r['team_id']} {r['kind']} {_name(conn, r['player_id']) if r['player_id'] else ''} "
            f"{r['detail'] or ''}".strip() for r in rows]
