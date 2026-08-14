"""Local API token stored under ``~/.agentlog`` (mode 0600).

Closes the residual where any process on the machine can call the loopback
dashboard. The browser never types the token: the served SPA (or the Vite
dev proxy) carries it. MCP stays on stdio and never uses this HTTP token.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from agentlog.api.security import generate_token
from agentlog.config import DEFAULT_DB_PATH
from agentlog.safety.write_guard import assert_writable

TOKEN_FILENAME = "api_token"


def default_token_path(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home) / ".agentlog" / TOKEN_FILENAME
    return DEFAULT_DB_PATH.parent / TOKEN_FILENAME


def _chmod_private(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def read_token_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def write_token_file(path: Path, token: str) -> Path:
    """Write ``token`` with owner-only permissions (0600)."""
    path = Path(path)
    assert_writable(path, purpose="api token").parent.mkdir(
        parents=True, exist_ok=True
    )
    # Write then chmod so a umask-wide create never stays world-readable.
    path.write_text(token.strip() + "\n", encoding="utf-8")
    _chmod_private(path)
    return path


def ensure_token_file(
    path: Path | None = None,
    *,
    rotate: bool = False,
) -> tuple[str, Path, bool]:
    """Return ``(token, path, created_or_rotated)``.

    Creates a new token when the file is missing or ``rotate`` is true.
    """
    target = Path(path) if path is not None else default_token_path()
    existing = None if rotate else read_token_file(target)
    if existing is not None:
        # Heal permissions if an older create left them too open.
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                _chmod_private(target)
        except OSError:
            pass
        return existing, target, False
    token = generate_token()
    write_token_file(target, token)
    return token, target, True


@dataclass(frozen=True)
class ServeToken:
    token: str
    path: Path | None
    source: str  # cli | env | file | generated


def resolve_serve_token(
    *,
    cli_token: str | None = None,
    env_token: str | None = None,
    rotate: bool = False,
    token_path: Path | None = None,
) -> ServeToken:
    """Resolve the token used by ``agentlog serve``.

    Precedence: ``--token`` > ``AGENTLOG_API_TOKEN`` > on-disk file (auto-create).
    Serve always ends with a concrete token so loopback is not world-readable
    to every local process.
    """
    if cli_token and cli_token.strip():
        return ServeToken(token=cli_token.strip(), path=None, source="cli")
    if env_token and env_token.strip():
        return ServeToken(token=env_token.strip(), path=None, source="env")
    token, path, created = ensure_token_file(token_path, rotate=rotate)
    return ServeToken(
        token=token,
        path=path,
        source="generated" if created else "file",
    )
