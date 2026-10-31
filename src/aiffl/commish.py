"""Commissioner console (for the human commissioner).

    python -m aiffl.commish                      # what needs attention: trades to review, recent moves
    python -m aiffl.commish approve 7 "fair deal"
    python -m aiffl.commish veto 7 "collusion: one-sided dump"
    python -m aiffl.commish undo 42 "roster-limit exploit"   # reverse a waiver add or a drop (transaction id)
    python -m aiffl.commish say "Reminder: trade deadline is before week 14."
    python -m aiffl.commish claims               # last waiver run's results, all teams
"""

import json
import sys

from aiffl import db, league, transactions as T
from aiffl.config import load_league, load_teams


def show(conn) -> None:
    models = {t.id: t.model for t in load_teams()}
    hours = load_league()["trades"].get("auto_approve_hours", 24)
    pending = T.pending_trades(conn)
    print(f"== Trades awaiting your review ({len(pending)}; each auto-approves {hours}h after acceptance)")
    for t in pending:
        print(f"  #{t['trade_id']}  {t['from_team']} [{models.get(t['from_team'])}] gives {t['from_team_gives']}")
        print(f"        {t['to_team']} [{models.get(t['to_team'])}] gives {t['from_team_receives']}")
        print(f"        accepted {t['responded_at']}  pitch: {t['message']!r}  reply: {t['response']!r}")
        for side in (t["give_ids"], t["receive_ids"]):
            for pid in side:
                s = conn.execute("SELECT full_name, position, ros_projection, injury_status FROM nfl_players "
                                 "JOIN nfl_player_summary USING (player_id) WHERE player_id = ?", (pid,)).fetchone()
                if s:
                    print(f"          {s['full_name']:<24} {s['position']:<3} ROS proj {s['ros_projection']:>6}"
                          f"  {s['injury_status'] or ''}")
    if not pending:
        print("  none")
    print("\n== Recent roster moves (undo with: python -m aiffl.commish undo <id> \"reason\")")
    for r in conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 25"):
        p = conn.execute("SELECT full_name, position FROM nfl_players WHERE player_id = ?", (r["player_id"],)).fetchone()
        print(f"  {r['id']:>4}  {r['at']}  {r['team_id']}  {r['kind']:<17} "
              f"{(p['full_name'] + ' ' + p['position']) if p else '':<28} {r['detail'] or ''}")
    print("\n== Open trade offers (not yet answered)")
    for t in conn.execute("SELECT * FROM trades WHERE status = 'open' ORDER BY id"):
        v = T._trade_view(conn, t)
        print(f"  #{v['trade_id']}  {v['from_team']} -> {v['to_team']}: {v['from_team_gives']} for "
              f"{v['from_team_receives']}")


def claims(conn) -> None:
    last = conn.execute("SELECT MAX(processed_at) FROM waiver_claims").fetchone()[0]
    print(f"== Waiver run {last}")
    for r in conn.execute("SELECT * FROM waiver_claims WHERE processed_at = ? ORDER BY bid DESC", (last,)):
        add = conn.execute("SELECT full_name FROM nfl_players WHERE player_id = ?", (r["add_player"],)).fetchone()
        print(f"  {r['team_id']}  ${r['bid']:<3} {add['full_name'] if add else r['add_player']:<26} {r['status']:<8} "
              f"{r['note'] or ''}")


def main() -> None:
    args = sys.argv[1:]
    conn = db.connect()
    try:
        if not args:
            show(conn)
        elif args[0] == "approve":
            print(json.dumps(T.approve_trade(conn, int(args[1]), args[2] if len(args) > 2 else
                                             "approved by the commissioner"), indent=2, default=str))
        elif args[0] == "veto":
            print(T.veto_trade(conn, int(args[1]), args[2] if len(args) > 2 else "vetoed by the commissioner"))
        elif args[0] == "undo":
            print(T.undo_transaction(conn, int(args[1]), args[2] if len(args) > 2 else "commissioner decision"))
        elif args[0] == "say":
            print(T.send_message(conn, T.COMMISSIONER, None, " ".join(args[1:])))
        elif args[0] == "claims":
            claims(conn)
        else:
            print(__doc__)
    except league.LeagueError as e:
        print(f"Not done: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
