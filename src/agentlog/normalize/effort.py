from __future__ import annotations

CANONICAL_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "ultra", "max", "unknown"}
)

# Source aliases → canonical. Identity for every value already in the set.
_EFFORT_ALIASES: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "med": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "x-high": "xhigh",
    "extra_high": "xhigh",
    "extrahigh": "xhigh",
    "ultra": "ultra",
    "max": "max",
    "unknown": "unknown",
}


def normalize_effort(raw: str | None) -> tuple[str | None, str | None]:
    """Return (canonical_effort, effort_source).

    Unknown non-empty source values become canonical ``unknown`` while the
    raw string is retained in ``effort_source``. ``None``/empty stay ``None``.
    """
    if raw is None:
        return None, None
    source = str(raw).strip()
    if not source:
        return None, None
    key = source.lower()
    canonical = _EFFORT_ALIASES.get(key)
    if canonical is None:
        return "unknown", source
    return canonical, source
