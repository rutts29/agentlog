from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentlog import config


@dataclass(frozen=True)
class WatchSource:
    harness: str
    path: Path
    # SQLite / WAL-backed sources prefer mtime polling as a reliability backstop.
    poll: bool = False


def default_sources() -> list[WatchSource]:
    """Authoritative watch roots from config (mirrors adapter discover roots)."""
    sources: list[WatchSource] = [
        WatchSource("codex", config.CODEX_SESSIONS_DIR, poll=False),
        WatchSource("claude", config.CLAUDE_PROJECTS_DIR, poll=False),
        WatchSource("grok", config.GROK_SESSIONS_DIR, poll=False),
        WatchSource("cursor", config.CURSOR_PROJECTS_DIR, poll=False),
        WatchSource("cursor", config.CURSOR_STATE_VSCDB, poll=True),
        WatchSource("warp", config.WARP_SQLITE, poll=True),
        WatchSource("hermes", config.HERMES_STATE_DB, poll=True),
        WatchSource("hermes", config.HERMES_KANBAN_DB, poll=True),
        WatchSource("hermes", config.HERMES_HOME / "kanban" / "boards", poll=True),
    ]
    sources.extend(t3code_sources())
    return sources


def t3code_sources() -> list[WatchSource]:
    """One source per discovered t3 code state DB, plus its parent dir.

    The cask auto-updates nightly, so roots are globbed rather than fixed and
    an absent install simply yields nothing.
    """
    from agentlog.ingest.t3code import discover_t3code_dbs

    out: list[WatchSource] = []
    seen: set[Path] = set()
    for db_path in discover_t3code_dbs():
        if db_path not in seen:
            seen.add(db_path)
            out.append(WatchSource("t3code", db_path, poll=True))
        parent = db_path.parent
        if parent not in seen:
            seen.add(parent)
            out.append(WatchSource("t3code", parent, poll=True))
    if not out:
        for root in config.T3CODE_HOME_CANDIDATES:
            out.append(WatchSource("t3code", root / "userdata", poll=True))
    return out


def existing_watch_roots(sources: list[WatchSource] | None = None) -> list[WatchSource]:
    """Return sources whose path exists (file or directory)."""
    out: list[WatchSource] = []
    for src in sources if sources is not None else default_sources():
        if src.path.exists():
            out.append(src)
    return out
