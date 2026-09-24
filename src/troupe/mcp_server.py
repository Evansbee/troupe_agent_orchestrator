"""Per-agent MCP server (stdio). Spawned by claude/codex; identity comes from the environment."""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import load_runtime
from .store import Store
from .team import TeamAPI


def build_server(root: Path, agent_id: str) -> tuple[MCPServer, TeamAPI]:
    cfg = load_runtime(root)
    api = TeamAPI(cfg, Store(cfg.db_path), agent_id)
    server = MCPServer(
        "troupe",
        instructions=f"Troupe team tools for agent '{api.names.name(agent_id)}': mailbox, tasks, questions for the human, memory.",
    )
    for fn in api.tools():
        server.add_tool(fn, structured_output=False)
    return server, api


def main() -> None:
    root = Path(os.environ["TROUPE_ROOT"])
    agent_id = os.environ["TROUPE_AGENT"]
    server, _ = build_server(root, agent_id)
    server.run("stdio")


if __name__ == "__main__":
    main()
