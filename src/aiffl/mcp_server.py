"""stdio MCP server for one team. Launched by Claude Code / Codex via their MCP config.

    python -m aiffl.mcp_server --team team_01
"""

import argparse

from aiffl.tools import build_server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", required=True)
    args = parser.parse_args()
    build_server(args.team).run("stdio")


if __name__ == "__main__":
    main()
