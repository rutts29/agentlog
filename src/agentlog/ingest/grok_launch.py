"""Conservative, text-free evidence extraction for non-interactive Grok launches."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

from agentlog.ingest.base import content_hash_text


_PROMPT_FLAGS = {"--prompt", "-p", "--query", "--message", "--single"}
_MODEL_FLAGS = {"--model", "-m"}
_CWD_FLAGS = {"--cwd", "-C"}
_SHELL_CONTROL = re.compile(r"[;&|<>`]|\$\(")
_JS_CMD = re.compile(r"(?:^|[,{\s])(?:cmd|command)\s*:\s*")


@dataclass(frozen=True)
class GrokLaunch:
    prompt_hash: str
    requested_model: str | None
    cwd: str | None


def _string_command(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        command = decoded.get("cmd") or decoded.get("command")
        return [command] if isinstance(command, str) else []
    return [value]


def _commands(payload: dict[str, Any]) -> list[str]:
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        command = arguments.get("cmd") or arguments.get("command")
        return [command] if isinstance(command, str) else []
    if isinstance(arguments, str):
        found = _string_command(arguments)
        if found and found != [arguments]:
            return found
    for key in ("cmd", "command"):
        value = payload.get(key)
        if isinstance(value, str):
            return [value]
    # T3/Codex custom exec records a JavaScript tool body. Decode only a literal
    # JSON-compatible cmd string; dynamic scripts are intentionally ignored.
    value = payload.get("input")
    if not isinstance(value, str):
        return []
    direct = _string_command(value)
    if direct and direct != [value]:
        return direct
    commands: list[str] = []
    for match in _JS_CMD.finditer(value):
        remainder = value[match.end() :].lstrip()
        if not remainder.startswith('"'):
            continue
        try:
            command, _ = json.JSONDecoder().raw_decode(remainder)
        except json.JSONDecodeError:
            continue
        if isinstance(command, str):
            commands.append(command)
    return commands


def _launch_from_command(command: str, *, cwd: str | None) -> GrokLaunch | None:
    if _SHELL_CONTROL.search(command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = tokens[0].rsplit("/", 1)[-1].casefold()
    if executable not in {"grok", "grok-cli"}:
        return None
    prompt_values: list[str] = []
    model: str | None = None
    command_cwd = cwd
    i = 1
    while i < len(tokens):
        token = tokens[i]
        key, sep, value = token.partition("=")
        if key in _PROMPT_FLAGS:
            if sep:
                prompt_values.append(value)
            elif i + 1 < len(tokens):
                i += 1
                prompt_values.append(tokens[i])
            else:
                return None
        elif key in _MODEL_FLAGS:
            if sep:
                model = value or None
            elif i + 1 < len(tokens):
                i += 1
                model = tokens[i] or None
            else:
                return None
        elif key in _CWD_FLAGS:
            if sep:
                command_cwd = value or None
            elif i + 1 < len(tokens):
                i += 1
                command_cwd = tokens[i] or None
            else:
                return None
        i += 1
    if len(prompt_values) != 1 or not prompt_values[0].strip():
        return None
    return GrokLaunch(
        prompt_hash=content_hash_text(prompt_values[0].strip()),
        requested_model=model,
        cwd=command_cwd,
    )


def completed_grok_launch(payload: dict[str, Any], *, cwd: str | None) -> GrokLaunch | None:
    """Return a launch only when exactly one static non-interactive CLI call exists."""
    candidates = [
        launch
        for command in _commands(payload)
        if (launch := _launch_from_command(command, cwd=cwd)) is not None
    ]
    return candidates[0] if len(candidates) == 1 else None
