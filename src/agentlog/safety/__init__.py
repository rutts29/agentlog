"""Structural guarantees that keep agentlog advisory-only."""

from __future__ import annotations

from agentlog.safety.egress import (
    ACKNOWLEDGEMENT,
    EGRESS_DISCLOSURE,
    EgressBlocked,
    assert_egress_allowed,
    disable_remote_extraction,
    enable_remote_extraction,
    remote_extraction,
    remote_extraction_enabled,
)
from agentlog.safety.redaction import (
    REDACTION_VERSION,
    RedactionReport,
    redact_payload,
    redact_payloads,
    redact_text,
)
from agentlog.safety.write_guard import (
    WriteGuardViolation,
    allowed_roots,
    assert_writable,
    is_harness_config,
    write_text,
)

__all__ = [
    "ACKNOWLEDGEMENT",
    "EGRESS_DISCLOSURE",
    "EgressBlocked",
    "REDACTION_VERSION",
    "RedactionReport",
    "WriteGuardViolation",
    "allowed_roots",
    "assert_egress_allowed",
    "assert_writable",
    "disable_remote_extraction",
    "enable_remote_extraction",
    "is_harness_config",
    "redact_payload",
    "redact_payloads",
    "redact_text",
    "remote_extraction",
    "remote_extraction_enabled",
    "write_text",
]
