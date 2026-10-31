"""Check that a model can drive the agent loop: read notes, inspect a roster, set a valid
optimal lineup, and write notes. Uses the real system prompt with a fake in-memory league.

Usage:
    python -m aiffl.smoke_test                  # every openrouter team in teams.yaml
    python -m aiffl.smoke_test qwen/qwen3.8-flash
"""

import sys

from aiffl.config import Team, load_teams, system_prompt
from aiffl.runners.openrouter import make_client, run_agent, save_transcript

SLOTS = {"QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "FLEX": {"RB", "WR", "TE"}}

PLAYERS = {
    "p1": {"name": "Quarterback A", "pos": "QB", "status": "active", "projected": 18.4},
    "p2": {"name": "Quarterback B", "pos": "QB", "status": "active", "projected": 14.1},
    "p3": {"name": "Running Back C", "pos": "RB", "status": "OUT", "projected": 0.0},
    "p4": {"name": "Running Back D", "pos": "RB", "status": "active", "projected": 11.2},
    "p5": {"name": "Running Back E", "pos": "RB", "status": "active", "projected": 8.0},
    "p6": {"name": "Receiver F", "pos": "WR", "status": "BYE", "projected": 0.0},
    "p7": {"name": "Receiver G", "pos": "WR", "status": "active", "projected": 12.5},
    "p8": {"name": "Receiver H", "pos": "WR", "status": "active", "projected": 9.3},
}
OPTIMAL = {"QB": "p1", "RB": "p4", "WR": "p7", "FLEX": "p8"}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_notes",
        "description": "List the files in your private notes folder.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_note",
        "description": "Read one of your note files.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "write_note",
        "description": "Create or overwrite one of your note files.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "content": {"type": "string"}}, "required": ["name", "content"]},
    }},
    {"type": "function", "function": {
        "name": "get_my_team",
        "description": "Your roster with status and projected points, plus the lineup slots to fill.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "set_lineup",
        "description": "Set your starters. Map every slot to a player_id from your roster.",
        "parameters": {"type": "object", "properties": {
            "lineup": {"type": "object", "description": "slot -> player_id, e.g. {\"QB\": \"p1\", ...}",
                       "additionalProperties": {"type": "string"}}},
            "required": ["lineup"]},
    }},
]


class FakeLeague:
    def __init__(self):
        self.notes: dict[str, str] = {}
        self.lineup: dict[str, str] | None = None

    def handle(self, name: str, args: dict) -> object:
        match name:
            case "list_notes":
                return {"files": sorted(self.notes)}
            case "read_note":
                if args["name"] not in self.notes:
                    return {"error": f"No note named {args['name']!r}. Existing: {sorted(self.notes)}"}
                return {"content": self.notes[args["name"]]}
            case "write_note":
                self.notes[args["name"]] = args["content"]
                return {"ok": True}
            case "get_my_team":
                return {"slots": {s: sorted(p) for s, p in SLOTS.items()},
                        "players": [{"player_id": pid, **p} for pid, p in PLAYERS.items()]}
            case "set_lineup":
                return self._set_lineup(args.get("lineup") or {})
        return {"error": f"Unknown tool {name!r}"}

    def _set_lineup(self, lineup: dict) -> dict:
        if set(lineup) != set(SLOTS):
            return {"error": f"Lineup must fill exactly these slots: {sorted(SLOTS)}"}
        if len(set(lineup.values())) != len(lineup):
            return {"error": "A player can only fill one slot."}
        for slot, pid in lineup.items():
            if pid not in PLAYERS:
                return {"error": f"{pid!r} is not on your roster."}
            if PLAYERS[pid]["pos"] not in SLOTS[slot]:
                return {"error": f"{pid} ({PLAYERS[pid]['pos']}) cannot play {slot}."}
        self.lineup = dict(lineup)
        return {"ok": True, "lineup": self.lineup}

    def check_finish(self) -> str | None:
        missing = []
        if self.lineup is None:
            missing.append("set a valid lineup")
        if "summary.md" not in self.notes:
            missing.append("write summary.md")
        if missing:
            return f"Before finishing you still need to: {', '.join(missing)}. Do that now."
        return None


def grade(league: FakeLeague) -> str:
    if league.lineup is None:
        return "FAIL  no valid lineup set"
    bad = [pid for pid in league.lineup.values() if PLAYERS[pid]["status"] != "active"]
    if bad:
        return f"FAIL  started inactive players {bad}"
    lineup_ok = league.lineup == OPTIMAL
    notes_ok = "summary.md" in league.notes
    if lineup_ok and notes_ok:
        return "PASS"
    problems = []
    if not lineup_ok:
        problems.append(f"suboptimal lineup {league.lineup}")
    if not notes_ok:
        problems.append("did not write summary.md")
    return "WARN  " + "; ".join(problems)


def smoke(team: Team, num_teams: int, client) -> None:
    league = FakeLeague()
    system = system_prompt(team, num_teams, run_context="Smoke test. Week 10, Sunday 11:00 ET. No games have started.")
    result = run_agent(team.model, system, "Set your lineup for this week.", TOOLS, league.handle,
                       max_turns=15, client=client, check_finish=league.check_finish)
    path = save_transcript(result, f"smoke_{team.id}")
    print(f"{team.id}  {team.model:40} {grade(league):50} "
          f"turns={result.turns} tools={result.tool_calls} reminders={result.reminders} "
          f"cost=${result.cost_usd:.4f}  ({result.stopped_reason})")
    print(f"        transcript: {path}")


def main() -> None:
    teams = load_teams()
    if len(sys.argv) > 1:
        targets = [Team(id="team_test", model=sys.argv[1], harness="openrouter")]
    else:
        targets = [t for t in teams if t.harness == "openrouter"]

    client = make_client()
    for team in targets:
        try:
            smoke(team, len(teams), client)
        except Exception as e:
            print(f"{team.id}  {team.model:40} ERROR  {e}")


if __name__ == "__main__":
    main()
