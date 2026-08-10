"""User-level launchd service management for agentlog daemons."""

from __future__ import annotations

from agentlog.service.health import build_health
from agentlog.service.launchd import (
    API_LABEL,
    WATCH_LABEL,
    install_services,
    service_status,
    start_services,
    stop_services,
    uninstall_services,
)

__all__ = [
    "API_LABEL",
    "WATCH_LABEL",
    "build_health",
    "install_services",
    "service_status",
    "start_services",
    "stop_services",
    "uninstall_services",
]
