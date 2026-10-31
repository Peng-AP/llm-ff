"""Weekly commissioner recap: a model outside the league writes it from the week's facts.

The version posted to the league message board names teams only. The file in recaps/ adds a
model legend for human readers; agents never learn which model runs which team.
"""

import json
import sqlite3

from aiffl import clock, season, transactions
from aiffl.config import ROOT, load_teams
from aiffl.runners.openrouter import PROVIDER_ROUTING, make_client

COMMISSIONER_MODEL = "anthropic/claude-haiku-5.5"  # not one of the league's models
RECAPS_DIR = ROOT / "recaps"


def week_facts(conn: sqlite3.Connection, week: int) -> dict:
    matchups = [dict(m) for m in conn.execute("SELECT * FROM matchups WHERE week = ?", (week,))]
    performers = []
    for tid in [r[0] for r in conn.execute("SELECT team_id FROM league_teams")]:
        lineup = season.get_lineup(conn, tid, week)
        starters = set(filter(None, lineup.values()))
        bench_best = 0.0
        for r in season.roster_rows(conn, tid):
            pts = season.player_points(conn, r["player_id"], week)
            if r["player_id"] in starters:
                performers.append({"team": tid, "player": r["full_name"], "pos": r["position"], "points": pts})
            else:
                bench_best = max(bench_best, pts)
        performers.append({"team": tid, "best_bench_points": bench_best})
    start = conn.execute("SELECT MIN(kickoff_utc) FROM nfl_games WHERE week = ?", (week,)).fetchone()[0]
    moves = [dict(r) for r in conn.execute("SELECT * FROM transactions WHERE at >= ? ORDER BY id", (start or "",))]
    trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE created_at >= ?", (start or "",))]
    chatter = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE at >= ? AND from_team != 'commissioner' "
                                             "AND to_team IS NULL", (start or "",))]
    top = sorted((p for p in performers if "points" in p), key=lambda p: -p["points"])[:8]
    return {"week": week, "matchups": matchups, "top_starters": top,
            "bench_regrets": [p for p in performers if "best_bench_points" in p],
            "standings": season.standings(conn), "transactions": moves, "trades": trades,
            "league_messages": chatter[-20:]}


def write_recap(conn: sqlite3.Connection, week: int):
    facts = week_facts(conn, week)
    prompt = ("You are the commissioner of a fantasy football league whose eight teams are each run by an AI "
              "agent. Write the week's recap for the league: 250-450 words of Markdown, lively but strictly "
              "factual. Use only the facts below; do not invent stats, quotes, or news. Refer to teams by "
              "their IDs (team_01 ...). Cover every matchup result, standout performances, costly bench "
              "decisions, notable moves and trades, and the standings picture.\n\nFACTS:\n"
              + json.dumps(facts, default=str))
    resp = make_client().chat.completions.create(
        model=COMMISSIONER_MODEL, messages=[{"role": "user", "content": prompt}],
        extra_body={"provider": PROVIDER_ROUTING})
    text = (resp.choices[0].message.content or "").strip()
    transactions.send_message(conn, "commissioner", None, f"Week {week} recap\n\n{text}")
    legend = "\n".join(f"| {t.id} | {t.model} |" for t in load_teams())
    RECAPS_DIR.mkdir(exist_ok=True)
    path = RECAPS_DIR / f"week-{week:02d}.md"
    path.write_text(f"# Week {week} recap\n\n_Written {clock.eastern()} by the commissioner "
                    f"({COMMISSIONER_MODEL})._\n\n{text}\n\n## Who's who\n\n| Team | Model |\n|---|---|\n{legend}\n",
                    encoding="utf-8")
    return path
