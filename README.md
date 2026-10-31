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
  runners/       harnesses that drive one agent run (OpenRouter loop so far)
  list_models.py check configured models against OpenRouter
  smoke_test.py  fake-league test that a model can use the tools
agents/team_NN/  each team's private notes (contents are gitignored)
logs/            run transcripts (gitignored)
```

## Setup (Windows)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
copy .env.example .env      # then add your OpenRouter key
python -m aiffl.list_models # confirms the configured models exist
python -m aiffl.smoke_test  # runs the fake-league test on the OpenRouter teams
```

## Status

- [x] Config, shared prompt, OpenRouter agent loop, smoke test
- [ ] League server (rosters, lineups, waivers, trades, scoring, lineup locks)
- [ ] Player and stats sync (Sleeper API)
- [ ] League MCP server with per-team auth, `web_search`, `fetch_page`, notes
- [ ] Claude Code and Codex runners
- [ ] Draft, scheduler, commissioner, dashboard
