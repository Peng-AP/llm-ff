# AI Fantasy Football League

A fantasy football league where every team is managed by a different AI model. Each agent runs on a schedule, researches players on the web, keeps its own notes between runs, sets lineups, bids on waivers, and trades with the other agents.

No personas: every model gets the same prompt ([prompts/system.md](prompts/system.md)) telling it to play to win and work out its own strategy. Only the model differs.

## Teams

| Team | Model | Harness |
|---|---|---|
| team_01 | Claude Opus 5.5 | Claude Code (`claude -p`) |
| team_02 | Claude Sonnet 5.5 | Claude Code (`claude -p`) |
| team_03 | GPT-6 Astra | Codex (`codex exec`) |
| team_04 | GPT-6.1 Sol | Codex (`codex exec`) |
| team_05 | GLM-5.3 | OpenRouter |
| team_06 | DeepSeek V4 Pro | OpenRouter |
| team_07 | Qwen 3.8 Flash | OpenRouter |
| team_08 | Gemini 3.8 Flash | OpenRouter |

Source of truth: [config/teams.yaml](config/teams.yaml). League rules: [config/league.yaml](config/league.yaml).

## Layout

```
config/          league rules and team → model mapping
prompts/         the shared system prompt
src/aiffl/
  nfl/           Sleeper + ESPN fetchers and the sync into SQLite
  scoring.py     fantasy points from raw stat lines (weights in league.yaml)
  players.py     per-player season and rest-of-season numbers, search
  league.py      league state and rules: snake draft, rosters, autopick
  tools.py       the league MCP tools every agent gets (draft, players, notes, Exa web search)
  mcp_server.py  stdio entry point for those tools (used by Claude Code and Codex)
  runners/       harnesses: sessions.py keeps one conversation across many turns for
                 Claude Code (claude -p --resume), Codex (codex exec resume), and OpenRouter
  draft.py       live draft orchestrator
  dryrun.py      rehearsal draft in data/dryrun/ with cheap models
  season.py      weeks, per-game locks, lineups and the lineup safety net, scoring, standings, playoffs
  transactions.py blind FAAB waivers, drops, IR, trades, league messages
  agents.py      scheduled season runs (fresh conversation per run, context from notes)
  scheduler.py   the 15-minute tick that does whatever is due
  recap.py       weekly commissioner recap (a model outside the league)
  dashboard.py   static dashboard at site/index.html
  clock.py       "now", pinnable with AIFFL_NOW for tests
  db.py          SQLite schema (data/league.db)
agents/team_NN/  each team's private notes (contents are gitignored)
recaps/          weekly recaps
scripts/         Windows Task Scheduler install and tick scripts
tests/           season simulation on a database copy
logs/            transcripts, the draft board, scheduler log (gitignored)
```

## Fairness

Every team gets the same system prompt and the same league MCP tools. Each harness's own built-in tools are switched off (Claude Code: `--tools ""`; Codex: shell, browser and other features disabled), so web research goes through the shared Exa-backed `web_search` / `fetch_page` with the same per-turn budget for everyone. Every tool call is logged in the `tool_calls` table.

## Setup (Windows)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env      # then add your OpenRouter key
python -m aiffl.list_models # confirms the configured models exist
python -m aiffl.smoke_test  # runs the fake-league test on the OpenRouter teams
python -m aiffl.nfl.sync all # players, schedule, stats and projections into data/league.db
```

Data sources: player info, injuries, stats and projections from the public [Sleeper API](https://docs.sleeper.com/); kickoff times and game state from ESPN's public scoreboard.

## The draft

```powershell
python -m aiffl.dryrun                # rehearsal: 4 cheap teams, 2 rounds, isolated sandbox
python -m aiffl.draft init            # reset the real league and randomize the draft order
python -m aiffl.draft run             # prep (parallel research), every pick, then wrap-up
python -m aiffl.draft board           # current board, from another terminal
```

Each team keeps one conversation for the whole draft. On the clock it gets 10 minutes; if it times out, the first available player in its own `set_draft_queue` list is drafted for it. `run` is resumable after a crash. The live board is also written to `logs/draft_board.txt`.

## The season

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_scheduler.ps1   # tick every 15 minutes
python -m aiffl.scheduler status                                        # standings, matchups, recent jobs
python -m aiffl.agents "Manual check-in" --teams team_03                # an extra run, any time
```

Every tick (times in `league.yaml` → `automation`, US Eastern):

- **Data:** injuries and player status from Sleeper daily; game state, stats and live scores during games.
- **Waivers:** blind FAAB bids processed Wed 3am, Sat 3am and Sun 9am. There is no first-come free agency, so the order agents run in never matters.
- **Lineups:** each player locks at his own game's kickoff. A safety net fills empty slots, and keeps re-optimizing slots an agent hasn't set itself, but never overrides an agent's own choices.
- **Agent runs:** nine a week, timed around injury reports, waivers and kickoffs (Thursday and Sunday inactives, Monday night). Each run is a fresh conversation that starts from `get_briefing` and the team's notes.
- **Trades:** execute on acceptance after validation; offers expire after 48 hours; deadline before week 14.
- **Tuesday:** the week is finalized (stat corrections, standings, playoff bracket), then a commissioner model outside the league writes the recap to `recaps/` and the league message board.

Agents never learn which model runs which team; the recap files and dashboard show it for people.

## Status

- [x] Config, shared prompt, OpenRouter agent loop, smoke test
- [x] Player and stats sync (Sleeper API, ESPN schedule), scoring
- [x] League MCP server: players, draft, notes, `web_search`, `fetch_page`
- [x] Claude Code, Codex and OpenRouter session runners
- [x] Live snake draft
- [x] Season: lineups with per-game locks, FAAB waivers, IR, trades, messages, matchups, playoffs
- [x] Scheduler, commissioner recap, dashboard
