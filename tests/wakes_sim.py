"""Event-wake rules on a copy of the league database (no agents are run).

    python tests/wakes_sim.py <copy-of-league.db>
"""

import os
import sys

os.environ["AIFFL_DB"] = sys.argv[1]
os.environ["AIFFL_NOW"] = "2026-11-01T03:00:00Z"

from aiffl import agents, db, transactions as T  # noqa: E402
from aiffl.config import load_league  # noqa: E402

cfg = load_league()
cfg["automation"]["event_wakes"]["enabled"] = True
agents.load_league = lambda: cfg  # test with wakes on, without touching league.yaml

conn = db.connect()
conn.execute("UPDATE agent_runs SET seconds = COALESCE(seconds, 60), started_at = '2026-11-01T01:00:00Z'")
conn.commit()

T.send_message(conn, "team_01", "team_02", "Interested in your WR depth?")          # direct message
T.send_message(conn, "team_05", None, "team_06 — want to talk RB for TE?")           # board post naming a team
T.send_message(conn, "team_08", None, "Good luck all")                               # names nobody: no wake
offer = conn.execute("SELECT r1.player_id a, r2.player_id b FROM rosters r1, rosters r2 "
                     "WHERE r1.team_id = 'team_03' AND r2.team_id = 'team_07' LIMIT 1").fetchone()
T.propose_trade(conn, "team_03", "team_07", [offer["a"]], [offer["b"]], "test offer")  # trade offer

wakes = agents.due_wakes(conn)
print("wakes:", {k: len(v) for k, v in wakes.items()})
assert set(wakes) == {"team_02", "team_06", "team_07"}, wakes

# Cooldown: team_02 just ran.
conn.execute("INSERT INTO agent_runs (team_id, why, started_at, seconds, ok) VALUES "
             "('team_02', 'x', '2026-11-01T02:30:00Z', 60, 1)")
# In progress: team_06 has a run with no end.
conn.execute("INSERT INTO agent_runs (team_id, why, started_at) VALUES ('team_06', 'x', '2026-11-01T02:55:00Z')")
# Daily cap: team_07 already woken 6 times today (Oct 31 ET), all before its offer arrived.
for i in range(6):
    conn.execute("INSERT INTO agent_runs (team_id, why, started_at, seconds, ok) VALUES "
                 "('team_07', 'Woken for: x', ?, 60, 1)", (f"2026-11-01T00:0{i}:00Z",))  # 8:00-8:05pm ET
conn.commit()
T.send_message(conn, "team_01", "team_07", "ping")  # new item after those runs
wakes = agents.due_wakes(conn)
print("after guards:", {k: len(v) for k, v in wakes.items()})
assert "team_02" not in wakes, "cooldown"
assert "team_06" not in wakes, "in progress"
assert "team_07" not in wakes, "daily cap"
assert agents.pending_items(conn, "team_07"), "team_07 must still have an item, so only the cap blocks it"

# Waiver results wake the bidders, and only them.
fa = conn.execute("SELECT p.player_id FROM nfl_players p LEFT JOIN rosters r USING (player_id) "
                  "WHERE r.team_id IS NULL AND p.team IS NOT NULL AND p.position = 'WR' LIMIT 1").fetchone()[0]
drop4 = conn.execute("SELECT player_id FROM rosters WHERE team_id = 'team_04' AND on_ir = 0 LIMIT 1").fetchone()[0]
drop5 = conn.execute("SELECT player_id FROM rosters WHERE team_id = 'team_05' AND on_ir = 0 LIMIT 1").fetchone()[0]
conn.execute("UPDATE waiver_claims SET status = 'cancelled' WHERE status = 'pending'")
conn.commit()
T.submit_claim(conn, "team_04", fa, drop4, 9)
T.submit_claim(conn, "team_05", fa, drop5, 3)
os.environ["AIFFL_NOW"] = "2026-11-01T14:00:00Z"
print("waivers:", T.process_waivers(conn))
items4, items5 = agents.pending_items(conn, "team_04"), agents.pending_items(conn, "team_05")
print("team_04:", items4)
print("team_05:", items5)
assert any(i.startswith("waiver results: won") for i in items4)
assert any("outbid on" in i for i in items5)
assert not any(i.startswith("waiver results") for i in agents.pending_items(conn, "team_08"))
assert {"team_04", "team_05"} <= set(agents.due_wakes(conn))
print("WAKE CHECKS PASSED")
