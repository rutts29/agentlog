"""Declarative model registry: canonical ids, providers, aliases, families.

Effort / thinking / speed suffixes are not part of model identity — those
belong in ``sessions.effort`` / ``messages.effort``. Cursor-prefixed slugs
resolve to the underlying model id.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ModelRecord(TypedDict, total=False):
    id: str
    provider: str
    family: str
    aliases: list[str]


# Provider names that have been misfiled into model fields.
PROVIDERS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "xai",
        "google",
        "azure",
        "aws",
        "bedrock",
        "cursor",
        "nous",
    }
)

# Placeholders that are not models.
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "<synthetic>",
        "synthetic",
        "default",
        "auto",
        "unknown",
        "none",
        "null",
    }
)

# Agent / profile identities. optional base_model when the profile clearly
# wraps a known model; otherwise canonical stays unknown.
AGENT_PROFILES: dict[str, str | None] = {
    "codex-auto-review": None,
    "grok-4.5-build": "grok-4.5",
}

MODELS: list[ModelRecord] = [
    # OpenAI / Codex
    {"id": "gpt-5.5", "provider": "openai", "family": "gpt-5", "aliases": []},
    {
        "id": "gpt-5.6-sol",
        "provider": "openai",
        "family": "gpt-5.6",
        "aliases": ["gpt-5.6-sol-high"],
    },
    {
        "id": "gpt-5.6-terra",
        "provider": "openai",
        "family": "gpt-5.6",
        "aliases": [],
    },
    {
        "id": "gpt-5.6-luna",
        "provider": "openai",
        "family": "gpt-5.6",
        "aliases": [],
    },
    {"id": "gpt-5.4", "provider": "openai", "family": "gpt-5", "aliases": []},
    {
        "id": "gpt-5.4-mini",
        "provider": "openai",
        "family": "gpt-5",
        "aliases": [],
    },
    {
        "id": "gpt-5.3-codex-spark",
        "provider": "openai",
        "family": "gpt-5",
        "aliases": [],
    },
    # xAI / Grok
    {
        "id": "grok-4.5",
        "provider": "xai",
        "family": "grok-4.5",
        "aliases": [
            "cursor-grok-4.5",
            "cursor-grok-4.5-high",
            "cursor-grok-4.5-high-fast",
            "cursor-grok-4.5-fast",
        ],
    },
    # Anthropic / Claude
    {
        "id": "claude-opus-5",
        "provider": "anthropic",
        "family": "claude-opus",
        "aliases": [
            "claude-opus-5-thinking-high",
            "claude-opus-5-thinking",
        ],
    },
    {
        "id": "claude-fable-5",
        "provider": "anthropic",
        "family": "claude-fable",
        "aliases": [
            "claude-fable-5-thinking-high",
            "claude-fable-5-thinking",
        ],
    },
    {
        "id": "claude-opus-4-7",
        "provider": "anthropic",
        "family": "claude-opus",
        "aliases": [],
    },
    {
        "id": "claude-opus-4-5",
        "provider": "anthropic",
        "family": "claude-opus",
        "aliases": [],
    },
    # Others observed in corpus
    {
        "id": "kimi-k3-max",
        "provider": "moonshot",
        "family": "kimi",
        "aliases": [],
    },
    {"id": "glm-5.2", "provider": "zhipu", "family": "glm", "aliases": []},
    {
        "id": "composer-2.5",
        "provider": "cursor",
        "family": "composer",
        "aliases": [],
    },
]


def _index_models() -> tuple[dict[str, ModelRecord], dict[str, str]]:
    by_id: dict[str, ModelRecord] = {}
    alias_to_id: dict[str, str] = {}
    for row in MODELS:
        mid = row["id"]
        by_id[mid] = row
        alias_to_id[mid] = mid
        for alias in row.get("aliases") or []:
            alias_to_id[alias] = mid
    for profile, base in AGENT_PROFILES.items():
        if base and base in by_id and profile not in alias_to_id:
            # Profile may resolve to a base model, but remains an agent_profile.
            pass
    return by_id, alias_to_id


MODELS_BY_ID, ALIAS_TO_CANONICAL = _index_models()


def list_models() -> list[dict[str, Any]]:
    return [dict(m) for m in MODELS]


def get_model(model_id: str) -> ModelRecord | None:
    return MODELS_BY_ID.get(model_id)
