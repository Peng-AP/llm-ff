"""Load league and team configuration from config/ and prompts/."""

from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
AGENTS_DIR = ROOT / "agents"
LOGS_DIR = ROOT / "logs"


@dataclass(frozen=True)
class Team:
    id: str
    model: str
    harness: str

    @property
    def workspace(self) -> Path:
        return AGENTS_DIR / self.id


def load_teams() -> list[Team]:
    data = yaml.safe_load((CONFIG_DIR / "teams.yaml").read_text(encoding="utf-8"))
    return [Team(**t) for t in data["teams"]]


def load_league() -> dict:
    return yaml.safe_load((CONFIG_DIR / "league.yaml").read_text(encoding="utf-8"))


def system_prompt(team: Team, num_teams: int, run_context: str) -> str:
    template = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    return template.format(team_id=team.id, num_teams=num_teams, run_context=run_context)
