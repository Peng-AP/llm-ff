"""Rehearse the draft in an isolated sandbox (data/dryrun/) with cheap models and short clocks.

    python -m aiffl.dryrun [--rounds 2] [--prep-minutes 8] [--pick-minutes 5]

Copies the real NFL data, never touches the real league, agents, or logs.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "data" / "dryrun"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--prep-minutes", type=int, default=8)
    ap.add_argument("--pick-minutes", type=int, default=5)
    args = ap.parse_args()

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)
    shutil.copy(ROOT / "data" / "league.db", SANDBOX / "league.db")
    # Must be set before any aiffl module that reads paths is imported.
    os.environ.update({
        "AIFFL_DB": str(SANDBOX / "league.db"),
        "AIFFL_AGENTS_DIR": str(SANDBOX / "agents"),
        "AIFFL_LOGS_DIR": str(SANDBOX / "logs"),
        "AIFFL_TEAMS": str(ROOT / "config" / "teams.dryrun.yaml"),
    })
    from aiffl import draft

    sys.argv = ["draft", "init", "--rounds", str(args.rounds), "--seed", "7"]
    draft.main()
    sys.argv = ["draft", "run", "--prep-minutes", str(args.prep_minutes), "--pick-minutes", str(args.pick_minutes)]
    draft.main()


if __name__ == "__main__":
    main()
