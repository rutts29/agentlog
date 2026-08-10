"""Resolve raw source model strings into canonical identity fields.

Keeps the raw string for provenance. Never invents a model when the source
value is a provider name, placeholder, or agent/profile without a declared
base model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentlog.registry.models import (
    AGENT_PROFILES,
    ALIAS_TO_CANONICAL,
    MODELS_BY_ID,
    PLACEHOLDERS,
    PROVIDERS,
)

UNKNOWN_MODEL_LABEL = "(unknown)"

# Effort / mode tails are stored separately; strip longest-first.
_EFFORT_MODE_SUFFIXES: tuple[str, ...] = (
    "-thinking-high",
    "-thinking-medium",
    "-thinking-low",
    "-high-fast",
    "-medium-fast",
    "-low-fast",
    "-thinking",
    "-xhigh",
    "-ultra",
    "-medium",
    "-high",
    "-fast",
    "-low",
    "-max",
)

_WS_RE = re.compile(r"[_\s]+")


@dataclass(frozen=True)
class ModelIdentity:
    raw: str | None
    canonical: str | None
    provider: str | None
    agent_profile: str | None
    family: str | None


def display_model(canonical: str | None) -> str:
    if canonical is None or not str(canonical).strip():
        return UNKNOWN_MODEL_LABEL
    return str(canonical)


def sql_coalesce_model(column: str = "model_canonical") -> str:
    """SQL expression that surfaces unknown honestly for UI payloads."""
    return f"COALESCE(NULLIF({column}, ''), '{UNKNOWN_MODEL_LABEL}')"


def _normalize_key(raw: str) -> str:
    text = raw.strip().lower()
    text = _WS_RE.sub("-", text)
    return text


def normalize_model_key(raw: str) -> str:
    """Public lookup-key form of a raw model string."""
    return _normalize_key(raw)


def _strip_cursor_prefix(key: str) -> str:
    if key.startswith("cursor-"):
        return key[len("cursor-") :]
    return key


def _strip_effort_suffixes(key: str) -> str:
    changed = True
    while changed:
        changed = False
        for suffix in _EFFORT_MODE_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                key = key[: -len(suffix)]
                changed = True
                break
    return key


def _candidates(key: str) -> list[str]:
    """Ordered lookup keys from most specific to stripped forms."""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            out.append(value)

    add(key)
    stripped = _strip_effort_suffixes(key)
    add(stripped)
    no_cursor = _strip_cursor_prefix(key)
    add(no_cursor)
    add(_strip_effort_suffixes(no_cursor))
    return out


def resolve_model_identity(
    raw: str | None,
    *,
    provider_hint: str | None = None,
    agent_profile_hint: str | None = None,
) -> ModelIdentity:
    if raw is None:
        return ModelIdentity(
            raw=None,
            canonical=None,
            provider=_clean_hint(provider_hint),
            agent_profile=_clean_hint(agent_profile_hint),
            family=None,
        )
    source = str(raw).strip()
    if not source:
        return ModelIdentity(
            raw=None,
            canonical=None,
            provider=_clean_hint(provider_hint),
            agent_profile=_clean_hint(agent_profile_hint),
            family=None,
        )

    key = _normalize_key(source)
    provider = _clean_hint(provider_hint)
    agent_profile = _clean_hint(agent_profile_hint)

    if key in PLACEHOLDERS:
        return ModelIdentity(
            raw=source,
            canonical=None,
            provider=provider,
            agent_profile=agent_profile,
            family=None,
        )

    if key in PROVIDERS:
        return ModelIdentity(
            raw=source,
            canonical=None,
            provider=provider or key,
            agent_profile=agent_profile,
            family=None,
        )

    if key in AGENT_PROFILES:
        base = AGENT_PROFILES[key]
        # Model-field profile identity wins over a weaker role hint.
        agent_profile = source
        if base is None:
            return ModelIdentity(
                raw=source,
                canonical=None,
                provider=provider,
                agent_profile=agent_profile,
                family=None,
            )
        record = MODELS_BY_ID.get(base)
        return ModelIdentity(
            raw=source,
            canonical=base,
            provider=provider or (record["provider"] if record else None),
            agent_profile=agent_profile,
            family=record.get("family") if record else None,
        )

    for candidate in _candidates(key):
        if candidate in AGENT_PROFILES:
            return resolve_model_identity(
                candidate,
                provider_hint=provider,
                agent_profile_hint=agent_profile or source,
            )
        canonical = ALIAS_TO_CANONICAL.get(candidate)
        if canonical is None:
            continue
        record = MODELS_BY_ID[canonical]
        return ModelIdentity(
            raw=source,
            canonical=canonical,
            provider=provider or record.get("provider"),
            agent_profile=agent_profile,
            family=record.get("family"),
        )

    # Unregistered non-blocked slug: keep as provisional canonical so new
    # models surface immediately. Still never promote known non-models.
    provisional = _strip_effort_suffixes(_strip_cursor_prefix(key))
    if (
        not provisional
        or provisional in PROVIDERS
        or provisional in PLACEHOLDERS
        or provisional in AGENT_PROFILES
    ):
        return ModelIdentity(
            raw=source,
            canonical=None,
            provider=provider,
            agent_profile=agent_profile or (
                source if key in AGENT_PROFILES else None
            ),
            family=None,
        )
    return ModelIdentity(
        raw=source,
        canonical=provisional,
        provider=provider,
        agent_profile=agent_profile,
        family=None,
    )


def _clean_hint(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_known_non_model(raw: str | None) -> bool:
    """True when raw is a provider, placeholder, or agent profile identity."""
    if raw is None:
        return False
    key = _normalize_key(str(raw))
    return key in PROVIDERS or key in PLACEHOLDERS or key in AGENT_PROFILES


def backfill_model_identity(conn) -> None:
    """Resolve identity columns from existing raw ``model`` values.

    Safe to re-run. Used by migration v014 and by tests that insert rows
    with raw SQL.
    """
    for table in ("sessions", "messages"):
        rows = conn.execute(f"SELECT id, model FROM {table}").fetchall()
        for row in rows:
            ident = resolve_model_identity(row["model"])
            conn.execute(
                f"""
                UPDATE {table}
                SET model_canonical = ?,
                    provider = COALESCE(provider, ?),
                    agent_profile = COALESCE(agent_profile, ?)
                WHERE id = ?
                """,
                (
                    ident.canonical,
                    ident.provider,
                    ident.agent_profile,
                    row["id"],
                ),
            )
    try:
        rows = conn.execute("SELECT id, model FROM token_usage").fetchall()
    except Exception:
        return
    for row in rows:
        ident = resolve_model_identity(row["model"])
        conn.execute(
            "UPDATE token_usage SET model_canonical = ? WHERE id = ?",
            (ident.canonical, row["id"]),
        )


def repair_null_model_identity(
    conn,
    *,
    batch_size: int = 500,
    tables: tuple[str, ...] = ("sessions", "messages"),
    include_token_usage: bool = True,
) -> int:
    """Fill identity only where ``model_canonical`` is still null.

    Bounded batches with commits so concurrent WAL writers are not blocked
    for long. Returns the number of session+message+usage rows updated.
    """
    import sqlite3
    import time

    def _retry(fn, *, attempts: int = 8) -> None:
        last: BaseException | None = None
        for i in range(attempts):
            try:
                fn()
                return
            except sqlite3.OperationalError as exc:
                last = exc
                msg = str(exc).lower()
                if ("locked" not in msg and "busy" not in msg) or i >= attempts - 1:
                    raise
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                time.sleep(0.05 * (2**i))
        assert last is not None
        raise last

    updated = 0
    for table in tables:
        rows = list(
            conn.execute(
                f"""
                SELECT id, model FROM {table}
                WHERE model_canonical IS NULL
                   OR TRIM(COALESCE(model_canonical, '')) = ''
                """
            )
        )
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]

            def _apply(batch=chunk, tbl=table) -> None:
                nonlocal updated
                for row in batch:
                    ident = resolve_model_identity(row["model"])
                    conn.execute(
                        f"""
                        UPDATE {tbl}
                        SET model_canonical = ?,
                            provider = COALESCE(provider, ?),
                            agent_profile = COALESCE(agent_profile, ?)
                        WHERE id = ?
                        """,
                        (
                            ident.canonical,
                            ident.provider,
                            ident.agent_profile,
                            row["id"],
                        ),
                    )
                    updated += 1
                conn.commit()

            _retry(_apply)
    if not include_token_usage:
        return updated
    try:
        rows = list(
            conn.execute(
                """
                SELECT id, model FROM token_usage
                WHERE model_canonical IS NULL
                   OR TRIM(COALESCE(model_canonical, '')) = ''
                """
            )
        )
    except Exception:
        return updated
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]

        def _apply_usage(batch=chunk) -> None:
            nonlocal updated
            for row in batch:
                ident = resolve_model_identity(row["model"])
                conn.execute(
                    "UPDATE token_usage SET model_canonical = ? WHERE id = ?",
                    (ident.canonical, row["id"]),
                )
                updated += 1
            conn.commit()

        _retry(_apply_usage)
    return updated
