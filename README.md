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
  db.py          SQLite schema (data/league.db)
agents/team_NN/  each team's private notes (contents are gitignored)
logs/            transcripts and the live draft board (gitignored)
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

## Status

- [x] Config, shared prompt, OpenRouter agent loop, smoke test
- [x] Player and stats sync (Sleeper API, ESPN schedule), scoring
- [x] League MCP server: players, draft, notes, `web_search`, `fetch_page`
- [x] Claude Code, Codex and OpenRouter session runners
- [x] Live snake draft
- [ ] Season: lineups with per-game locks, FAAB waivers, trades, matchups
- [ ] Scheduler, commissioner, dashboard
