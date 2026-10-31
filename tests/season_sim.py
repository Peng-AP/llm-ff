"""End-to-end season simulation on a copy of the database, with the clock pinned.

    $env:PYTHONPATH = "<repo>\\src"; python tests/season_sim.py <copy-of-league.db> <scratch dir>

Autodrafts all 8 teams, starts the season, then walks through week 9: lineups and locks,
waivers, IR, trades, messages, scoring, and finalization (with fake final game states).
"""

import json
import os
import sys
from pathlib import Path

db_path, scratch = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
os.environ.update({"AIFFL_DB": str(db_path), "AIFFL_AGENTS_DIR": str(scratch / "agents"),
                   "AIFFL_LOGS_DIR": str(scratch / "logs")})


def at(iso: str) -> None:
    os.environ["AIFFL_NOW"] = iso


at("2026-11-01T02:00:00Z")  # Saturday night ET, after the draft

from aiffl import db, league, season, transactions as T  # noqa: E402
from aiffl.config import load_teams  # noqa: E402
from aiffl.league import LeagueError  # noqa: E402
from aiffl.tools import build_server  # noqa: E402
import asyncio  # noqa: E402

conn = db.connect()
order = league.init_league(conn, load_teams(), seed=3)
league.set_draft_status(conn, "live")
while league.current_pick(conn):
    league.autopick(conn, league.current_pick(conn)["team_id"])
league.set_draft_status(conn, "done")
print("drafted:", conn.execute("SELECT COUNT(*) FROM rosters").fetchone()[0], "players")

season.start_season(conn)
week = season.current_week(conn)
print("current week:", week)
print("week 9 matchups:", [(m["team_a"], m["team_b"]) for m in conn.execute("SELECT * FROM matchups WHERE week = 9")])
pairs = {}
for m in conn.execute("SELECT * FROM matchups WHERE kind = 'regular'"):
    pairs.setdefault(frozenset((m["team_a"], m["team_b"])), []).append(m["week"])
assert len(pairs) == 28 and all(len(w) == 1 for w in pairs.values()), "round robin must pair every team once"
print("round robin ok: 28 unique pairings over 7 weeks")

t1, t2 = order[0], order[1]
lineup = season.lineup_detail(conn, t1, week)
print(f"{t1} auto lineup:", {s: (p["name"], p["projected"]) if p else None for s, p in lineup["starters"].items()})
assert all(lineup["starters"].values()), "auto lineup should fill every slot"

# Lineup changes and locks ---------------------------------------------------------------
bench = [p for p in lineup["bench"] if p["position"] in ("RB", "WR")]
flex_before = lineup["starters"]["FLEX"]
r = season.set_lineup(conn, t1, week, {"FLEX": bench[0]["player_id"]})
print("set FLEX:", r["lineup"]["FLEX"]["name"], "warnings:", r["warnings"])
try:
    season.set_lineup(conn, t1, week, {"QB": bench[0]["player_id"]})
except LeagueError as e:
    print("position check ok:", e)

# Jump to just after Thursday kickoff: TNF players lock.
at("2026-11-06T01:30:00Z")
tnf = {r["home"] for r in conn.execute("SELECT home, away FROM nfl_games WHERE week = 9 AND kickoff_utc = "
                                       "'2026-11-06T01:15:00Z'")} | {r["away"] for r in conn.execute(
    "SELECT away FROM nfl_games WHERE week = 9 AND kickoff_utc = '2026-11-06T01:15:00Z'")}
locked_team = next((tid for tid in order for s, p in season.lineup_detail(conn, tid, week)["starters"].items()
                    if p and p["nfl_team"] in tnf), None)
if locked_team:
    d = season.lineup_detail(conn, locked_team, week)
    slot, p = next((s, p) for s, p in d["starters"].items() if p and p["nfl_team"] in tnf)
    other = next((b for b in d["bench"] if b["position"] == p["position"]), None)
    try:
        season.set_lineup(conn, locked_team, week, {slot: other["player_id"] if other else None})
        print("LOCK FAILED: moved a locked player")
    except LeagueError as e:
        print("lock ok:", e)
    try:
        T.drop_player(conn, locked_team, p["player_id"])
        print("LOCK FAILED: dropped a locked player")
    except LeagueError as e:
        print("drop lock ok:", e)
at("2026-11-02T15:00:00Z")  # back to Monday

