"""Shared deterministic proof predicates for coach results and materialization."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

EVIDENCE_MESSAGE = "message"
EVIDENCE_TOOL = "tool"
EVIDENCE_SKILL = "skill"
_TERMINAL_ACTIONS = frozenset({"result", "end"})
_ARTIFACT_ACTIONS = frozenset({"write", "apply", "patch", "commit"})
_DETERMINISTIC_ACTIONS = _TERMINAL_ACTIONS | _ARTIFACT_ACTIONS
_OPERATION_KINDS = frozenset({
    "verification", "artifact_write", "read_only", "execute_other", "unknown",
})
_OWNER_CONFIRMATION = re.compile(r"\b(?:confirm(?:ed)?|yes|correct|works?|working|approved|verified|passes?|passed)\b", re.IGNORECASE)
_OWNER_CORRECTION = re.compile(r"\b(?:no|wrong|failed|failure|still missing|didn['’]?t|did not|not done|broken|incomplete|missing)\b", re.IGNORECASE)
_TARGET_WORD = re.compile(r"[a-z0-9][a-z0-9_./-]*", re.IGNORECASE)
_TARGET_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "can", "check", "do", "for",
    "from", "i", "is", "it", "me", "my", "of", "on", "or", "please", "result",
    "run", "should", "that", "the", "this", "to", "verify", "was", "we", "with",
    "work", "yes", "you", "your",
})
_TARGET_ALIASES = {
    "pytest": "verification",
    "test": "verification", "tests": "verification", "testing": "verification",
    "verify": "verification", "verified": "verification",
}
_ARTIFACT_REQUEST_WORDS = frozenset({
    "add", "apply", "change", "commit", "create", "delete", "edit", "fix",
    "implement", "patch", "refactor", "remove", "update", "write",
})
_ATTRIBUTABLE_SKILL_EXPOSURES = frozenset({
    "attached", "injected", "tool_use", "loaded", "activated", "invoked",
})


def evidence_type(evidence: Mapping[str, Any]) -> str:
    return str(evidence.get("evidence_type") or "")


def _fact_object(evidence: Mapping[str, Any]) -> dict[str, Any]:
    raw = evidence.get("fact")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _tool_fact(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return _fact_object(evidence) if evidence_type(evidence) == EVIDENCE_TOOL else {}


def _operation_kind(fact: Mapping[str, Any]) -> str:
    value = str(fact.get("operation_kind") or "unknown").strip().lower()
    return value if value in _OPERATION_KINDS else "unknown"


def is_successful_tool_result(evidence: Mapping[str, Any]) -> bool:
    fact = _tool_fact(evidence)
    action = str(fact.get("action") or "").strip().lower()
    operation_kind = _operation_kind(fact)
    return (
        evidence_type(evidence) == EVIDENCE_TOOL
        and fact.get("success") in (True, 1)
        and action in _TERMINAL_ACTIONS
        and operation_kind in {"verification", "artifact_write"}
    )


def is_failed_tool_result(evidence: Mapping[str, Any]) -> bool:
    fact = _tool_fact(evidence)
    action = str(fact.get("action") or "").strip().lower()
    operation_kind = _operation_kind(fact)
    return (
        evidence_type(evidence) == EVIDENCE_TOOL
        and fact.get("success") in (False, 0)
        and action in _DETERMINISTIC_ACTIONS
        and operation_kind in {"verification", "artifact_write"}
    )


def is_successful_artifact_result(evidence: Mapping[str, Any]) -> bool:
    fact = _tool_fact(evidence)
    action = str(fact.get("action") or "").strip().lower()
    return (
        evidence_type(evidence) == EVIDENCE_TOOL
        and fact.get("success") in (True, 1)
        and action in _DETERMINISTIC_ACTIONS
        and _operation_kind(fact) == "artifact_write"
    )


def is_verification_result(evidence: Mapping[str, Any]) -> bool:
    fact = _tool_fact(evidence)
    return is_successful_tool_result(evidence) and _operation_kind(fact) == "verification"


def _normalized_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sequence(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def is_causally_later(
    later: Mapping[str, Any],
    earlier: Mapping[str, Any],
    window_order: Mapping[str, int],
) -> bool:
    later_timestamp = _normalized_utc_timestamp(later.get("timestamp"))
    earlier_timestamp = _normalized_utc_timestamp(earlier.get("timestamp"))
    if later_timestamp is not None and earlier_timestamp is not None:
        if later_timestamp != earlier_timestamp:
            return later_timestamp > earlier_timestamp
        later_session = str(later.get("session_id") or "")
        earlier_session = str(earlier.get("session_id") or "")
        later_seq = _sequence(later.get("seq"))
        earlier_seq = _sequence(earlier.get("seq"))
        return (
            bool(later_session)
            and later_session == earlier_session
            and later_seq is not None
            and earlier_seq is not None
            and later_seq > earlier_seq
        )
    return window_order.get(str(later.get("window_id") or ""), -1) > window_order.get(
        str(earlier.get("window_id") or ""), -1
    )


def is_later_owner_evidence(
    evidence: Mapping[str, Any],
    request_window_ids: set[str],
    window_order: Mapping[str, int],
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    requests = [request for request in request_evidence if isinstance(request, Mapping)]
    return (
        evidence_type(evidence) == EVIDENCE_MESSAGE
        and evidence.get("role") == "user"
        and bool(_OWNER_CONFIRMATION.search(str(evidence.get("quote") or "")))
        and not bool(_OWNER_CORRECTION.search(str(evidence.get("quote") or "")))
        and any(
            is_causally_later(evidence, request, window_order)
            for request in requests
            if str(request.get("window_id") or "") in request_window_ids
        )
        and _owner_evidence_links_request(evidence, requests)
    )


def is_later_owner_correction(
    evidence: Mapping[str, Any],
    request_window_ids: set[str],
    window_order: Mapping[str, int],
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    requests = [request for request in request_evidence if isinstance(request, Mapping)]
    return (
        evidence_type(evidence) == EVIDENCE_MESSAGE
        and evidence.get("role") == "user"
        and bool(_OWNER_CORRECTION.search(str(evidence.get("quote") or "")))
        and any(
            is_causally_later(evidence, request, window_order)
            for request in requests
            if str(request.get("window_id") or "") in request_window_ids
        )
        and _owner_evidence_links_request(evidence, requests)
    )


def _normalized_terms(text: str) -> set[str]:
    return {
        normalized
        for raw_token in _TARGET_WORD.findall(text.lower())
        for token in [raw_token.rstrip(".,;:!?")]
        if len(token) >= 4
        for normalized in [_TARGET_ALIASES.get(token, token)]
        if normalized not in _TARGET_STOP_WORDS
    }


def _target_terms(evidence: Mapping[str, Any]) -> set[str]:
    return _normalized_terms(str(evidence.get("quote") or ""))


def _tool_target_terms(evidence: Mapping[str, Any]) -> set[str]:
    fact = _tool_fact(evidence)
    text = str(fact.get("tool_name") or "")
    return _normalized_terms(text)


def _requested_operation_kinds(evidence: Mapping[str, Any]) -> set[str]:
    terms = _target_terms(evidence)
    kinds: set[str] = set()
    if "verification" in terms:
        kinds.add("verification")
    raw_words = {token.rstrip(".,;:!?") for token in _TARGET_WORD.findall(str(evidence.get("quote") or "").lower())}
    if raw_words & _ARTIFACT_REQUEST_WORDS:
        kinds.add("artifact_write")
    return kinds


def deterministic_result_links_request(
    evidence: Mapping[str, Any],
    request_evidence: Iterable[Mapping[str, Any]],
) -> bool:
    if evidence_type(evidence) != EVIDENCE_TOOL:
        return False
    tool_terms = _tool_target_terms(evidence)
    for request in request_evidence:
        if not isinstance(request, Mapping):
            continue
        if tool_terms and tool_terms & _target_terms(request):
            return True
        if result_uses_category_attribution(evidence, (request,)):
            return True
    return False


def result_uses_category_attribution(
    evidence: Mapping[str, Any],
    request_evidence: Iterable[Mapping[str, Any]],
) -> bool:
    if evidence_type(evidence) != EVIDENCE_TOOL:
        return False
    requests = [request for request in request_evidence if isinstance(request, Mapping)]
    tool_terms = _tool_target_terms(evidence)
    if tool_terms and any(tool_terms & _target_terms(request) for request in requests):
        return False
    operation_kind = _operation_kind(_tool_fact(evidence))
    return any(
        str(evidence.get("window_id") or "") == str(request.get("window_id") or "")
        and operation_kind in _requested_operation_kinds(request)
        for request in requests
    )


def _owner_evidence_links_request(
    evidence: Mapping[str, Any],
    request_evidence: Iterable[Mapping[str, Any]],
) -> bool:
    response_terms = _target_terms(evidence)
    for request in request_evidence:
        if not isinstance(request, Mapping):
            continue
        if len(response_terms & _target_terms(request)) >= 2:
            return True
    return False


def supports_successful_result(
    evidence: Iterable[Mapping[str, Any]],
    *,
    request_window_ids: set[str],
    window_order: Mapping[str, int],
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    items = list(evidence)
    requested_kinds = set().union(
        *(_requested_operation_kinds(item) for item in request_evidence if isinstance(item, Mapping))
    )
    successful_kinds = {
        _operation_kind(_tool_fact(item))
        for item in items
        if is_successful_tool_result(item) and deterministic_result_links_request(item, request_evidence)
    }
    owner_kinds = set().union(
        *(
            _requested_operation_kinds(item)
            for item in items
            if is_later_owner_evidence(item, request_window_ids, window_order, request_evidence)
        )
    )
    proven_kinds = successful_kinds | owner_kinds
    return bool(proven_kinds) and requested_kinds <= proven_kinds


def supports_bounded_gap(
    evidence: Iterable[Mapping[str, Any]],
    *,
    request_window_ids: set[str],
    window_order: Mapping[str, int],
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    items = list(evidence)
    requested_kinds = set().union(
        *(_requested_operation_kinds(item) for item in request_evidence if isinstance(item, Mapping))
    )
    failed_kinds = {
        _operation_kind(_tool_fact(item))
        for item in items
        if is_failed_tool_result(item) and deterministic_result_links_request(item, request_evidence)
    }
    owner_kinds = set().union(
        *(
            _requested_operation_kinds(item)
            for item in items
            if is_later_owner_correction(item, request_window_ids, window_order, request_evidence)
        )
    )
    proven_kinds = failed_kinds | owner_kinds
    return bool(proven_kinds) and requested_kinds <= proven_kinds


def supports_skill_action(
    evidence: Iterable[Mapping[str, Any]],
    *,
    skill_evidence: Iterable[Mapping[str, Any]] = (),
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    skills = [item for item in skill_evidence if evidence_type(item) == EVIDENCE_SKILL]
    requests = [item for item in request_evidence if evidence_type(item) == EVIDENCE_MESSAGE and item.get("role") == "user"]
    for action in evidence:
        if not is_successful_tool_result(action):
            continue
        action_fact = _tool_fact(action)
        action_kind = _operation_kind(action_fact)
        action_window = str(action.get("window_id") or "")
        action_message = str(action_fact.get("message_id") or action.get("message_id") or "")
        for skill in skills:
            skill_fact = _fact_object(skill)
            skill_window = str(skill.get("window_id") or "")
            skill_message = str(skill_fact.get("message_id") or skill.get("message_id") or "")
            skill_name_terms = _normalized_terms(str(skill_fact.get("skill_name") or ""))
            exposure_type = str(skill_fact.get("exposure_type") or "").strip().lower().replace("-", "_")
            action_seq = action_fact.get("message_seq", action.get("message_seq"))
            skill_seq = skill_fact.get("message_seq", skill.get("message_seq"))
            ordered = (
                isinstance(action_seq, int)
                and not isinstance(action_seq, bool)
                and isinstance(skill_seq, int)
                and not isinstance(skill_seq, bool)
                and action_seq >= skill_seq
            ) or (
                action_seq is None
                and skill_seq is None
                and action_message == skill_message
            )
            if (
                not action_window or action_window != skill_window or not action_message
                or not ordered or exposure_type not in _ATTRIBUTABLE_SKILL_EXPOSURES
            ):
                continue
            if any(
                str(request.get("window_id") or "") == action_window
                and skill_name_terms & _target_terms(request)
                and action_kind in _requested_operation_kinds(request)
                for request in requests
            ):
                return True
    return False


is_successful_terminal_result = is_successful_tool_result
supports_owner_confirmation = is_later_owner_evidence
supports_owner_correction = is_later_owner_correction


def supports_verification_result(
    evidence: Iterable[Mapping[str, Any]],
    *,
    request_window_ids: set[str],
    window_order: Mapping[str, int],
    request_evidence: Iterable[Mapping[str, Any]] = (),
) -> bool:
    return any(
        is_verification_result(item)
        and deterministic_result_links_request(item, request_evidence)
        or is_later_owner_evidence(item, request_window_ids, window_order, request_evidence)
        for item in evidence
    )


is_later_owner_confirmation = is_later_owner_evidence


__all__ = [
    "EVIDENCE_MESSAGE", "EVIDENCE_TOOL", "EVIDENCE_SKILL", "evidence_type",
    "is_successful_tool_result", "is_successful_terminal_result", "is_failed_tool_result", "is_later_owner_evidence",
    "is_later_owner_confirmation", "supports_owner_confirmation", "is_causally_later",
    "is_later_owner_correction", "supports_owner_correction", "is_successful_artifact_result", "is_verification_result", "supports_verification_result",
    "supports_successful_result", "supports_bounded_gap", "supports_skill_action",
    "deterministic_result_links_request", "result_uses_category_attribution",
]
