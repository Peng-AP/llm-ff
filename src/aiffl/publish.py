"""Build the public site (site/) from the league database, and deploy it to Cloudflare Pages.

    python -m aiffl.publish build      # site/ only; open site/index.html locally
    python -m aiffl.publish deploy     # build, then push to Cloudflare Pages (needs `npx wrangler login` once)

The site is a static app in web/ plus one generated data file, site/data.js. It shows only what
is public inside the league (rosters, lineups, scores, projections, transactions, trades,
league-wide messages, recaps) plus which model runs each team, waiver bids, and direct messages
between teams. Agents' notes are never published, and agents' web tools exclude this domain, so
bids and direct messages stay hidden from the other teams.
"""

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict

from aiffl import clock, db, league, season, transactions
from aiffl.config import ROOT, load_league, load_teams

WEB = ROOT / "web"
SITE = ROOT / "site"
WRANGLER = "wrangler@4.151.1"
SITE_DOMAIN = "aaronpeng.dev"  # excluded from agents' web search and fetch

MODEL_NAMES = {
    "claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "gpt-6-astra": "GPT-6 Astra", "gpt-6.1-sol": "GPT-6.1 Sol", "z-ai/glm-5.3": "GLM-5.3",
    "deepseek/deepseek-v4-pro-0813": "DeepSeek V4 Pro", "qwen/qwen3.8-flash": "Qwen 3.8 Flash",
    "google/gemini-3.8-flash": "Gemini 3.8 Flash",
}
MAKERS = {"claude": "Anthropic", "gpt": "OpenAI", "z-ai": "Zhipu", "deepseek": "DeepSeek", "qwen": "Alibaba",
          "google": "Google"}
HARNESS_NAMES = {"claude_code": "Claude Code", "codex": "Codex", "openrouter": "OpenRouter"}


def _maker(model: str) -> str:
    return next((v for k, v in MAKERS.items() if model.startswith(k)), "")


def _player_line(conn, pid: str, week: int, roster_row=None) -> dict:
    p = roster_row or conn.execute("SELECT player_id, full_name, position, team, injury_status FROM nfl_players "
                                   "WHERE player_id = ?", (pid,)).fetchone()
    game = season.game_for(conn, p["team"], week)
    proj = season.week_projection(conn, pid, p["position"], week)
    pts = season.player_points(conn, pid, week)
    state = game["state"] if game else "bye"
    if game and state == "pre" and season.is_locked(conn, pid, week):
        state = "in"
    # Live projection: actual points once a game is over; the better of actual and projected while
    # it's being played (a mid-game total understates the finish); the projection before kickoff.
    live = pts if state == "post" else max(pts, proj) if state == "in" else proj if game else 0.0
    return {"id": pid, "name": p["full_name"], "pos": p["position"], "nfl": p["team"],
            "inj": p["injury_status"], "state": state,
            "game": ({"away": game["away"], "home": game["home"], "kickoff": game["kickoff_utc"],
                      "score": [game["away_score"], game["home_score"]]} if game else None),
            "proj": round(proj, 1), "pts": round(pts, 1), "live": round(live, 1)}


def _lineup_block(conn, tid: str, week: int, current: int) -> dict:
    if week < current or week == current:
        lineup = season.get_lineup(conn, tid, week)
    else:
        lineup = season.best_lineup(conn, tid, week, {})  # future week: best lineup by projection
    roster = {r["player_id"]: r for r in season.roster_rows(conn, tid)}
    starters = [{"slot": s, **(_player_line(conn, pid, week, roster.get(pid)) if pid else {})}
                for s, pid in lineup.items()]
    used = set(filter(None, lineup.values()))
    bench = [_player_line(conn, pid, week, r) for pid, r in roster.items() if pid not in used and not r["on_ir"]]
    ir = [_player_line(conn, pid, week, r) for pid, r in roster.items() if r["on_ir"]]
    bench.sort(key=lambda p: -p["proj"])
    return {"starters": starters, "bench": bench, "ir": ir,
            "proj": round(sum(p.get("proj", 0) for p in starters), 1),
            "pts": round(sum(p.get("pts", 0) for p in starters), 1),
            "live": round(sum(p.get("live", 0) for p in starters), 1),
            "set_by_agent": bool(conn.execute("SELECT 1 FROM lineups WHERE week = ? AND team_id = ? AND set_by = "
                                              "'agent'", (week, tid)).fetchone())}


