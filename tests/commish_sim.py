"""Commissioner flow on a simulated league (run after tests/season_sim.py on the same database).

    $env:PYTHONPATH = "<repo>\\src"; python tests/commish_sim.py <league.db> <scratch dir>
"""

import os
import subprocess
import sys
from pathlib import Path

db_path, scratch = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
os.environ.update({"AIFFL_DB": str(db_path), "AIFFL_AGENTS_DIR": str(scratch / "agents"),
                   "AIFFL_LOGS_DIR": str(scratch / "logs"), "AIFFL_NOW": "2026-11-10T15:00:00Z"})

from aiffl import db, season, transactions as T  # noqa: E402
from aiffl.league import LeagueError  # noqa: E402

conn = db.connect()
week = season.current_week(conn)
teams = [r[0] for r in conn.execute("SELECT team_id FROM league_teams ORDER BY draft_slot")]
a, b, c = teams[2], teams[3], teams[4]
bench = lambda t: [p["player_id"] for p in season.lineup_detail(conn, t, week)["bench"]]

t1 = T.propose_trade(conn, a, b, bench(a)[:1], bench(b)[:1], "Depth swap")["trade"]
r = T.respond_to_trade(conn, b, t1["trade_id"], True, "OK")
print("after accept:", r["status"], "|", r["note"][:80])
assert T.owner(conn, t1["give_ids"][0])["team_id"] == a, "rosters must not change before approval"

t2 = T.propose_trade(conn, a, c, bench(a)[1:2], bench(c)[:1], "Another")["trade"]
T.respond_to_trade(conn, c, t2["trade_id"], True)

cli = [sys.executable, "-m", "aiffl.commish"]
print(subprocess.run(cli, capture_output=True, text=True, env=os.environ).stdout[:1500])
print(subprocess.run(cli + ["approve", str(t1["trade_id"]), "fair depth swap"], capture_output=True, text=True,
                     env=os.environ).stdout[:200])
assert T.owner(conn, t1["give_ids"][0])["team_id"] == b, "approved trade must execute"
assert conn.execute("SELECT response FROM trades WHERE id = ?", (t1["trade_id"],)).fetchone()[0] == "OK", \
    "approval must keep the other team's reply"
print(subprocess.run(cli + ["veto", str(t2["trade_id"]), "testing veto"], capture_output=True, text=True,
                     env=os.environ).stdout)
assert T.owner(conn, t2["give_ids"][0])["team_id"] == a, "vetoed trade must not execute"
print(f"{a} inbox:", [m["text"][:70] for m in T.messages_for(conn, a)][-2:])

add_tx = conn.execute("SELECT id, team_id FROM transactions WHERE kind = 'add' ORDER BY id DESC LIMIT 1").fetchone()
before = T.faab(conn, add_tx["team_id"])
print(subprocess.run(cli + ["undo", str(add_tx["id"]), "testing undo"], capture_output=True, text=True,
                     env=os.environ).stdout)
print("FAAB refunded:", before, "->", T.faab(conn, add_tx["team_id"]))

# Auto-approval: a third trade accepted 25 hours "ago".
t3 = T.propose_trade(conn, b, c, bench(b)[1:2], bench(c)[1:2], "Late")["trade"]
T.respond_to_trade(conn, c, t3["trade_id"], True)
os.environ["AIFFL_NOW"] = "2026-11-11T16:00:00Z"
print("auto:", T.auto_approve_trades(conn))
print("COMMISSIONER CHECKS PASSED")
