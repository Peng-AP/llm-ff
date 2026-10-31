"""The league tools every agent gets, as an MCP server bound to one team.

Claude Code and Codex launch this over stdio (python -m aiffl.mcp_server --team team_01).
The OpenRouter runner builds the same server in-process and calls it directly, so all
harnesses see identical tool names, descriptions, and schemas.

One server instance lives for one agent turn, so the web budgets below are per turn.
"""

import functools
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer

from aiffl import db, league, players, web
from aiffl.config import load_league, team_by_id

NOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
MAX_NOTE_CHARS = 200_000
EASTERN = ZoneInfo("America/New_York")


def build_server(team_id: str, budgets: dict[str, int] | None = None) -> MCPServer:
    team = team_by_id(team_id)
    team.notes_dir.mkdir(parents=True, exist_ok=True)
    budget = {"web_search": int(os.environ.get("AIFFL_SEARCH_BUDGET", 25)),
              "fetch_page": int(os.environ.get("AIFFL_FETCH_BUDGET", 15))}
    budget.update(budgets or {})
    turn = {"searches": 0, "pick_nudged": False}  # this server lives for one agent turn
    season = int(load_league()["season"])
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
        import inspect
        sig = inspect.signature(fn)
        wrapper.__signature__ = sig.replace(parameters=list(sig.parameters.values())[1:])
        del wrapper.__wrapped__
        server.tool()(wrapper)
        return fn

    # Time and rules ------------------------------------------------------------------------

    @tool
    def get_current_time(conn) -> dict:
        """The current date and time (UTC and US Eastern). Use it to judge how fresh information is."""
        now = datetime.now(timezone.utc)
        return {"utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "eastern": now.astimezone(EASTERN).strftime("%A %Y-%m-%d %H:%M %Z")}

    @tool
    def get_league_info(conn) -> dict:
        """League rules (roster slots, scoring, waivers, trades, schedule), the teams, and the draft order."""
        cfg = load_league()
        return {"your_team": team_id, "rules": cfg, "draft_order": league.draft_order(conn),
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
        available_only: true hides players already on a fantasy roster. limit: up to 100.
        Projections are a baseline from Sleeper, not ground truth; verify with current news.
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
        p["weekly_log"] = players.weekly_log(conn, player_id, season)
        p["projections_by_week"] = players.weekly_projections(conn, player_id, season, start)
        return p

    # Draft ---------------------------------------------------------------------------------

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