def build_data(conn: sqlite3.Connection) -> dict:
    cfg = load_league()
    teams_cfg = load_teams()
    state = league.draft_state(conn)
    drafted = bool(state and state["status"] == "done")
    current = season.current_week(conn)
    standings = {s["team_id"]: s for s in season.standings(conn)} if drafted else {}

    teams = []
    for i, t in enumerate(teams_cfg):
        runs = conn.execute("SELECT COUNT(seconds) n, SUM(ok) ok, AVG(seconds) secs, SUM(cost_usd) cost "
                            "FROM agent_runs WHERE team_id = ?", (t.id,)).fetchone()  # finished runs only
        tools = {r[0]: r[1] for r in conn.execute("SELECT tool, COUNT(*) FROM tool_calls WHERE team_id = ? "
                                                  "GROUP BY tool", (t.id,))}
        teams.append({
            "id": t.id, "slot": i + 1, "model": t.model, "name": MODEL_NAMES.get(t.model, t.model),
            "maker": _maker(t.model), "harness": HARNESS_NAMES.get(t.harness, t.harness),
            "record": standings.get(t.id), "faab": transactions.faab(conn, t.id) if drafted else None,
            "agent": {"runs": runs["n"], "failed": (runs["n"] or 0) - (runs["ok"] or 0),
                      "avg_min": round((runs["secs"] or 0) / 60, 1), "cost": round(runs["cost"] or 0, 2),
                      "searches": tools.get("web_search", 0), "fetches": tools.get("fetch_page", 0),
                      "tool_calls": sum(tools.values())},
        })

    weeks = defaultdict(list)
    if drafted:
        for m in conn.execute("SELECT * FROM matchups ORDER BY week, rowid"):
            w = m["week"]
            entry = {"week": w, "kind": m["kind"], "status": m["status"], "a": m["team_a"], "b": m["team_b"]}
            if w <= current + 1:
                la, lb = (_lineup_block(conn, m["team_a"], w, current), _lineup_block(conn, m["team_b"], w, current))
                entry.update({"a_lineup": la, "b_lineup": lb})
            entry.update({"score": [m["score_a"], m["score_b"]]})
            weeks[w].append(entry)

    rosters = {}
    if drafted:
        for t in teams_cfg:
            rosters[t.id] = _lineup_block(conn, t.id, current, current)
    weekly_points = defaultdict(dict)
    for m in conn.execute("SELECT * FROM matchups WHERE status = 'final'"):
        weekly_points[m["team_a"]][m["week"]] = m["score_a"]
        weekly_points[m["team_b"]][m["week"]] = m["score_b"]

    picks = [{"no": p["pick_no"], "round": p["round"], "team": p["team_id"], "name": p["full_name"],
              "pos": p["position"], "nfl": p["team"], "method": p["method"]} for p in league.picks_made(conn)]
    tx = [{"id": r["id"], "at": r["at"], "team": r["team_id"], "kind": r["kind"],
           "player": (conn.execute("SELECT full_name || ' (' || position || ', ' || COALESCE(team, 'FA') || ')' "
                                   "FROM nfl_players WHERE player_id = ?", (r["player_id"],)).fetchone() or [None])[0],
           "detail": r["detail"]} for r in conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 500")]
    trades = [transactions._trade_view(conn, t) for t in conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 200")]
    for t in trades:
        t.pop("give_ids"), t.pop("receive_ids")
    messages = [dict(r) for r in conn.execute("SELECT at, from_team, text FROM messages WHERE to_team IS NULL "
                                              "ORDER BY id DESC LIMIT 300")]
    # Like blind bids below, team-to-team DMs are public here only: agents' web tools block this site's domain.
    dms = [dict(r) for r in conn.execute("SELECT at, from_team, to_team, text FROM messages WHERE "
                                         "from_team LIKE 'team%' AND to_team LIKE 'team%' ORDER BY id DESC LIMIT 300")]
    # Blind bids are public here only: agents' web tools block this site's domain.
    claims = [{"team": r["team_id"], "add": transactions._name(conn, r["add_player"]),
               "drop": transactions._name(conn, r["drop_player"]) if r["drop_player"] else None,
               "bid": r["bid"], "priority": r["priority"], "status": r["status"], "note": r["note"],
               "placed": r["created_at"], "processed": r["processed_at"]}
              for r in conn.execute("SELECT * FROM waiver_claims WHERE status != 'cancelled' "
                                    "ORDER BY (status = 'pending') DESC, processed_at DESC, bid DESC LIMIT 400")]
    recaps = [{"week": int(p.stem.split("-")[1]), "markdown": p.read_text(encoding="utf-8")}
              for p in sorted((ROOT / "recaps").glob("week-*.md"), reverse=True)]

    from aiffl import scoring
    rules = {
        "format": cfg.get("format", "redraft"), "teams": len(teams_cfg), "roster": cfg["roster"],
        "scoring": cfg["scoring"], "other_td": 6, "kicker": scoring.KICKER, "dst": scoring.DST,
        "dst_allowed": [[lim, pts] for lim, pts in scoring.DST_POINTS_ALLOWED],
        "dst_allowed_else": scoring.DST_POINTS_ALLOWED_ELSE,
        "waivers": cfg["waivers"], "trades": cfg["trades"], "schedule": cfg["schedule"],
        "draft_rounds": state["rounds"] if state else None,
    }

    return {
        "rules": rules,
        "league": {"name": cfg["name"], "season": cfg["season"], "updated": clock.now_iso(),
                   "current_week": current, "draft_status": state["status"] if state else None,
                   "start_week": cfg["schedule"]["start_week"],
                   "regular_season_end": cfg["schedule"]["regular_season_end_week"],
                   "playoff_weeks": cfg["schedule"]["playoff_weeks"],
                   "next_waivers": transactions.next_processing() if drafted else None},
        "teams": teams, "weeks": weeks, "rosters": rosters, "weekly_points": weekly_points,
        "draft": {"order": league.draft_order(conn), "rounds": state["rounds"] if state else 0, "picks": picks},
        "transactions": tx, "trades": trades, "messages": messages, "dms": dms, "recaps": recaps, "claims": claims,
    }


def build(conn: sqlite3.Connection | None = None) -> str:
    """Write site/ and return a hash of its contents (to skip deploys when nothing changed)."""
    conn = conn or db.connect()
    data = build_data(conn)
    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(WEB, SITE)
    # Version the app's own files so browsers never run a stale copy, and so an open tab can tell
    # (via data.js) that a newer version was deployed and reload itself.
    assets = hashlib.sha256(b"".join((WEB / f).read_bytes() for f in ("app.js", "style.css", "index.html"))
                            ).hexdigest()[:12]
    index = (SITE / "index.html").read_text(encoding="utf-8")
    index = index.replace('src="app.js"', f'src="app.js?v={assets}"').replace('href="style.css"',
                                                                              f'href="style.css?v={assets}"')
    (SITE / "index.html").write_text(index, encoding="utf-8")
    data["league"]["assets"] = assets
    payload = json.dumps(data, default=str, separators=(",", ":"))
    (SITE / "data.js").write_text(f"window.LEAGUE = {payload};\n", encoding="utf-8")
    stable = json.dumps({k: v for k, v in data.items() if k != "league"}, default=str, sort_keys=True)
    return hashlib.sha256(stable.encode()).hexdigest()


def deploy() -> None:
    """Upload site/ as Workers static assets; wrangler.jsonc names the Worker and the custom domain."""
    npx = shutil.which("npx") or "npx"
    cmd = [npx, "--yes", WRANGLER, "deploy", "--config", str(ROOT / "wrangler.jsonc")]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, shell=(sys.platform == "win32"))
    if proc.returncode != 0:
        raise RuntimeError(f"wrangler deploy failed: {(proc.stderr or proc.stdout)[-800:]}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    build()
    print(f"built {SITE / 'index.html'}")
    if cmd == "deploy":
        deploy()
        print("deployed (see wrangler.jsonc for the Worker name and domain)")


if __name__ == "__main__":
    main()
