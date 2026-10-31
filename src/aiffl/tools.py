"""The league tools every agent gets, as an MCP server bound to one team.

Claude Code and Codex launch this over stdio (python -m aiffl.mcp_server --team team_01).
The OpenRouter runner builds the same server in-process and calls it directly, so all
harnesses see identical tool names, descriptions, and schemas.

One server instance lives for one agent turn, so the web budgets below are per turn.
Draft tools exist only until the draft is done; season tools only after.
"""

import functools
import inspect
import json
import os
import re
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

from aiffl import clock, db, league, players, season, transactions, web
from aiffl.config import load_league, team_by_id

NOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
MAX_NOTE_CHARS = 200_000


def build_server(team_id: str, budgets: dict[str, int] | None = None) -> MCPServer:
    team = team_by_id(team_id)
    team.notes_dir.mkdir(parents=True, exist_ok=True)
    budget = {"web_search": int(os.environ.get("AIFFL_SEARCH_BUDGET", 25)),
              "fetch_page": int(os.environ.get("AIFFL_FETCH_BUDGET", 15))}
    budget.update(budgets or {})
    turn = {"searches": 0, "pick_nudged": False}  # this server lives for one agent turn
    year = int(load_league()["season"])
    # WARNING keeps the MCP library from switching on per-request HTTP logging in the draft console.
    server = MCPServer(name="league", instructions=f"Fantasy league tools for team {team_id}.", log_level="WARNING")

    def tool(fn):
        """Register fn as a tool; log every call and turn exceptions into error results."""
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            conn = db.connect()
            try:
                result = fn(conn, *args, **kwargs)
                ok = not (isinstance(result, dict) and "error" in result)
            except league.LeagueError as e:
                result, ok = {"error": str(e)}, False
            except Exception as e:  # surface to the agent rather than crash its turn
                result, ok = {"error": f"{type(e).__name__}: {e}"}, False
            league.log_tool_call(conn, team_id, fn.__name__, json.dumps(kwargs, default=str), ok,
                                 json.dumps(result, default=str)[:500])
            conn.close()
            return result
        # MCP builds the schema from the signature; hide the injected connection.
        sig = inspect.signature(fn)
        wrapper.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])
        del wrapper.__wrapped__
        server.tool()(wrapper)
        return fn

    # Time and rules ------------------------------------------------------------------------

    @tool
    def get_current_time(conn) -> dict:
        """The current date and time (UTC and US Eastern). Use it to judge how fresh information is."""
        return {"utc": clock.now_iso(), "eastern": clock.eastern()}

    @tool
    def get_league_info(conn) -> dict:
        """League rules (roster slots, scoring, waivers, trades, schedule), the teams, and the draft order."""
        return {"your_team": team_id, "rules": load_league(), "teams": league.draft_order(conn),
                "scoring_notes": "Kicker: FG 0-39 = 3, 40-49 = 4, 50+ = 5, XP = 1, missed FG or XP = -1. "
                                 "DST: sack 1, INT 2, fumble recovery 2, safety 2, blocked kick 2, "
                                 "defensive or return TD 6; points allowed 0 = 10, 1-6 = 7, 7-13 = 4, "
                                 "14-20 = 1, 21-27 = 0, 28-34 = -1, 35+ = -4."}

    # Players -------------------------------------------------------------------------------

    @tool
    def search_players(conn, position: str | None = None, name: str | None = None, nfl_team: str | None = None,
                       available_only: bool = True, limit: int = 40) -> dict:
        """Find players, sorted by rest-of-season projected points (league scoring).

        position: QB, RB, WR, TE, K, or DEF. name: partial name match. nfl_team: e.g. "DET".
        available_only: true shows only players on no fantasy roster (free agents).
        limit: up to 100. Projections are a baseline from Sleeper, not ground truth; verify with
        current news.
        """
        rows = players.search(conn, position=position, name=name, nfl_team=nfl_team,
                              available_only=available_only, limit=limit)
        return {"count": len(rows), "players": rows}

    @tool
    def get_player(conn, player_id: str) -> dict:
        """Full detail for one player: status, injury, depth chart, this season's weekly log, and
        weekly projections for the rest of the season."""
        p = players.get_player(conn, player_id)
        if not p:
            return {"error": f"No player with player_id {player_id!r}."}
        start = load_league()["schedule"]["start_week"]
        p["weekly_log"] = players.weekly_log(conn, player_id, year)
        p["projections_by_week"] = players.weekly_projections(conn, player_id, year, start)
        return p

    with db.connect() as c:
        state = league.draft_state(c)
    if not state or state["status"] != "done":
        _draft_tools(tool, team_id, turn)
    else:
        _season_tools(tool, team_id)

    # Notes ---------------------------------------------------------------------------------

    def note_path(name: str):
        if not NOTE_NAME.match(name) or ".." in name:
            raise league.LeagueError("Note names may use letters, digits, '_', '-', '.', e.g. 'summary.md'.")
        return team.notes_dir / name

    @tool
    def list_notes(conn) -> dict:
        """List your private note files (name, size, last modified UTC)."""
        files = sorted(team.notes_dir.glob("*"))
        return {"notes": [{"name": f.name, "chars": f.stat().st_size,
                           "modified": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%SZ")} for f in files if f.is_file()]}

    @tool
    def read_note(conn, name: str) -> dict:
        """Read one of your note files."""
        path = note_path(name)
        if not path.exists():
            return {"error": f"No note named {name!r}. Use list_notes."}
        return {"name": name, "content": path.read_text(encoding="utf-8")}

    @tool
    def write_note(conn, name: str, content: str) -> dict:
        """Create or overwrite one of your note files. Only you can read your notes."""
        if len(content) > MAX_NOTE_CHARS:
            return {"error": f"Note too long ({len(content)} chars, max {MAX_NOTE_CHARS})."}
        note_path(name).write_text(content, encoding="utf-8")
        return {"ok": True, "name": name, "chars": len(content)}

    # Web -----------------------------------------------------------------------------------

    def spend(kind: str) -> None:
        if budget[kind] <= 0:
            raise league.LeagueError(f"{kind} budget for this turn is used up. Decide with what you have.")
        budget[kind] -= 1

    @tool
    def web_search(conn, query: str, num_results: int = 6, published_after: str | None = None) -> dict:
        """Search the web. Returns title, URL, publication date, and an excerpt for each result.

        published_after: ISO date like "2026-10-28" to keep only recent pages. Use it for news,
        injuries, and depth charts, which go stale fast.
        """
        spend("web_search")
        turn["searches"] += 1
        return {"results": web.search(query, num_results, published_after), "searches_left": budget["web_search"]}

    @tool
    def fetch_page(conn, url: str, max_chars: int = 10000) -> dict:
        """Read the text of a web page (up to max_chars, max 20000)."""
        spend("fetch_page")
        return {**web.fetch(url, max_chars), "fetches_left": budget["fetch_page"]}

    return server


def _draft_tools(tool, team_id: str, turn: dict) -> None:
    @tool
    def get_draft_status(conn) -> dict:
        """Where the draft stands: who is on the clock, every pick so far, your roster and remaining
        needs, your upcoming pick numbers, and the available players at the top of your queue."""
        state = league.draft_state(conn)
        if not state:
            return {"error": "No draft has been set up."}
        pick = league.current_pick(conn)
        mine = league.team_pick_numbers(conn, team_id)
        counts = league.position_counts(conn, team_id)
        return {
            "status": state["status"],
            "current_pick": dict(pick) if pick else None,
            "you_are_on_the_clock": bool(pick and pick["team_id"] == team_id and state["status"] == "live"),
            "your_remaining_pick_numbers": mine,
            "picks_until_your_next_turn": (mine[0] - pick["pick_no"]) if (mine and pick) else None,
            "your_roster": league.roster(conn, team_id),
            "your_needs": league.describe_needs(counts),
            "picks_made": [f"{p['pick_no']}. {p['team_id']}: {p['full_name']} ({p['position']}, {p['team']})"
                           for p in league.picks_made(conn)],
            "your_queue_top": league.queue(conn, team_id)[:15],
        }

    @tool
    def draft_player(conn, player_id: str) -> dict:
        """Draft a player. Only works while you are on the clock. The pick is final."""
        if turn["searches"] == 0 and not turn["pick_nudged"]:
            # Soft verification nudge, identical for every team: once per turn, a pick made without
            # any web research this turn is held back. Calling again drafts the player.
            turn["pick_nudged"] = True
            return {"error": "Not drafted yet: you have not run any web_search this turn. Verify the player's "
                             "current injury status, role, and news with a dated source first. If you are "
                             "confident without checking, call draft_player again with the same player_id."}
        return {"ok": True, "pick": league.make_pick(conn, team_id, player_id)}

    @tool
    def set_draft_queue(conn, player_ids: list[str]) -> dict:
        """Replace your ranked draft queue (best first). If you ever run out of time on the clock,
        the first valid available player in this queue is drafted for you, so keep it current."""
        return league.set_queue(conn, team_id, player_ids)


def _season_tools(tool, team_id: str) -> None:
    T = transactions

    @tool
    def get_briefing(conn) -> dict:
        """Start here. Everything you need in one call: time, week, deadlines, your lineup and bench
        (with lock state, injury status, projections), your matchup, standings, FAAB, pending claims,
        open trades, recent messages, and recent league transactions."""
        week = season.current_week(conn)
        lineup = season.lineup_detail(conn, team_id, week)
        unlocked = [s for s in lineup["starters"].values() if s and not s["locked"]]
        return {
            "now": clock.eastern(), "week": week, "your_team": team_id,
            "deadlines": {
                "next_waiver_processing": T.next_processing(),
                "trade_deadline": f"before week {load_league()['trades']['deadline_week'] + 1} kicks off",
                "trades_open": T.trade_window_open(conn),
                "lineup_locks": "each player locks at his own game's kickoff (see 'game' on each player)",
                "unlocked_starters": len(unlocked),
            },
            "lineup": lineup,
            "matchup": season.matchup_for(conn, team_id, week),
            "standings": season.standings(conn),
            "faab_remaining": T.faab(conn, team_id),
            "pending_claims": [c for c in T.claims(conn, team_id) if c["status"] == "pending"],
            "recent_claim_results": [c for c in T.claims(conn, team_id) if c["status"] != "pending"][:8],
            "trades": T.trades_for(conn, team_id),
            "messages": T.messages_for(conn, team_id, limit=15),
            "league_transactions": T.recent_transactions(conn, limit=20),
        }

    @tool
    def get_lineup(conn, week: int | None = None) -> dict:
        """Your starters, bench, and IR for a week (default: the current week), with each player's
        game, lock state, injury status, projection, and points so far."""
        return season.lineup_detail(conn, team_id, week or season.current_week(conn))

    @tool
    def set_lineup(conn, changes: dict[str, str | None], week: int | None = None) -> dict:
        """Change starters. `changes` maps slot -> player_id (null empties a slot), e.g.
        {"RB2": "9221", "FLEX": "4034"}. Slots: QB, RB1, RB2, WR1, WR2, TE, FLEX (RB/WR/TE), K, DEF.
        Unlisted slots stay as they are. A player is locked once his game kicks off. Players not
        in a slot are on your bench. week defaults to the current week."""
        return season.set_lineup(conn, team_id, week or season.current_week(conn), changes)

    @tool
    def get_team(conn, team_id_to_view: str) -> dict:
        """Another team's roster, record, and current lineup. Rosters are public."""
        if not conn.execute("SELECT 1 FROM league_teams WHERE team_id = ?", (team_id_to_view,)).fetchone():
            return {"error": f"No team {team_id_to_view!r}."}
        week = season.current_week(conn)
        record = next((s for s in season.standings(conn) if s["team_id"] == team_id_to_view), None)
        return {"team_id": team_id_to_view, "record": record, "roster": league.roster(conn, team_id_to_view),
                "lineup": season.lineup_detail(conn, team_id_to_view, week)["starters"],
                "faab_remaining": T.faab(conn, team_id_to_view)}

    @tool
    def get_scoreboard(conn, week: int | None = None) -> dict:
        """All matchups and scores for a week (default: current week), plus standings."""
        week = week or season.current_week(conn)
        rows = conn.execute("SELECT * FROM matchups WHERE week = ?", (week,)).fetchall()
        return {"week": week, "matchups": [dict(r) for r in rows], "standings": season.standings(conn)}

    @tool
    def submit_waiver_claim(conn, add_player_id: str, bid: int, drop_player_id: str | None = None) -> dict:
        """Bid FAAB on a free agent. Bids are blind and processed at the scheduled waiver times
        (see get_briefing): highest bid wins, ties go to the team lower in the standings. Claims are
        processed highest bid first; if two of your claims drop the same player, only the first to
        win goes through. Include drop_player_id if your roster is full."""
        return T.submit_claim(conn, team_id, add_player_id, drop_player_id, bid)

    @tool
    def cancel_waiver_claim(conn, claim_id: int) -> dict:
        """Cancel one of your pending waiver claims."""
        return T.cancel_claim(conn, team_id, claim_id)

    @tool
    def get_my_claims(conn) -> dict:
        """Your pending waiver claims and recent results."""
        return {"claims": T.claims(conn, team_id), "faab_remaining": T.faab(conn, team_id),
                "next_processing": T.next_processing()}

    @tool
    def drop_player(conn, player_id: str) -> dict:
        """Release a player immediately. He becomes a free agent anyone can bid on."""
        return T.drop_player(conn, team_id, player_id)

    @tool
    def move_to_ir(conn, player_id: str) -> dict:
        """Move an injured player (status IR, Out, PUP, Sus, NA, COV, DNR) to your IR slot, freeing a roster spot."""
        return T.move_to_ir(conn, team_id, player_id)

    @tool
    def activate_from_ir(conn, player_id: str) -> dict:
        """Move a player from IR back to your active roster (needs a free roster spot)."""
        return T.activate_from_ir(conn, team_id, player_id)

    @tool
    def propose_trade(conn, to_team: str, give_player_ids: list[str], receive_player_ids: list[str],
                      message: str = "") -> dict:
        """Offer a trade. It executes as soon as the other team accepts, if both rosters are still
        valid. Offers expire after 48 hours. Your message is shown to the other team."""
        return T.propose_trade(conn, team_id, to_team, give_player_ids, receive_player_ids, message)

    @tool
    def respond_to_trade(conn, trade_id: int, accept: bool, message: str = "") -> dict:
        """Accept or reject a trade offered to you. Accepting executes it immediately."""
        return T.respond_to_trade(conn, team_id, trade_id, accept, message)

    @tool
    def cancel_trade(conn, trade_id: int) -> dict:
        """Withdraw an open trade offer you made."""
        return T.cancel_trade(conn, team_id, trade_id)

    @tool
    def get_trades(conn) -> dict:
        """Trade offers you sent or received, open ones first."""
        T.expire_trades(conn)
        return {"trades": T.trades_for(conn, team_id), "trades_open": T.trade_window_open(conn)}

    @tool
    def send_message(conn, text: str, to_team: str | None = None) -> dict:
        """Message another team (to_team) or the whole league (to_team null). Everyone sees league-wide
        messages; direct messages are seen only by the recipient."""
        return T.send_message(conn, team_id, to_team, text)

    @tool
    def get_messages(conn) -> dict:
        """Recent league-wide messages and direct messages to or from you."""
        return {"messages": T.messages_for(conn, team_id)}

    @tool
    def get_transactions(conn) -> dict:
        """The league's recent adds, drops, trades, and IR moves."""
        return {"transactions": T.recent_transactions(conn, limit=40)}
