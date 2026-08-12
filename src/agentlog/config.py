from __future__ import annotations

from pathlib import Path

PARSER_VERSION = "15"

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
WARP_SQLITE = (
    HOME
    / "Library"
    / "Application Support"
    / "dev.warp.Warp-Stable"
    / "warp.sqlite"
)
HERMES_HOME = HOME / ".hermes"
HERMES_STATE_DB = HERMES_HOME / "state.db"
HERMES_KANBAN_DB = HERMES_HOME / "kanban.db"

# t3 code ships as a Homebrew cask on a nightly auto-update channel, so the
# state root is treated as a candidate list rather than one fixed path.
T3CODE_HOME_CANDIDATES = (
    HOME / ".t3",
    HOME / ".t3code",
    HOME / ".config" / "t3",
    HOME / ".config" / "t3code",
    HOME / "Library" / "Application Support" / "t3code",
)
T3CODE_STATE_DB_GLOBS = (
    "userdata/state.sqlite",
    "state.sqlite",
)
T3CODE_HOME = HOME / ".t3"
T3CODE_STATE_DB = T3CODE_HOME / "userdata" / "state.sqlite"
T3CODE_CACHES_DIR = T3CODE_HOME / "caches"

WATCH_DEBOUNCE_SECONDS = 30.0
WATCH_MAX_WAIT_SECONDS = 120.0
WATCH_POLL_SECONDS = 60.0

# Live presence: transcript mtime within this window ⇒ session is "active".
PRESENCE_ACTIVE_SECONDS = 90.0
PRESENCE_HEARTBEAT_SECONDS = 15.0
# Harnesses only flush transcripts at turn boundaries, so an agent grinding
# through a long tool call writes nothing for minutes. When the last record on
# disk is an unanswered tool call / mid-turn assistant text, keep the session
# live for this longer window instead of dropping it at PRESENCE_ACTIVE_SECONDS.
PRESENCE_WORKING_GRACE_SECONDS = 1800.0
# Widest mtime window the live scanner considers at all.
PRESENCE_SCAN_WINDOW_SECONDS = 1800.0
# Scan result reuse window; keeps a 1–2s frontend poll off the filesystem.
PRESENCE_SCAN_CACHE_SECONDS = 1.0
DEFAULT_PRESENCE_PATH = HOME / ".agentlog" / "presence.json"
DEFAULT_LOG_DIR = HOME / ".agentlog" / "logs"
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 5

# launchd-managed dashboard API (fixed port for bookmarks / clients)
SERVICE_API_HOST = "127.0.0.1"
SERVICE_API_PORT = 8787

# Optional override for the dashboard API bearer token. When unset, ``serve``
# auto-loads or creates ``~/.agentlog/api_token`` (mode 0600) so loopback is
# not readable by every local process. Non-loopback binds still require a token
# (file, env, or --token) plus --allow-remote-access.
API_TOKEN_ENV_VAR = "AGENTLOG_API_TOKEN"
API_TOKEN_FILENAME = "api_token"

# Presence heartbeat every 15s; treat watcher as dead after 3 missed beats.
WATCHER_PRESENCE_STALE_SECONDS = PRESENCE_HEARTBEAT_SECONDS * 3


def ensure_db_parent(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def presence_path_for_db(db_path: Path | None = None) -> Path:
    """Presence state file lives beside the agentlog database."""
    root = Path(db_path or DEFAULT_DB_PATH).expanduser().resolve().parent
    return root / "presence.json"
