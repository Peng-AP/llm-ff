"""List OpenRouter models that support tool calling, and check the ones in teams.yaml.

Usage:
    python -m aiffl.list_models            # check configured teams only
    python -m aiffl.list_models --all      # also print every tool-capable model
"""

import sys

import httpx

from aiffl.config import load_teams

MODELS_URL = "https://openrouter.ai/api/v1/models"


def per_million(price: str) -> float:
    return float(price) * 1_000_000


def main() -> None:
    resp = httpx.get(MODELS_URL, params={"supported_parameters": "tools"}, timeout=30)
    resp.raise_for_status()
    models = {m["id"]: m for m in resp.json()["data"]}

    if "--all" in sys.argv:
        for mid in sorted(models):
            p = models[mid]["pricing"]
            print(f"{mid:55} in ${per_million(p['prompt']):6.2f}/M  out ${per_million(p['completion']):6.2f}/M")
        print()

    print("Configured OpenRouter teams:")
    ok = True
    for team in load_teams():
        if team.harness != "openrouter":
            continue
        m = models.get(team.model)
        if m is None:
            ok = False
            print(f"  {team.id}  {team.model:40}  MISSING (not found, or no tool support)")
            continue
        p = m["pricing"]
        print(f"  {team.id}  {team.model:40}  ok  in ${per_million(p['prompt']):.2f}/M  "
              f"out ${per_million(p['completion']):.2f}/M  ctx {m.get('context_length')}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
