"""Load league and team configuration from config/ and prompts/.

Runtime locations can be overridden with environment variables so a dry run never touches
the real league: AIFFL_DB, AIFFL_AGENTS_DIR, AIFFL_LOGS_DIR, AIFFL_TEAMS.
"""

import copy
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"


def _path_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).resolve() if value else default


AGENTS_DIR = _path_env("AIFFL_AGENTS_DIR", ROOT / "agents")
LOGS_DIR = _path_env("AIFFL_LOGS_DIR", ROOT / "logs")
TEAMS_FILE = _path_env("AIFFL_TEAMS", CONFIG_DIR / "teams.yaml")


@dataclass(frozen=True)
class Team:
    id: str
    model: str
    harness: str

    @property
    def workspace(self) -> Path:
        return AGENTS_DIR / self.id

    @property
    def notes_dir(self) -> Path:
        return self.workspace / "notes"


def load_teams() -> list[Team]:
    data = yaml.safe_load(TEAMS_FILE.read_text(encoding="utf-8"))
    return [Team(**t) for t in data["teams"]]


def team_by_id(team_id: str) -> Team:
    for t in load_teams():
        if t.id == team_id:
            return t
    raise KeyError(f"Unknown team {team_id!r}")


_league_cache: tuple[int, dict] | None = None


def load_league() -> dict:
    """league.yaml, parsed once per edit (it's read thousands of times per site build). Callers get a copy."""
    global _league_cache
    path = CONFIG_DIR / "league.yaml"
    mtime = path.stat().st_mtime_ns
    if not _league_cache or _league_cache[0] != mtime:
        _league_cache = (mtime, yaml.safe_load(path.read_text(encoding="utf-8")))
    return copy.deepcopy(_league_cache[1])


def system_prompt(team: Team, num_teams: int, run_context: str) -> str:
    template = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")
    return template.format(team_id=team.id, num_teams=num_teams, run_context=run_context)


def runtime_env() -> dict[str, str]:
    """Environment variables that pin a child process (MCP server, CLI agent) to this league."""
    from aiffl.db import DB_PATH  # avoid an import cycle
    return {
        "AIFFL_DB": str(DB_PATH),
        "AIFFL_AGENTS_DIR": str(AGENTS_DIR),
        "AIFFL_LOGS_DIR": str(LOGS_DIR),
        "AIFFL_TEAMS": str(TEAMS_FILE),
    }
