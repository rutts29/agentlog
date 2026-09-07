"""Local API token stored under ``~/.agentlog`` (mode 0600).

Closes the residual where any process on the machine can call the loopback
dashboard. The browser never types the token: the served SPA (or the Vite
dev proxy) carries it. MCP stays on stdio and never uses this HTTP token.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentlog.api.security import generate_token
from agentlog.config import DEFAULT_DB_PATH, ensure_db_parent
from agentlog.safety.write_guard import assert_writable

TOKEN_FILENAME = "api_token"


def default_token_path(home: Path | None = None) -> Path:
    if home is not None:
        return Path(home) / ".agentlog" / TOKEN_FILENAME
    return DEFAULT_DB_PATH.parent / TOKEN_FILENAME


def _chmod_private(path: Path) -> None:
    if os.name != "posix":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("API token must be a regular file")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def read_token_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def write_token_file(path: Path, token: str) -> Path:
    """Write ``token`` with owner-only permissions (0600)."""
    path = Path(path)
    target = assert_writable(path, purpose="api token")
    ensure_db_parent(target)
    fd, temporary = tempfile.mkstemp(prefix=".api-token-", dir=target.parent)
    pending = Path(temporary)
    try:
        # mkstemp creates with 0600, including under a permissive umask.
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o600)
            stream.write(token.strip() + "\n")
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)
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
            if mode != 0o600:
                writable = assert_writable(target, purpose="api token permissions")
                _chmod_private(writable)
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
