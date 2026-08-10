"""Allowlist for every filesystem write agentlog performs.

agentlog observes a harness; it never drives one. No code path may create,
modify, or delete a file in the owner's agent configuration — AGENTS.md,
CLAUDE.md, .cursor/rules, SKILL.md, hooks.json, plugin manifests, or anything
else inside a harness home. Recommended changes are surfaced as reviewable
proposals; the owner applies them by hand.

Writes are permitted only under agentlog's own working roots. Harness config
files are refused even when they happen to sit inside one of those roots, so a
proposal target can never be written by accident.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from agentlog.config import DEFAULT_DB_PATH

HOME = Path.home()

AGENTLOG_DATA_DIR = DEFAULT_DB_PATH.parent
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
TEMP_DIR = Path(tempfile.gettempdir())

HARNESS_CONFIG_NAMES = frozenset(
    {
        "AGENTS.md",
        "AGENT.md",
        "CLAUDE.md",
        "GEMINI.md",
        "SKILL.md",
        "RULE.md",
        "hooks.json",
        "mcp.json",
        ".mcp.json",
        "plugin.json",
        "marketplace.json",
        "settings.json",
        "settings.local.json",
        "config.toml",
    }
)

HARNESS_CONFIG_SUFFIXES = frozenset({".mdc"})

HARNESS_HOME_DIRS = (
    HOME / ".claude",
    HOME / ".codex",
    HOME / ".cursor",
    HOME / ".agents",
    HOME / ".gemini",
    HOME / ".hermes",
    HOME / "Library" / "Application Support" / "Cursor",
    HOME / "Library" / "Application Support" / "dev.warp.Warp-Stable",
)


class WriteGuardViolation(PermissionError):
    """Raised when a write target sits outside agentlog's own working roots."""


def _project_root() -> Path | None:
    """Repo checkout root, when agentlog runs from source rather than a wheel."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "pyproject.toml").is_file() else None


def allowed_roots() -> tuple[Path, ...]:
    """Roots agentlog may write inside, most specific intent first."""
    roots = [AGENTLOG_DATA_DIR, LAUNCH_AGENTS_DIR, TEMP_DIR]
    project = _project_root()
    if project is not None:
        roots.append(project)
    out: list[Path] = []
    for root in roots:
        try:
            out.append(root.expanduser().resolve())
        except OSError:
            continue
    return tuple(out)


def _resolve(path: Path | str) -> Path:
    """Absolute, symlink-resolved path that tolerates missing components."""
    p = Path(path).expanduser()
    p = Path(os.path.abspath(p))
    tail: list[str] = []
    probe = p
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        tail.append(probe.name)
        probe = parent
    base = probe.resolve() if probe.exists() else probe
    for name in reversed(tail):
        base = base / name
    return base


def is_harness_config(path: Path | str) -> bool:
    """True when ``path`` names an agent configuration surface."""
    resolved = _resolve(path)
    if resolved.name in HARNESS_CONFIG_NAMES:
        return True
    if resolved.suffix.lower() in HARNESS_CONFIG_SUFFIXES:
        return True
    parts = resolved.parts
    for i in range(len(parts) - 1):
        if parts[i] in {".cursor", ".claude", ".codex", ".agents"} and parts[i + 1] in {
            "rules",
            "skills",
            "commands",
            "agents",
            "plugins",
            "hooks",
        }:
            return True
    for root in HARNESS_HOME_DIRS:
        try:
            if resolved.is_relative_to(root.expanduser()):
                return True
        except (OSError, ValueError):
            continue
    return False


def assert_writable(path: Path | str, *, purpose: str = "") -> Path:
    """Return the resolved path, or raise ``WriteGuardViolation``."""
    resolved = _resolve(path)
    context = f" ({purpose})" if purpose else ""
    if is_harness_config(resolved):
        raise WriteGuardViolation(
            f"refusing to write agent configuration file {resolved}{context}. "
            "agentlog proposes changes for review; the owner applies them."
        )
    for root in allowed_roots():
        try:
            if resolved.is_relative_to(root):
                return resolved
        except ValueError:
            continue
    roots = ", ".join(str(r) for r in allowed_roots())
    raise WriteGuardViolation(
        f"refusing to write outside agentlog working roots: {resolved}"
        f"{context}. Allowed roots: {roots}"
    )


def write_text(
    path: Path | str,
    text: str,
    *,
    encoding: str = "utf-8",
    make_parents: bool = True,
) -> Path:
    """Guarded ``Path.write_text``."""
    target = assert_writable(path, purpose="write_text")
    if make_parents:
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding=encoding)
    return target
