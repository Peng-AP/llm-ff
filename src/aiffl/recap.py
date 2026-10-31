"""Weekly recap, written by Claude Opus from the week's facts only.

Opus runs through `claude -p` on the Claude subscription with no tools, no MCP servers and none
of team_01's notes, so it knows only what is in the facts below (team_01 is also Opus; this is a
separate, stateless instance). If the CLI fails, Opus on OpenRouter is the fallback.

The version posted to the league message board names teams only. The file in recaps/ adds a
model legend for human readers; agents never learn which model runs which team.
"""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

from aiffl import clock, season, transactions
from aiffl.config import ROOT, load_teams
from aiffl.runners.openrouter import PROVIDER_ROUTING, make_client

RECAP_MODEL = "claude-opus-5-5"                  # via claude -p
RECAP_MODEL_OPENROUTER = "anthropic/claude-opus-5.5"
RECAPS_DIR = ROOT / "recaps"
POSTED_AS = "league_recap"


def _via_claude_code(prompt: str) -> str | None:
    exe = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")
    workdir = ROOT / "data" / "recap"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run([exe, "-p", "--model", RECAP_MODEL, "--output-format", "json", "--tools", "",
                               "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"],
                              input=prompt, capture_output=True, text=True, encoding="utf-8", cwd=workdir,
                              timeout=600)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        return None
    return None if data.get("is_error") else (data.get("result") or "").strip() or None


def _via_openrouter(prompt: str) -> str:
    resp = make_client().chat.completions.create(model=RECAP_MODEL_OPENROUTER,
                                                 messages=[{"role": "user", "content": prompt}],
                                                 extra_body={"provider": PROVIDER_ROUTING})
    return (resp.choices[0].message.content or "").strip()


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
    chatter = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE at >= ? AND from_team != ? "
                                             "AND to_team IS NULL", (start or "", POSTED_AS))]
    top = sorted((p for p in performers if "points" in p), key=lambda p: -p["points"])[:8]
    return {"week": week, "matchups": matchups, "top_starters": top,
            "bench_regrets": [p for p in performers if "best_bench_points" in p],
            "standings": season.standings(conn), "transactions": moves, "trades": trades,
            "league_messages": chatter[-20:]}


def write_recap(conn: sqlite3.Connection, week: int):
    facts = week_facts(conn, week)
    prompt = ("You write the weekly recap for a fantasy football league whose eight teams are each run by an AI "
              "agent (a person serves as commissioner). Write this week's recap: 250-450 words of Markdown, "
              "lively but strictly factual. Use only the facts below; do not invent stats, quotes, or news, and "
              "do not use any tools. Refer to teams by their IDs (team_01 ...). Cover every matchup result, "
              "standout performances, costly bench decisions, notable moves and trades, and the standings "
              "picture. Reply with the recap only.\n\nFACTS:\n" + json.dumps(facts, default=str))
    text, writer = _via_claude_code(prompt), f"{RECAP_MODEL} via Claude Code"
    if not text:
        text, writer = _via_openrouter(prompt), f"{RECAP_MODEL_OPENROUTER} via OpenRouter"
    transactions.send_message(conn, POSTED_AS, None, f"Week {week} recap\n\n{text}")
    legend = "\n".join(f"| {t.id} | {t.model} |" for t in load_teams())
    RECAPS_DIR.mkdir(exist_ok=True)
    path = RECAPS_DIR / f"week-{week:02d}.md"
    path.write_text(f"# Week {week} recap\n\n_Written {clock.eastern()} by {writer}, from league data only._\n\n"
                    f"{text}\n\n## Who's who\n\n| Team | Model |\n|---|---|\n{legend}\n", encoding="utf-8")
    return path
