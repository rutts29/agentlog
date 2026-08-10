# agentlog MCP server (read-only)

Exposes local usage history to coding agents (Cursor, Claude Code, Codex) over MCP stdio. Every tool opens the SQLite database with `mode=ro`. No writes, shell, or file mutations.

## Run

```bash
.venv/bin/python -m agentlog.mcp_server
```

Optional DB path: set `AGENTLOG_DB` (default `~/.agentlog/agentlog.db`).

## Cursor MCP settings

Paste into Cursor MCP config (adjust the absolute paths):

```json
{
  "mcpServers": {
    "agentlog": {
      "command": "/Users/ruttanshbhatelia/side_projects/Plugin/.venv/bin/python",
      "args": ["-m", "agentlog.mcp_server"],
      "env": {
        "AGENTLOG_DB": "/Users/ruttanshbhatelia/.agentlog/agentlog.db"
      }
    }
  }
}
```

## Tools

| Tool | Purpose |
|------|---------|
| `search_sessions` | FTS/metadata search; session ids + compact metadata |
| `get_session` | Session detail; messages truncated (default 200 chars) |
| `usage_stats` | Counts/durations by harness, model, day, or repo |
| `attention_inbox` | Attention Inbox from `analysis.attention` |
| `skill_inventory` | Skills with exposure counts |
| `agreement_and_extraction_status` | Extraction coverage and label distributions |
