"""Copy a league database and autodraft all teams into it, for scheduler and agent tests.

    python tests/make_sim_db.py <source league.db> <destination league.db>
"""

import os
import sqlite3
import sys

src, dst = sys.argv[1], sys.argv[2]
s, d = sqlite3.connect(src), sqlite3.connect(dst)
s.backup(d)
d.close()
os.environ["AIFFL_DB"] = dst

from aiffl import db, league  # noqa: E402
from aiffl.config import load_teams  # noqa: E402

conn = db.connect()
league.init_league(conn, load_teams(), seed=11)
league.set_draft_status(conn, "live")
while league.current_pick(conn):
    league.autopick(conn, league.current_pick(conn)["team_id"])
league.set_draft_status(conn, "done")
print("sim league ready:", conn.execute("SELECT COUNT(*) FROM rosters").fetchone()[0], "players rostered")