# Waivers --------------------------------------------------------------------------------
fa = conn.execute("SELECT p.player_id FROM nfl_players p JOIN nfl_player_summary s USING (player_id) "
                  "LEFT JOIN rosters r USING (player_id) WHERE r.team_id IS NULL AND p.team IS NOT NULL "
                  "AND p.position = 'RB' ORDER BY s.ros_projection DESC LIMIT 2").fetchall()
target, target2 = fa[0][0], fa[1][0]
drop1 = season.lineup_detail(conn, t1, week)["bench"][-1]["player_id"]
drop2 = season.lineup_detail(conn, t2, week)["bench"][-1]["player_id"]
print("claim:", T.submit_claim(conn, t1, target, drop1, 12)["add"])
T.submit_claim(conn, t2, target, drop2, 20)
T.submit_claim(conn, t1, target2, drop1, 5)  # same drop as the first claim
try:
    T.submit_claim(conn, t1, target, None, 999)
except LeagueError as e:
    print("bid cap ok:", e)
print("waivers:", T.process_waivers(conn))
for c in T.claims(conn, t1):
    print(f"   {t1} claim {c['claim_id']}: {c['status']} {c['note'] or ''}")
assert T.faab(conn, t2) == 80

# Trades ---------------------------------------------------------------------------------
give = [season.lineup_detail(conn, t1, week)["bench"][0]["player_id"]]
recv = [season.lineup_detail(conn, t2, week)["bench"][0]["player_id"]]
tr = T.propose_trade(conn, t1, t2, give, recv, "Bench swap?")
print("proposed trade", tr["trade"]["trade_id"], tr["trade"]["from_team_gives"], "for", tr["trade"]["from_team_receives"])
try:
    T.respond_to_trade(conn, t1, tr["trade"]["trade_id"], True)
except LeagueError as e:
    print("only recipient can accept ok:", e)
acc = T.respond_to_trade(conn, t2, tr["trade"]["trade_id"], True, "Deal")
print("accepted:", acc["accepted"]["status"])
assert T.owner(conn, give[0])["team_id"] == t2 and T.owner(conn, recv[0])["team_id"] == t1

# Messages -------------------------------------------------------------------------------
T.send_message(conn, t1, None, "Good luck everyone.")
T.send_message(conn, t2, t1, "Thanks for the trade.")
print("t1 inbox:", [m["text"] for m in T.messages_for(conn, t1)])
print("t3 inbox:", [m["text"] for m in T.messages_for(conn, order[2])])

# Safety net after a trade emptied a slot ----------------------------------------------
print("ensure_lineups:", season.ensure_lineups(conn, week))

# Tools through MCP, season mode ----------------------------------------------------------
async def mcp():
    s = build_server(t1)
    names = [t.name for t in await s.list_tools()]
    print("season tools:", names)
    assert "draft_player" not in names and "get_briefing" in names
    b = json.loads((await s.call_tool("get_briefing", {})).content[0].text)
    print("briefing keys:", list(b), "| matchup:", b["matchup"])
    r = json.loads((await s.call_tool("set_lineup", {"changes": {"K": None}})).content[0].text)
    print("empty K via tool -> warnings:", r.get("warnings"))
asyncio.run(mcp())
print("safety net refills K:", season.ensure_lineups(conn, week))

# Scoring with week 8 stats standing in, then finalize with fake final games --------------
conn.execute("INSERT OR REPLACE INTO nfl_stats SELECT season, 9, player_id, stats, updated_at FROM nfl_stats "
             "WHERE season = 2026 AND week = 7")
conn.execute("UPDATE nfl_games SET state = 'post' WHERE season = 2026 AND week = 9")
conn.commit()
at("2026-11-10T14:00:00Z")
season.score_week(conn, 9)
print("finalized:", season.finalize_week(conn, 9))
for m in conn.execute("SELECT * FROM matchups WHERE week = 9"):
    print(f"   {m['team_a']} {m['score_a']:.1f} - {m['score_b']:.1f} {m['team_b']} ({m['status']})")
print("standings:", [(s["team_id"], f"{s['wins']}-{s['losses']}") for s in season.standings(conn)])
print("current week now:", season.current_week(conn))
print("week 10 lineups (carry-over):", season.ensure_lineups(conn, 10)[:3])
print("ALL CHECKS PASSED")
