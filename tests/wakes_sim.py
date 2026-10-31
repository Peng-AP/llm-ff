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
print("WAKE CHECKS PASSED")
