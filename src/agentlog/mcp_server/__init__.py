"""Read-only MCP server over the agentlog SQLite database."""

from __future__ import annotations

from agentlog.mcp_server.server import create_server, main

__all__ = ["create_server", "main"]
