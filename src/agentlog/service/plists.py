"""launchd plist templates for agentlog background services."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


def _plist_header() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
    )


def _plist_footer() -> str:
    return "</dict>\n</plist>\n"


def _kv_string(key: str, value: str, indent: str = "  ") -> str:
    return f"{indent}<key>{escape(key)}</key>\n{indent}<string>{escape(value)}</string>\n"


def _kv_bool(key: str, value: bool, indent: str = "  ") -> str:
    tag = "true" if value else "false"
    return f"{indent}<key>{escape(key)}</key>\n{indent}<{tag}/>\n"


def _kv_integer(key: str, value: int, indent: str = "  ") -> str:
    return f"{indent}<key>{escape(key)}</key>\n{indent}<integer>{value}</integer>\n"


def render_daemon_plist(
    *,
    label: str,
    python: Path,
    module_args: list[str],
    working_directory: Path,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str],
    nice: int = 10,
) -> str:
    """Render a user LaunchAgent plist for a background Python daemon."""
    parts = [_plist_header()]
    parts.append(_kv_string("Label", label))
    parts.append("  <key>ProgramArguments</key>\n  <array>\n")
    parts.append(f"    <string>{escape(str(python))}</string>\n")
    for arg in module_args:
        parts.append(f"    <string>{escape(arg)}</string>\n")
    parts.append("  </array>\n")
    parts.append(_kv_string("WorkingDirectory", str(working_directory)))
    parts.append("  <key>EnvironmentVariables</key>\n  <dict>\n")
    for key, value in sorted(env.items()):
        parts.append(f"    <key>{escape(key)}</key>\n")
        parts.append(f"    <string>{escape(value)}</string>\n")
    parts.append("  </dict>\n")
    parts.append(_kv_bool("RunAtLoad", True))
    parts.append(_kv_bool("KeepAlive", True))
    parts.append(_kv_string("ProcessType", "Background"))
    parts.append(_kv_integer("Nice", nice))
    parts.append(_kv_string("StandardOutPath", str(stdout_path)))
    parts.append(_kv_string("StandardErrorPath", str(stderr_path)))
    parts.append(_plist_footer())
    return "".join(parts)
