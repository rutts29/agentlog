"""Compatibility exports for shared coach locator redaction."""

from __future__ import annotations

from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text

COACH_REDACTION_VERSION = REDACTION_VERSION


def redact_locator_text(text: str, report: RedactionReport | None = None) -> str:
    return redact_text(text, report)


__all__ = ["COACH_REDACTION_VERSION", "redact_locator_text"]
