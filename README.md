# agentlog

Local-first tool for analyzing AI coding agent transcripts (Claude Code, Codex CLI, Cursor).

## Install

```bash
uv pip install -e .
# or
pip install -e .
```

## Commands

```bash
agentlog ingest
agentlog stats
agentlog sessions [--harness X] [--since DATE]
agentlog session show <id>
agentlog search <query>
```

Data is stored in `~/.agentlog/agentlog.db` by default.
