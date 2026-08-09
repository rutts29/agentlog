from __future__ import annotations

from typing import Any

from agentlog.db.repository import Repository


def compute_stats(repo: Repository) -> dict[str, Any]:
    return repo.stats()
