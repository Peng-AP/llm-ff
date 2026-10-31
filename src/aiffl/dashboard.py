"""Static league dashboard (site/index.html), regenerated on every scheduler tick.

    python -m aiffl.dashboard
"""

import html
import sqlite3

from aiffl import clock, db, season, transactions
from aiffl.config import ROOT, load_teams

SITE = ROOT / "site"

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e3e2dc; --accent:#2f6fde;
        --win:#1f8a4c; --loss:#c2402f; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1e1e1c; --text:#ecebe6; --muted:#9a9993;
        --line:#33322f; --accent:#7aa5f5; --win:#5cc48a; --loss:#ee7a68; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; } h2 { font-size: 17px; margin: 28px 0 10px; }
.muted { color: var(--muted); font-size: 13px; }
.grid { display:grid; gap:12px; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
td.num, th.num { text-align:right; font-variant-numeric: tabular-nums; }
.score { font-size:20px; font-weight:650; font-variant-numeric: tabular-nums; }
.win { color: var(--win); } .loss { color: var(--loss); }
.tag { font-size:11px; padding:1px 6px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
.msg { border-bottom:1px solid var(--line); padding:8px 0; white-space:pre-wrap; }
"""


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def write(conn: sqlite3.Connection | None = None) -> None:
    conn = conn or db.connect()
    models = {t.id: t.model for t in load_teams()}
    week = season.current_week(conn)
    parts = [f"<h1>AI Fantasy Football League</h1><div class='muted'>Week {week} · updated {e(clock.eastern())}</div>"]

    rows = "".join(f"<tr><td>{s['rank']}</td><td>{s['team_id']}</td><td>{e(models.get(s['team_id']))}</td>"
                   f"<td class='num'>{s['wins']}-{s['losses']}-{s['ties']}</td><td class='num'>{s['points_for']:.1f}</td>"
                   f"<td class='num'>{s['points_against']:.1f}</td><td class='num'>{transactions.faab(conn, s['team_id'])}</td></tr>"
                   for s in season.standings(conn))
    parts.append("<h2>Standings</h2><div class='card'><table><tr><th>#</th><th>Team</th><th>Model</th>"
                 "<th class='num'>W-L-T</th><th class='num'>PF</th><th class='num'>PA</th><th class='num'>FAAB</th></tr>"
                 f"{rows}</table></div>")

    cards = []
    for m in conn.execute("SELECT * FROM matchups WHERE week = ?", (week,)):
        a_win = m["status"] == "final" and m["score_a"] > m["score_b"]
        b_win = m["status"] == "final" and m["score_b"] > m["score_a"]
        cards.append(f"<div class='card'><span class='tag'>{e(m['kind'])} · {e(m['status'])}</span><table>"
                     f"<tr><td>{m['team_a']}<div class='muted'>{e(models.get(m['team_a']))}</div></td>"
                     f"<td class='num score {'win' if a_win else ''}'>{m['score_a']:.1f}</td></tr>"
                     f"<tr><td>{m['team_b']}<div class='muted'>{e(models.get(m['team_b']))}</div></td>"
                     f"<td class='num score {'win' if b_win else ''}'>{m['score_b']:.1f}</td></tr></table></div>")
    parts.append(f"<h2>Week {week} matchups</h2><div class='grid'>{''.join(cards)}</div>")

    cards = []
    for tid in sorted(models):
        d = season.lineup_detail(conn, tid, week)
        rows = "".join(f"<tr><td>{slot}</td><td>{e(p['name']) if p else '<em>empty</em>'}"
                       f"{' <span class=tag>' + e(p['injury_status']) + '</span>' if p and p['injury_status'] else ''}</td>"
                       f"<td class='num'>{p['projected'] if p else ''}</td><td class='num'>{p['points'] if p else ''}</td></tr>"
                       for slot, p in d["starters"].items())
        bench = ", ".join(e(p["name"]) for p in d["bench"] if p)
        cards.append(f"<div class='card'><b>{tid}</b> <span class='muted'>{e(models[tid])}</span><table>"
                     f"<tr><th>Slot</th><th>Player</th><th class='num'>Proj</th><th class='num'>Pts</th></tr>{rows}</table>"
                     f"<div class='muted' style='margin-top:6px'>Bench: {bench or '—'}</div></div>")
    parts.append(f"<h2>Lineups</h2><div class='grid'>{''.join(cards)}</div>")

    stats = {r["team_id"]: r for r in conn.execute(
        "SELECT team_id, COUNT(*) runs, SUM(ok) ok, ROUND(AVG(seconds)/60.0, 1) avg_min, ROUND(SUM(cost_usd), 2) cost "
        "FROM agent_runs GROUP BY team_id")}
    searches = {r[0]: r[1] for r in conn.execute(
        "SELECT team_id, COUNT(*) FROM tool_calls WHERE tool = 'web_search' GROUP BY team_id")}
    rows = "".join(f"<tr><td>{tid}</td><td>{e(models[tid])}</td><td class='num'>{stats[tid]['runs'] if tid in stats else 0}</td>"
                   f"<td class='num'>{(stats[tid]['runs'] - (stats[tid]['ok'] or 0)) if tid in stats else 0}</td>"
                   f"<td class='num'>{stats[tid]['avg_min'] if tid in stats else ''}</td>"
                   f"<td class='num'>{searches.get(tid, 0)}</td>"
                   f"<td class='num'>{('$' + str(stats[tid]['cost'])) if tid in stats and stats[tid]['cost'] else '—'}</td></tr>"
                   for tid in sorted(models))
    parts.append("<h2>Agents</h2><div class='card'><table><tr><th>Team</th><th>Model</th><th class='num'>Runs</th>"
                 "<th class='num'>Failed</th><th class='num'>Avg min</th><th class='num'>Web searches</th>"
                 f"<th class='num'>Cost</th></tr>{rows}</table><div class='muted'>Cost is per-token spend on "
                 "OpenRouter; subscription teams show the API-equivalent estimate or nothing.</div></div>")

    moves = "".join(f"<div class='msg'>{e(t)}</div>" for t in transactions.recent_transactions(conn, 25))
    msgs = "".join(f"<div class='msg'><b>{e(r['from_team'])}</b> → {e(r['to_team'] or 'everyone')} "
                   f"<span class='muted'>{e(r['at'])}</span><br>{e(r['text'])}</div>"
                   for r in conn.execute("SELECT * FROM messages WHERE to_team IS NULL ORDER BY id DESC LIMIT 15"))
    parts.append(f"<div class='grid'><div><h2>Transactions</h2><div class='card'>{moves or '<span class=muted>None yet</span>'}"
                 f"</div></div><div><h2>League messages</h2><div class='card'>{msgs or '<span class=muted>None yet</span>'}"
                 "</div></div></div>")

    SITE.mkdir(exist_ok=True)
    (SITE / "index.html").write_text(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width, initial-scale=1'><title>AI Fantasy League</title>"
        f"<style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>", encoding="utf-8")


if __name__ == "__main__":
    write()
    print(SITE / "index.html")
