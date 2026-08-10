from __future__ import annotations

import re
import shlex
from typing import Literal

OperationKind = Literal[
    "verification",
    "artifact_write",
    "read_only",
    "execute_other",
    "unknown",
]

_VERIFICATION_RE = re.compile(
    r"(?<![a-z0-9])(?:pytest|py\.test|unittest|tests?|lint|build|"
    r"typecheck|tsc|cargo\s+test|npm\s+(?:run\s+)?test|go\s+test|"
    r"make\s+(?:test|check))(?![a-z0-9])",
    re.IGNORECASE,
)
_ARTIFACT_RE = re.compile(
    r"(?<![a-z0-9])(?:apply[_ -]?patch|patch[_ -]?apply|patch|"
    r"write|write[_ -]?file|edit|edit[_ -]?file|str[_ -]?replace|"
    r"create[_ -]?file|delete[_ -]?file)(?![a-z0-9])",
    re.IGNORECASE,
)
_READ_ONLY_RE = re.compile(
    r"(?<![a-z0-9])(?:read|cat|rg|grep|ls|find|status|diff|search|"
    r"list|head|tail|pwd)(?![a-z0-9])",
    re.IGNORECASE,
)
_EXECUTE_NAMES = frozenset(
    {"exec_command", "exec", "shell", "bash", "zsh", "sh", "terminal", "command"}
)
_SHELL_SEPARATORS = frozenset({";", "&", "|", "||", "&&"})
_DETAIL_VERIFICATION_COMMANDS = frozenset(
    {"pytest", "py.test", "unittest", "test", "lint", "build", "typecheck", "tsc"}
)
_DETAIL_ARTIFACT_COMMANDS = frozenset(
    {"apply_patch", "patch_apply", "patch", "write", "write_file", "edit", "edit_file", "str_replace", "create_file", "delete_file"}
)
_DETAIL_READ_COMMANDS = frozenset(
    {"cat", "rg", "grep", "ls", "find", "status", "diff", "search", "read", "list", "head", "tail", "pwd"}
)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_target(command: str, args: list[str]) -> str:
    command = command.rsplit("/", 1)[-1]
    if command == "cd":
        return ""
    if command in {"sudo", "env", "time", "command"}:
        remaining = [part for part in args if not part.startswith("-") and not _ASSIGNMENT_RE.match(part)]
        return _command_target(remaining[0], remaining[1:]) if remaining else ""
    if command in {"npx", "uv", "poetry"}:
        for index, part in enumerate(args):
            if part in {"run", "exec"}:
                remaining = [item for item in args[index + 1 :] if not item.startswith("-") and not _ASSIGNMENT_RE.match(item)]
                return _command_target(remaining[0], remaining[1:]) if remaining else ""
        remaining = [part for part in args if not part.startswith("-") and not _ASSIGNMENT_RE.match(part)]
        return _command_target(remaining[0], remaining[1:]) if remaining else ""
    if command.startswith("python") or command == "pypy":
        if len(args) >= 2 and args[0] == "-m":
            return args[1].rsplit("/", 1)[-1]
        return command
    if command in {"cargo", "go"} and args and args[0] == "test":
        return "test"
    if command in {"npm", "yarn", "pnpm"}:
        remaining = [part for part in args if part not in {"run", "exec"} and not part.startswith("-")]
        return remaining[0] if remaining else ""
    if command == "make":
        remaining = [part for part in args if not part.startswith("-")]
        return remaining[0] if remaining else ""
    if command == "git" and args:
        return args[0]
    return command


def _detail_operation_flags(detail: str) -> tuple[bool, bool, bool, bool]:
    if not detail:
        return False, False, False, False
    try:
        lexer = shlex.shlex(detail, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False, False, False, True

    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)

    if any(separator != "&&" for separator in separators):
        return False, False, False, True

    if separators:
        for segment in segments[:-1]:
            parts = [part.strip().lower() for part in segment]
            while parts and _ASSIGNMENT_RE.match(parts[0]):
                parts.pop(0)
            if not parts or parts[0].rsplit("/", 1)[-1] != "cd":
                return False, False, False, True

    verification = artifact = read_only = False
    final_segment = segments[-1]
    parts = [part.strip().lower() for part in final_segment]
    while parts and _ASSIGNMENT_RE.match(parts[0]):
        parts.pop(0)
    if not parts:
        return False, False, False, True
    target = _command_target(parts[0], parts[1:])
    verification = target in _DETAIL_VERIFICATION_COMMANDS
    artifact = target in _DETAIL_ARTIFACT_COMMANDS
    read_only = target in _DETAIL_READ_COMMANDS
    return verification, artifact, read_only, False


def classify_operation(
    tool_name: str | None,
    detail: str | None = None,
    *,
    read_only_hint: bool | None = None,
) -> OperationKind:
    """Classify a tool without retaining command arguments or payload text."""
    if read_only_hint is True:
        return "read_only"
    name = str(tool_name or "").strip().lower().replace(".", " ")
    detail_text = str(detail or "").strip()
    if not name and not detail_text:
        return "unknown"
    detail_verification, detail_artifact, detail_read, detail_unsafe = _detail_operation_flags(
        detail_text
    )
    if detail_unsafe:
        if name in _EXECUTE_NAMES or name.endswith("_command") or name.endswith("_shell"):
            return "execute_other"
        return "unknown"
    name_flags = (
        bool(_VERIFICATION_RE.search(name)),
        bool(_ARTIFACT_RE.search(name)),
        bool(_READ_ONLY_RE.search(name)),
    )
    if sum(name_flags) == 1:
        if name_flags[0]:
            return "verification"
        if name_flags[1]:
            return "artifact_write"
        return "read_only"
    has_verification = name_flags[0] or detail_verification
    has_artifact = name_flags[1] or detail_artifact
    has_read = name_flags[2] or detail_read
    if has_verification and not has_artifact:
        return "verification"
    if has_artifact and not has_verification:
        return "artifact_write"
    if has_read and not has_verification and not has_artifact:
        return "read_only"
    if name in _EXECUTE_NAMES or name.endswith("_command") or name.endswith("_shell"):
        return "execute_other"
    return "unknown"
