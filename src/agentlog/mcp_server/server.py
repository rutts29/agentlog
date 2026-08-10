"""MCP stdio server wiring for agentlog read-only tools."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from agentlog import __version__
from agentlog.mcp_server import tools
from agentlog.mcp_server.db import connect_readonly, resolve_db_path

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

TOOL_NAMES = (
    "search_sessions",
    "get_session",
    "usage_stats",
    "attention_inbox",
    "skill_inventory",
    "agreement_and_extraction_status",
)


def create_server(db_path: Path | str | None = None) -> MCPServer:
    """Build the MCP server. ``db_path`` defaults to ``AGENTLOG_DB`` or ~/.agentlog."""
    path = resolve_db_path(db_path)

    mcp = MCPServer(
        "agentlog",
        version=__version__,
        instructions=(
            "Read-only access to the owner's local agentlog usage history "
            "(sessions, messages, attention inbox, skills, extraction status). "
            "No writes, no shell, no file mutations. "
            "agentlog never edits agent configuration; config proposals are "
            "reviewed by the owner on the dashboard Proposals board "
            "(HTTP API GET /api/proposals) and applied by hand."
        ),
    )

    def _conn():
        return connect_readonly(path)

    @mcp.tool(annotations=_READ_ONLY)
    def search_sessions(
        query: Annotated[str, Field(description="Search text over messages, repo, cwd, model, id.")],
        harness: Annotated[
            str | None,
            Field(description="Optional harness filter: codex, claude, cursor, warp, hermes."),
        ] = None,
        since: Annotated[
            str | None,
            Field(description="Optional ISO lower bound on session started_at."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=50, description="Max sessions to return (default 10)."),
        ] = 10,
    ) -> dict[str, Any]:
        """Search sessions by message text and metadata. Returns compact session rows."""
        with _conn() as conn:
            return tools.search_sessions(
                conn, query, harness=harness, since=since, limit=limit
            )

    @mcp.tool(annotations=_READ_ONLY)
    def get_session(
        session_id: Annotated[str, Field(description="Session id (harness:external_id).")],
        include_messages: Annotated[
            bool,
            Field(description="Include truncated messages (default true)."),
        ] = True,
    ) -> dict[str, Any]:
        """Fetch one session with truncated messages (no full transcripts)."""
        with _conn() as conn:
            return tools.get_session(
                conn, session_id, include_messages=include_messages
            )

    @mcp.tool(annotations=_READ_ONLY)
    def usage_stats(
        group_by: Annotated[
            Literal["harness", "model", "day", "repo", "agent_profile"],
            Field(description="Bucket key for aggregation."),
        ],
        since: Annotated[
            str | None,
            Field(description="Optional ISO lower bound on session started_at."),
        ] = None,
    ) -> dict[str, Any]:
        """Session counts and durations by harness, model, agent_profile, day, or repo."""
        with _conn() as conn:
            return tools.usage_stats(conn, group_by, since=since)

    @mcp.tool(annotations=_READ_ONLY)
    def attention_inbox() -> dict[str, Any]:
        """Sessions that may need attention (stale, errors, waiting on user, long-running)."""
        with _conn() as conn:
            return tools.attention_inbox(conn)

    @mcp.tool(annotations=_READ_ONLY)
    def skill_inventory() -> dict[str, Any]:
        """Indexed skills with exposure counts (compact)."""
        with _conn() as conn:
            return tools.skill_inventory(conn)

    @mcp.tool(annotations=_READ_ONLY)
    def agreement_and_extraction_status() -> dict[str, Any]:
        """UX extraction progress: observation counts, window coverage, label distributions."""
        with _conn() as conn:
            return tools.agreement_and_extraction_status(conn)

    return mcp


def main(db_path: Path | str | None = None) -> None:
    create_server(db_path).run()
