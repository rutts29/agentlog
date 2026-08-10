"""Size-based rotating logs for long-running daemons."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from agentlog.config import LOG_BACKUP_COUNT, LOG_MAX_BYTES


def ensure_log_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def configure_daemon_logging(
    log_file: Path | None = None,
    *,
    verbose: bool = False,
    also_stderr: bool = False,
) -> Path | None:
    """Configure root logging. Returns the active log file path, if any."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    dest: Path | None = None
    if log_file is not None:
        dest = ensure_log_dir(log_file)
        handler: logging.Handler = RotatingFileHandler(
            dest,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
        if also_stderr:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(fmt)
            root.addHandler(stream)
    else:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    logging.getLogger("watchdog").setLevel(logging.WARNING)
    return dest


def log_file_from_env() -> Path | None:
    raw = os.environ.get("AGENTLOG_LOG_FILE", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()
