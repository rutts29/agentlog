"""Install and manage agentlog LaunchAgents (user domain, no sudo)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlog import __file__ as _agentlog_file
from agentlog.config import (
    DEFAULT_DB_PATH,
    DEFAULT_LOG_DIR,
    SERVICE_API_HOST,
    SERVICE_API_PORT,
)
from agentlog.service.health import build_health
from agentlog.service.plists import render_daemon_plist
from agentlog.safety.write_guard import assert_writable

WATCH_LABEL = "com.agentlog.watch"
API_LABEL = "com.agentlog.api"

_LABELS = (WATCH_LABEL, API_LABEL)


@dataclass(frozen=True)
class ServicePaths:
    project_root: Path
    python: Path
    launch_agents: Path
    log_dir: Path
    db_path: Path

    @property
    def watch_plist(self) -> Path:
        return self.launch_agents / f"{WATCH_LABEL}.plist"

    @property
    def api_plist(self) -> Path:
        return self.launch_agents / f"{API_LABEL}.plist"


def detect_project_root() -> Path:
    """Resolve repo root from the installed package location (…/src/agentlog)."""
    return Path(_agentlog_file).resolve().parents[2]


def detect_venv_python(project_root: Path | None = None) -> Path:
    """Absolute path to the venv interpreter without resolving the symlink target.

    ``Path.resolve()`` follows ``.venv/bin/python`` into Homebrew's framework
    binary, which has no editable install / site-packages for agentlog.
    """
    root = project_root or detect_project_root()
    return Path(os.path.abspath(str(root / ".venv" / "bin" / "python")))


def default_paths(
    *,
    project_root: Path | None = None,
    db_path: Path | None = None,
) -> ServicePaths:
    root = (project_root or detect_project_root()).resolve()
    return ServicePaths(
        project_root=root,
        python=detect_venv_python(root),
        launch_agents=(Path.home() / "Library" / "LaunchAgents").resolve(),
        log_dir=DEFAULT_LOG_DIR.expanduser().resolve(),
        db_path=Path(db_path or DEFAULT_DB_PATH).expanduser().resolve(),
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target(label: str) -> str:
    return f"{_domain()}/{label}"


def _run_launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _ensure_dirs(paths: ServicePaths) -> None:
    paths.launch_agents.mkdir(parents=True, exist_ok=True)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)


def render_watch_plist(paths: ServicePaths) -> str:
    log_file = paths.log_dir / "watch.log"
    return render_daemon_plist(
        label=WATCH_LABEL,
        python=paths.python,
        module_args=["-m", "agentlog.watch", "--db", str(paths.db_path)],
        working_directory=paths.project_root,
        stdout_path=paths.log_dir / "watch.stdout.log",
        stderr_path=paths.log_dir / "watch.stderr.log",
        env={
            "AGENTLOG_LOG_FILE": str(log_file),
            "PYTHONUNBUFFERED": "1",
        },
    )


def render_api_plist(paths: ServicePaths) -> str:
    log_file = paths.log_dir / "api.log"
    return render_daemon_plist(
        label=API_LABEL,
        python=paths.python,
        module_args=[
            "-m",
            "agentlog",
            "--db",
            str(paths.db_path),
            "serve",
            "--host",
            SERVICE_API_HOST,
            "--port",
            str(SERVICE_API_PORT),
        ],
        working_directory=paths.project_root,
        stdout_path=paths.log_dir / "api.stdout.log",
        stderr_path=paths.log_dir / "api.stderr.log",
        env={
            "AGENTLOG_LOG_FILE": str(log_file),
            "PYTHONUNBUFFERED": "1",
        },
    )


def _write_plist(path: Path, contents: str) -> None:
    assert_writable(path, purpose="launchd plist").write_text(
        contents, encoding="utf-8"
    )


def _bootout(label: str) -> None:
    _run_launchctl(["bootout", _target(label)])


def _bootstrap(plist: Path) -> subprocess.CompletedProcess[str]:
    return _run_launchctl(["bootstrap", _domain(), str(plist)])


def _kickstart(label: str) -> subprocess.CompletedProcess[str]:
    return _run_launchctl(["kickstart", "-k", _target(label)])


def _enable(label: str) -> None:
    _run_launchctl(["enable", _target(label)])


def install_services(
    *,
    project_root: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    paths = default_paths(project_root=project_root, db_path=db_path)
    if not paths.python.is_file():
        raise FileNotFoundError(
            f"venv python not found at {paths.python}; create .venv first"
        )
    _ensure_dirs(paths)
    watch_body = render_watch_plist(paths)
    api_body = render_api_plist(paths)
    _write_plist(paths.watch_plist, watch_body)
    _write_plist(paths.api_plist, api_body)

    errors: list[str] = []
    for label, plist in (
        (WATCH_LABEL, paths.watch_plist),
        (API_LABEL, paths.api_plist),
    ):
        _bootout(label)
        time.sleep(1.0)
        _enable(label)
        result = _bootstrap(plist)
        if result.returncode != 0:
            combined = (result.stderr or "") + (result.stdout or "")
            if "already bootstrapped" not in combined.lower():
                # Retry — launchd can return transient I/O errors after bootout.
                for delay in (1.0, 2.0):
                    time.sleep(delay)
                    result = _bootstrap(plist)
                    combined = (result.stderr or "") + (result.stdout or "")
                    if result.returncode == 0 or "already bootstrapped" in combined.lower():
                        break
                else:
                    errors.append(f"{label}: bootstrap failed: {combined.strip()}")
                    continue
        kick = _kickstart(label)
        if kick.returncode != 0:
            errors.append(
                f"{label}: kickstart failed: {(kick.stderr or kick.stdout).strip()}"
            )

    return {
        "project_root": str(paths.project_root),
        "python": str(paths.python),
        "db": str(paths.db_path),
        "log_dir": str(paths.log_dir),
        "plists": {
            WATCH_LABEL: str(paths.watch_plist),
            API_LABEL: str(paths.api_plist),
        },
        "errors": errors,
    }


def uninstall_services() -> dict[str, Any]:
    paths = default_paths()
    removed: list[str] = []
    for label, plist in (
        (WATCH_LABEL, paths.watch_plist),
        (API_LABEL, paths.api_plist),
    ):
        _bootout(label)
        if plist.is_file():
            plist.unlink()
            removed.append(str(plist))
    return {"removed": removed}


def start_services(*, labels: tuple[str, ...] = _LABELS) -> dict[str, Any]:
    paths = default_paths()
    plist_for = {
        WATCH_LABEL: paths.watch_plist,
        API_LABEL: paths.api_plist,
    }
    started: list[str] = []
    errors: list[str] = []
    for label in labels:
        plist = plist_for[label]
        if not plist.is_file():
            errors.append(f"{label}: plist missing at {plist}; run service install")
            continue
        if not _is_loaded(label):
            boot = _bootstrap(plist)
            if boot.returncode != 0:
                combined = ((boot.stderr or "") + (boot.stdout or "")).strip()
                if "already bootstrapped" not in combined.lower():
                    errors.append(f"{label}: bootstrap failed: {combined}")
                    continue
        result = _kickstart(label)
        if result.returncode == 0:
            started.append(label)
        else:
            errors.append(
                f"{label}: {(result.stderr or result.stdout or 'kickstart failed').strip()}"
            )
    return {"started": started, "errors": errors}


def stop_services(*, labels: tuple[str, ...] = _LABELS) -> dict[str, Any]:
    stopped: list[str] = []
    for label in labels:
        _run_launchctl(["kill", "SIGTERM", _target(label)])
        # kill alone may restart via KeepAlive; bootout stops until start/install.
        _bootout(label)
        stopped.append(label)
    return {"stopped": stopped}


def _parse_launchctl_print(text: str) -> dict[str, Any]:
    pid: int | None = None
    last_exit: int | None = None
    state: str | None = None
    m = re.search(r"^\s*pid\s*=\s*(\d+)", text, re.MULTILINE)
    if m:
        pid = int(m.group(1))
    m = re.search(r"^\s*last exit code\s*=\s*\(?(\d+|unknown)", text, re.MULTILINE | re.I)
    if m and m.group(1).isdigit():
        last_exit = int(m.group(1))
    m = re.search(r"^\s*state\s*=\s*(\w+)", text, re.MULTILINE)
    if m:
        state = m.group(1)
    # Fallback: launchctl list style "PID\tStatus\tLabel"
    if pid is None:
        m = re.search(r"^(-|\d+)\s+(-?\d+)\s+\S+\s*$", text.strip(), re.MULTILINE)
        if m:
            pid = None if m.group(1) == "-" else int(m.group(1))
            last_exit = int(m.group(2))
    return {"pid": pid, "last_exit_status": last_exit, "state": state}


def _is_loaded(label: str) -> bool:
    printed = _run_launchctl(["print", _target(label)])
    if printed.returncode == 0:
        return True
    listed = _run_launchctl(["list", label])
    return listed.returncode == 0 and bool((listed.stdout or "").strip())


def inspect_label(label: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "label": label,
        "loaded": False,
        "pid": None,
        "last_exit_status": None,
        "state": None,
    }
    printed = _run_launchctl(["print", _target(label)])
    if printed.returncode == 0:
        info["loaded"] = True
        info.update(_parse_launchctl_print(printed.stdout or ""))
        return info
    listed = _run_launchctl(["list", label])
    if listed.returncode == 0 and (listed.stdout or "").strip():
        info["loaded"] = True
        info.update(_parse_launchctl_print(listed.stdout or ""))
    return info


def service_status(
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    paths = default_paths(db_path=db_path)
    health = build_health(paths.db_path)
    services: dict[str, Any] = {}
    for label in _LABELS:
        row = inspect_label(label)
        if label == WATCH_LABEL:
            row["log_path"] = str(paths.log_dir / "watch.log")
            row["stdout_path"] = str(paths.log_dir / "watch.stdout.log")
            row["last_ingest_at"] = health.get("last_ingest_at")
            row["presence_fresh"] = health["watcher"]["presence_fresh"]
            row["presence_age_seconds"] = health["watcher"]["presence_age_seconds"]
            row["watcher_alive"] = health["watcher"]["alive"]
        else:
            row["log_path"] = str(paths.log_dir / "api.log")
            row["stdout_path"] = str(paths.log_dir / "api.stdout.log")
            row["port"] = SERVICE_API_PORT
        row["plist"] = str(
            paths.watch_plist if label == WATCH_LABEL else paths.api_plist
        )
        services[label] = row
    return {
        "services": services,
        "health": health,
        "log_dir": str(paths.log_dir),
        "db": str(paths.db_path),
    }
