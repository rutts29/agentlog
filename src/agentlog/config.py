from __future__ import annotations

from pathlib import Path

PARSER_VERSION = "5"

HOME = Path.home()
DEFAULT_DB_PATH = HOME / ".agentlog" / "agentlog.db"

CODEX_SESSIONS_DIR = HOME / ".codex" / "sessions"
CLAUDE_PROJECTS_DIR = HOME / ".claude" / "projects"
CURSOR_PROJECTS_DIR = HOME / ".cursor" / "projects"
CURSOR_STATE_VSCDB = (
    HOME
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)


def ensure_db_parent(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
