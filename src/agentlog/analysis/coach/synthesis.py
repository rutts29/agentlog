"""Run-local semantic synthesis for validated harness-coach observations.

This stage deliberately has no database dependency.  It turns validated
per-root observations into bounded Terra packets, validates Terra's candidate
cards, and writes a reviewable local catalog.  It never promotes a claim or
edits a harness configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agentlog.analysis.coach.preprocess import validate_coach_result
from agentlog.analysis.coach.proof import (
    is_failed_tool_result,
    is_successful_artifact_result,
    is_successful_tool_result,
    result_uses_category_attribution,
    supports_bounded_gap,
    supports_skill_action,
    supports_successful_result,
    supports_verification_result,
)
from agentlog.safety.redaction import REDACTION_VERSION, RedactionReport, redact_text
from agentlog.safety.write_guard import assert_writable, write_text


SYNTHESIS_SCHEMA_VERSION = "coach.synthesis.v1"
TERRA_RESULT_VERSION = "coach.terra.result.v1"
SECOND_REVIEW_VERSION = "coach.second_review.v1"
CATALOG_VERSION = "coach.candidate_catalog.v1"

CANDIDATE_KINDS = (
    "observed_instance",
    "corpus_pattern",
    "coach_proposal",
)
_SENTIMENT_MARKERS = (
    "sentiment", "mood", "emotion", "tone", "feelings", "happy", "angry",
    "frustrated", "pleased", "annoyed", "disappointed",
)
_NEGATIVE_KINDS = frozenset({"instruction_miss", "delivery_gap", "repeated_ask"})
_EVIDENCE_FAMILIES = {
    "instruction_follow": "instruction_compliance",
    "instruction_miss": "instruction_compliance",
    "repeated_ask": "instruction_compliance",
    "skill_use": "skill_execution",
    "delivery_gap": "delivery",
    "verification": "verification",
    "process_fact": "process",
}
_THEME_NOISE = frozenset({
    "before", "completed", "completion", "delivery", "done", "explicit", "follow",
    "gap", "instruction", "miss", "missing", "request", "required", "result", "run", "the",
})
_THEME_ALIASES = {
    "pytest": "verification", "test": "verification", "tests": "verification", "testing": "verification",
    "verify": "verification", "verified": "verification",
}
_EXPLICIT_VERIFICATION_THEME_WORDS = frozenset({
    "pytest", "test", "tests", "testing", "verify", "verified", "verification",
})
_AMBIGUOUS_VERIFICATION_THEME_WORDS = frozenset({
    "check", "checks", "checking", "validate", "validated", "validation",
})
_VERIFICATION_THEME_WORDS = (
    _EXPLICIT_VERIFICATION_THEME_WORDS | _AMBIGUOUS_VERIFICATION_THEME_WORDS
)
_VERIFICATION_THEME_MODIFIERS = _VERIFICATION_THEME_WORDS | frozenset({"finish"})
_NON_VERIFICATION_TARGET_WORDS = frozenset({
    "config", "configuration", "deploy", "deployment", "environment", "infrastructure",
    "release", "rollout",
})
_THEME_TARGET_STOP_WORDS = _THEME_NOISE | frozenset({
    "a", "an", "and", "for", "if", "must", "only", "please", "requested", "root",
    "task", "tasks", "then", "to", "with", "work",
}) | _VERIFICATION_THEME_MODIFIERS
_COMPLETION_TERMS = re.compile(
    r"\b(?:complete(?:d|ion)?|deliver(?:ed|y)?|followed|verified|passed)\b",
    re.IGNORECASE,
)
_BULLET_MARKER = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_KEY_PART = re.compile(r"[^a-z0-9]+")
_SPACE = re.compile(r"\s+")
_SCOPE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,159}$")
_STOP_CONFIG_TOKENS = frozenset(
    {"instruction", "follow", "miss", "skill", "process", "verification", "delivery", "request"}
)
_CARD_BEHAVIOR_WORDS = frozenset({
    "applied", "checked", "completed", "failed", "followed", "missed", "ran",
    "recorded", "repeated", "returned", "skipped", "tested", "updated", "verified",
    "wrote",
})
_CARD_OUTCOME_WORDS = frozenset({
    "completed", "failed", "followed", "missed", "passed", "repeated", "returned",
    "skipped", "succeeded", "verified",
})
_PIPELINE_NARRATION = re.compile(
    r"\b(?:proof arc|observation id|cited root|evidence (?:contains|shows)|model output|packet (?:contains|shows)|validator)\b",
    re.IGNORECASE,
)
_CANONICAL_TERM_ALIASES = {
    "pytest": "verification", "test": "verification", "tests": "verification", "testing": "verification",
    "verified": "verification", "verify": "verification", "verification": "verification",
    "miss": "instruction_miss", "missed": "instruction_miss", "missing": "instruction_miss",
}


TERRA_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["packet_id", "result_id", "producer", "abstain"],
    "properties": {
        "packet_id": {"type": "string"},
        "result_id": {"type": "string"},
        "producer": {"type": "object"},
        "abstain": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "kind", "canonical", "title", "summary", "does_not_prove",
                    "supporting_observation_ids", "counterevidence_observation_ids",
                    "n", "denominator", "processed_roots", "eligible_roots",
                ],
                "properties": {
                    "kind": {"enum": list(CANDIDATE_KINDS)},
                    "canonical": {
                        "type": "object",
                        "required": ["scope", "subject", "predicate", "polarity"],
                    },
                    "instruction_text": {"type": "string"},
                    "pattern_canonical_key": {"type": "string"},
                    "target_ref": {"type": "string"},
                    "target_kind": {"type": "string"},
                    "action": {"enum": ["add"]},
                    "base_content_hash": {"type": "string"},
                    "config_gap": {"type": "object"},
                    "supporting_observation_ids": {"type": "array", "items": {"type": "string"}},
                    "counterevidence_observation_ids": {"type": "array", "items": {"type": "string"}},
                    "population_hash": {"type": "string"},
                    "cited_supporting_roots": {"type": "integer", "minimum": 0},
                    "counterevidence_roots": {"type": "integer", "minimum": 0},
                    "counterevidence_observations": {"type": "integer", "minimum": 0},
                },
            },
        },
    },
}

SECOND_REVIEW_RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["catalog_id", "review_id", "producer", "decisions"],
    "properties": {
        "catalog_id": {"type": "string"},
        "review_id": {"type": "string"},
        "producer": {"type": "object"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["candidate_id", "canonical_key", "decision", "observation_ids"],
                "properties": {
                    "candidate_id": {"type": "string"},
                    "canonical_key": {"type": "string"},
                    "decision": {"enum": ["accept", "reject"]},
                    "observation_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class SynthesisConfig:
    max_supporting_observations: int = 24
    max_counterexample_observations: int = 8
    max_config_snippets: int = 4
    max_config_snippet_chars: int = 320
    producer_provider: str = "openai"
    producer_model: str = "gpt-5.6-terra"
    producer_worker_id: str = "terra-synthesis"
    producer_assignment_id: str = "terra-synthesis"
    reviewer_provider: str = "openai"
    reviewer_model: str = "gpt-5.6-terra"
    reviewer_worker_id: str = "terra-second-review"
    reviewer_assignment_id: str = "terra-second-review"


@dataclass(frozen=True)
class SynthesisValidationFailure:
    reason: str
    index: int | None = None
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "index": self.index, "candidate_id": self.candidate_id}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _short_hash(value: Any) -> str:
    return _sha256(value)[:24]


def _assignment(
    *,
    role: str,
    provider: str,
    model: str,
    worker_id: str,
    assignment_id: str,
    prompt: str,
) -> dict[str, str]:
    return {
        "role": role,
        "provider": _normalized_text(provider),
        "model": _normalized_text(model),
        "worker_id": _normalized_text(worker_id),
        "assignment_id": _normalized_text(assignment_id),
        "prompt_version": TERRA_RESULT_VERSION if role == "synthesis" else SECOND_REVIEW_VERSION,
        "prompt_hash": _sha256(prompt),
    }


def _assignment_matches(raw: Any, expected: Any) -> bool:
    if not isinstance(raw, Mapping) or not isinstance(expected, Mapping):
        return False
    fields = (
        "role", "provider", "model", "worker_id", "assignment_id",
        "prompt_version", "prompt_hash",
    )
    return all(
        str(expected.get(field) or "")
        and str(raw.get(field) or "") == str(expected.get(field) or "")
        for field in fields
    )


def _normalized_text(value: Any) -> str:
    return _SPACE.sub(" ", str(value or "").strip())


def _timestamp_sort_key(value: Any) -> tuple[int, str]:
    text = _normalized_text(value)
    if not text:
        return 1, ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 1, text
    if parsed.tzinfo is None:
        return 1, text
    return 0, parsed.astimezone(timezone.utc).isoformat()


def _normalized_part(value: Any) -> str:
    return _KEY_PART.sub("_", _normalized_text(value).lower()).strip("_")


def _text_has_sentiment(*values: Any) -> bool:
    text = " ".join(_normalized_text(value).lower() for value in values)
    return any(marker in text for marker in _SENTIMENT_MARKERS)


def _plain_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _normalized_text(value)
    if not text or _BULLET_MARKER.match(text):
        return None
    return text


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _manifest_from(value: Mapping[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    path = Path(value)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("coverage manifest must be a JSON object")
    return loaded


def _coverage(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage manifest missing coverage")
    result = {
        key: coverage.get(key)
        for key in (
            "total", "eligible", "scanned", "selected", "processed", "total_windows",
            "eligible_windows", "scanned_windows", "selected_windows", "processed_windows",
            "total_roots", "eligible_roots", "selected_roots",
            "proof_capability_by_harness", "publication_mode", "publication_complete",
            "source_truncated_messages", "excluded_synthetic_windows", "excluded_synthetic_by_kind",
            "scope_denominators",
        )
        if key in coverage
    }
    denominator = _positive_int(coverage.get("eligible_roots"))
    if denominator is None:
        raise ValueError("coverage manifest missing positive eligible_roots denominator")
    result["full_eligible_root_denominator"] = denominator
    return result, denominator


def _snapshot_hash(snapshot: Mapping[str, Any]) -> str:
    body = dict(snapshot)
    body.pop("snapshot_hash", None)
    return _short_hash(json.dumps(body, sort_keys=True, ensure_ascii=False))


def _corpus_snapshot(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    snapshot = manifest.get("corpus_snapshot")
    declared = str(manifest.get("corpus_snapshot_hash") or "")
    if not isinstance(snapshot, Mapping) or not declared:
        raise ValueError("coverage manifest missing corpus snapshot lineage")
    snapshot_copy = dict(snapshot)
    embedded = str(snapshot_copy.get("snapshot_hash") or "")
    if embedded != declared or _snapshot_hash(snapshot_copy) != declared:
        raise ValueError("coverage manifest corpus snapshot hash mismatch")
    return snapshot_copy, declared


def _synthesis_packet_hash_valid(packet: Mapping[str, Any]) -> bool:
    declared = str(packet.get("packet_hash") or "")
    body = dict(packet)
    body.pop("packet_hash", None)
    return bool(declared) and declared == _sha256(body)


def _preprocess_packet_hash_valid(packet: Mapping[str, Any]) -> bool:
    declared = str(packet.get("packet_hash") or "")
    body = dict(packet)
    body.pop("packet_hash", None)
    expected = _short_hash(json.dumps(body, sort_keys=True, ensure_ascii=False))
    return bool(declared) and declared == expected


def _scope_from_observation(observation: Mapping[str, Any]) -> str:
    direct = observation.get("scope")
    if isinstance(direct, Mapping):
        scope_type = _normalized_part(direct.get("type") or direct.get("scope_type"))
        scope_id = _normalized_part(direct.get("id") or direct.get("scope_id"))
        if scope_type:
            return f"{scope_type}:{scope_id}" if scope_id else scope_type
    if isinstance(direct, str) and _normalized_part(direct):
        return _normalized_part(direct)
    scope_type = _normalized_part(observation.get("scope_type"))
    scope_id = _normalized_part(observation.get("scope_id"))
    if scope_type:
        return f"{scope_type}:{scope_id}" if scope_id else scope_type
    return "corpus"


def _polarity_from_observation(observation: Mapping[str, Any]) -> str:
    polarity = _normalized_part(observation.get("polarity"))
    if polarity in {"positive", "negative", "mixed", "unknown"}:
        return polarity
    return "negative" if str(observation.get("kind") or "") in _NEGATIVE_KINDS else "positive"


def _assertion_from_observation(observation: Mapping[str, Any]) -> str:
    return _normalized_part(observation.get("assertion_key"))


def _evidence_family(observation: Mapping[str, Any]) -> str:
    explicit = _normalized_part(observation.get("evidence_family"))
    if explicit:
        return explicit
    return _EVIDENCE_FAMILIES.get(str(observation.get("kind") or ""), "other")


def _assertion_theme(observation: Mapping[str, Any]) -> str:
    server_theme = _normalized_part(observation.get("server_theme"))
    if server_theme:
        return server_theme
    tokens = [
        _THEME_ALIASES.get(token, token)
        for token in _assertion_from_observation(observation).split("_")
        if token and token not in _THEME_NOISE
    ]
    if "verification" in tokens:
        tokens = [token for token in tokens if token not in {"check", "checks", "checking"}]
    return "_".join(dict.fromkeys(tokens)) or _evidence_family(observation)


def _theme_fact(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    fact = entry.get("fact")
    if isinstance(fact, Mapping):
        return fact
    if isinstance(fact, str):
        try:
            parsed = json.loads(fact)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _arc_refs(observation: Mapping[str, Any], labels: frozenset[str]) -> set[str]:
    return {
        str(ref)
        for arc in observation.get("proof_arcs", [])
        if isinstance(arc, Mapping)
        and _normalized_part(arc.get("arc")) in labels
        and isinstance(arc.get("evidence_refs"), list)
        for ref in arc["evidence_refs"]
    }


def _server_assertion_theme(
    observation: Mapping[str, Any],
    windows: Mapping[str, Mapping[str, Any]],
) -> str:
    kind = str(observation.get("kind") or "")
    evidence_by_ref = {
        str(entry.get("ref") or ""): entry
        for entry in observation.get("evidence", [])
        if isinstance(entry, Mapping) and str(entry.get("ref") or "")
    }
    if kind == "process_fact":
        artifact_refs = _arc_refs(observation, frozenset({"action", "artifact"}))
        operation_kinds = {
            _normalized_part(_theme_fact(evidence_by_ref[ref]).get("operation_kind"))
            for ref in artifact_refs
            if ref in evidence_by_ref
        } - {""}
        return f"process_{next(iter(operation_kinds))}" if len(operation_kinds) == 1 else "process"
    if kind == "skill_use":
        return "skill_execution"
    if kind not in {"instruction_follow", "instruction_miss", "repeated_ask", "delivery_gap", "verification"}:
        return ""
    request_refs = _arc_refs(
        observation,
        frozenset({"request", "expectation", "verification_request", "request_1", "request_2"}),
    )
    request_texts: list[str] = []
    for ref in sorted(request_refs):
        evidence = evidence_by_ref.get(ref)
        if not isinstance(evidence, Mapping) or str(evidence.get("evidence_type") or "") != "message":
            continue
        window = windows.get(str(evidence.get("window_id") or ""))
        if not isinstance(window, Mapping):
            continue
        message_id = str(evidence.get("message_id") or "")
        message = next(
            (
                entry for entry in window.get("messages", [])
                if isinstance(entry, Mapping)
                and str(entry.get("message_id") or "") == message_id
                and str(entry.get("role") or "") == "user"
            ),
            None,
        )
        source_text = str((message or {}).get("source_text") or "")
        if source_text:
            request_texts.append(source_text)
    if not request_texts:
        return ""
    request_tokens = [
        token
        for text in request_texts
        for token in _normalized_part(text).split("_")
        if token and not token.isdigit()
    ]
    request_set = set(request_tokens)
    terminal_refs = _arc_refs(
        observation,
        frozenset({"outcome", "gap", "delivery", "verification_result"}),
    )
    typed_verification = any(
        str(_theme_fact(evidence_by_ref[ref]).get("operation_kind") or "").lower()
        == "verification"
        for ref in terminal_refs
        if ref in evidence_by_ref
    )
    explicit_verification = bool(request_set & _EXPLICIT_VERIFICATION_THEME_WORDS)
    ambiguous_verification = bool(request_set & _AMBIGUOUS_VERIFICATION_THEME_WORDS)
    configuration_target = bool(request_set & _NON_VERIFICATION_TARGET_WORDS)
    if explicit_verification or (
        typed_verification and ambiguous_verification and not configuration_target
    ):
        target = [token for token in request_tokens if token not in _THEME_TARGET_STOP_WORDS]
        return "_".join(["verification", *dict.fromkeys(target)])
    target = [token for token in request_tokens if token not in _THEME_TARGET_STOP_WORDS]
    return "_".join(dict.fromkeys(target)) or f"unclassified_{_short_hash(request_texts)}"


def _packet_evidence_windows(packet: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    windows = {
        str(window.get("window_id") or ""): window
        for window in packet.get("windows", [])
        if isinstance(window, Mapping) and str(window.get("window_id") or "")
    }
    root_index = packet.get("root_request_index")
    if isinstance(root_index, Mapping):
        for root, entries in root_index.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                window_id = str(entry.get("window_id") or "")
                request = entry.get("request")
                if not window_id or not isinstance(request, Mapping):
                    continue
                windows.setdefault(
                    window_id,
                    {
                        "window_id": window_id,
                        "session_id": str(entry.get("session_id") or ""),
                        "root_session_id": str(entry.get("root_session_id") or root),
                        "timestamp": str(entry.get("timestamp") or ""),
                        "messages": [dict(request)],
                        "artifact": {},
                        "context_only": True,
                        "response_model_canonical": str(entry.get("response_model_canonical") or ""),
                        "response_effort": str(entry.get("response_effort") or ""),
                    },
                )
    return windows


def _observation_hash(
    observation: Mapping[str, Any],
    packet: Mapping[str, Any],
    *,
    server_theme: str = "",
) -> str:
    windows = _packet_evidence_windows(packet)
    evidence: list[dict[str, Any]] = []
    for item in observation.get("evidence", []):
        if not isinstance(item, Mapping):
            continue
        window = windows.get(str(item.get("window_id") or ""), {})
        evidence.append(
            {
                "evidence_type": str(item.get("evidence_type") or "message"),
                "window_id": str(item.get("window_id") or ""),
                "session_id": str(item.get("session_id") or window.get("session_id") or ""),
                "root_session_id": str(item.get("root_session_id") or window.get("root_session_id") or ""),
                "message_id": str(item.get("message_id") or ""),
                "tool_event_id": str(item.get("tool_event_id") or ""),
                "skill_exposure_id": str(item.get("skill_exposure_id") or ""),
                "fact": _normalized_text(item.get("fact")),
                "role": str(item.get("role") or ""),
                "seq": item.get("seq"),
                "quote": _normalized_text(item.get("quote")),
                "window_content_hash": str(window.get("content_hash") or ""),
            }
        )
    arcs = [
        {"arc": str(arc.get("arc") or ""), "evidence_refs": sorted(str(ref) for ref in arc.get("evidence_refs", []))}
        for arc in observation.get("proof_arcs", [])
        if isinstance(arc, Mapping)
    ]
    return _sha256(
        {
            "kind": str(observation.get("kind") or ""),
            "assertion_key": _assertion_from_observation(observation),
            "server_theme": server_theme,
            "scope": _scope_from_observation(observation),
            "polarity": _polarity_from_observation(observation),
            "does_not_prove": _normalized_text(observation.get("does_not_prove")),
            "evidence": sorted(evidence, key=_canonical_json),
            "proof_arcs": sorted(arcs, key=_canonical_json),
        }
    )


def _window_messages(window: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages = window.get("messages")
    return [message for message in messages if isinstance(message, Mapping)] if isinstance(messages, list) else []


def _message_sequence(message: Mapping[str, Any]) -> int | None:
    value = message.get("seq")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _message_attribution(message: Mapping[str, Any]) -> tuple[str, str]:
    return (
        _normalized_text(message.get("model_canonical")) or _normalized_text(message.get("model")),
        _normalized_text(message.get("effort")),
    )


def _canonical_response(window: Mapping[str, Any]) -> Mapping[str, Any] | None:
    response = window.get("response")
    if isinstance(response, Mapping):
        return response
    response_id = str(window.get("response_message_id") or "")
    response_seq = _message_sequence({"seq": window.get("response_seq")})
    candidates = [
        message
        for message in _window_messages(window)
        if str(message.get("role") or "") == "assistant"
        and (
            (response_id and str(message.get("message_id") or "") == response_id)
            or (response_seq is not None and _message_sequence(message) == response_seq)
        )
    ]
    if len(candidates) == 1:
        return candidates[0]
    assistants = [
        message for message in _window_messages(window)
        if str(message.get("role") or "") == "assistant"
    ]
    return assistants[0] if len(assistants) == 1 else None


def _window_response_attribution(window: Mapping[str, Any]) -> tuple[str, str]:
    response = _canonical_response(window)
    model, effort = _message_attribution(response) if response is not None else ("", "")
    return (
        model or _normalized_text(window.get("response_model_canonical")),
        effort or _normalized_text(window.get("response_effort")),
    )


def _evidence_fact(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
    fact = evidence.get("fact")
    if isinstance(fact, Mapping):
        return fact
    if not isinstance(fact, str):
        return {}
    try:
        parsed = json.loads(fact)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _assistant_attribution_for_evidence(
    evidence: Mapping[str, Any],
    window: Mapping[str, Any],
    *,
    prefer_latest_assistant: bool = False,
) -> tuple[str, str]:
    evidence_type = str(evidence.get("evidence_type") or "")
    message_id = str(evidence.get("message_id") or "")
    fact = _evidence_fact(evidence)
    if not message_id:
        message_id = str(fact.get("message_id") or "")
    messages = _window_messages(window)
    bound = next(
        (message for message in messages if str(message.get("message_id") or "") == message_id),
        None,
    )
    if evidence_type == "message" and str(evidence.get("role") or "") == "assistant":
        if bound is not None and str(bound.get("role") or "") == "assistant":
            return _message_attribution(bound)
        response = _canonical_response(window)
        if response is not None and str(response.get("message_id") or "") == message_id:
            return _message_attribution(response)
        return "", ""
    if evidence_type not in {"tool", "skill"}:
        if prefer_latest_assistant:
            evidence_seq = _message_sequence(bound) if bound is not None else _message_sequence(evidence)
            later_assistants = [
                message
                for message in messages
                if str(message.get("role") or "") == "assistant"
                and evidence_seq is not None
                and (_message_sequence(message) is not None and _message_sequence(message) > evidence_seq)
            ]
            if later_assistants:
                terminal = max(
                    later_assistants,
                    key=lambda message: (_message_sequence(message) or -1, str(message.get("message_id") or "")),
                )
                return _message_attribution(terminal)
        return _window_response_attribution(window)
    if bound is not None and str(bound.get("role") or "") == "assistant":
        return _message_attribution(bound)
    if not (
        bool(fact.get("message_is_tool_plumbing"))
        or (bound is not None and bool(bound.get("is_tool_plumbing")))
    ):
        return "", ""
    bound_seq = (
        _message_sequence(bound)
        if bound is not None
        else _message_sequence({"seq": fact.get("message_seq")})
    )
    if bound_seq is None:
        return "", ""
    preceding = [
        message
        for message in messages
        if str(message.get("role") or "") == "assistant"
        and (_message_sequence(message) is not None and _message_sequence(message) < bound_seq)
    ]
    if not preceding:
        return "", ""
    terminal = max(
        preceding,
        key=lambda message: (_message_sequence(message) or -1, str(message.get("message_id") or "")),
    )
    return _message_attribution(terminal)


def _terminal_model_attribution(
    observation: Mapping[str, Any],
    windows: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str, list[str], bool]:
    evidence_by_ref = {
        str(item.get("ref") or ""): item
        for item in observation.get("evidence", [])
        if isinstance(item, Mapping) and str(item.get("ref") or "")
    }
    arcs = {
        _normalized_part(arc.get("arc")): [str(ref) for ref in arc.get("evidence_refs", [])]
        for arc in observation.get("proof_arcs", [])
        if isinstance(arc, Mapping)
    }
    def arc_evidence(label: str) -> list[Mapping[str, Any]]:
        return [
            evidence_by_ref[ref]
            for ref in arcs.get(label, [])
            if ref in evidence_by_ref
        ]

    def targets(
        evidence: Iterable[Mapping[str, Any]],
        *,
        prefer_latest_assistant: bool = False,
    ) -> list[tuple[str, Mapping[str, Any], bool]]:
        return [
            (str(item["window_id"]), item, prefer_latest_assistant)
            for item in evidence
            if str(item.get("window_id") or "")
        ]

    kind = str(observation.get("kind") or "")
    target_evidence: list[tuple[str, Mapping[str, Any], bool]]
    if kind in {"instruction_follow", "instruction_miss"}:
        result_label = "outcome" if kind == "instruction_follow" else "gap"
        result_evidence = arc_evidence(result_label)
        terminal_tools = [
            item for item in result_evidence
            if str(item.get("evidence_type") or "") == "tool"
        ]
        if terminal_tools:
            target_evidence = targets(terminal_tools)
        elif result_evidence:
            request_evidence = arc_evidence("request")
            target_evidence = (
                targets(request_evidence, prefer_latest_assistant=True)
                if request_evidence
                else targets(arc_evidence("response"))
            )
        else:
            target_evidence = targets(arc_evidence("response"))
    elif kind == "repeated_ask":
        target_evidence = targets(arc_evidence("request_1"), prefer_latest_assistant=True)
    elif kind in {"delivery_gap", "verification"}:
        result_label = "delivery" if kind == "delivery_gap" else "verification_result"
        result_evidence = arc_evidence(result_label)
        terminal_tools = [
            item for item in result_evidence
            if str(item.get("evidence_type") or "") == "tool"
        ]
        if terminal_tools:
            target_evidence = targets(terminal_tools)
        else:
            request_label = "expectation" if kind == "delivery_gap" else "verification_request"
            target_evidence = targets(arc_evidence(request_label), prefer_latest_assistant=True)
    elif kind == "skill_use":
        target_evidence = targets(arc_evidence("skill_action"))
    elif kind == "process_fact":
        target_evidence = targets([*arc_evidence("artifact"), *arc_evidence("action")])
    else:
        target_evidence = []
    models: set[str] = set()
    efforts: set[str] = set()
    target_windows = {window_id for window_id, _, _ in target_evidence}
    for window_id, evidence, prefer_latest_assistant in target_evidence:
        window = windows.get(window_id)
        if window is None:
            continue
        model, effort = _assistant_attribution_for_evidence(
            evidence,
            window,
            prefer_latest_assistant=prefer_latest_assistant,
        )
        if model:
            models.add(model)
        if effort:
            efforts.add(effort)
    return (
        next(iter(models)) if len(models) == 1 else "",
        next(iter(efforts)) if len(efforts) == 1 else "",
        sorted(target_windows),
        len(models) > 1,
    )


def _record_from_observation(
    observation: Mapping[str, Any],
    packet: Mapping[str, Any],
    result_id: str,
    luna_producer: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, SynthesisValidationFailure | None]:
    windows = _packet_evidence_windows(packet)
    evidence = observation.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return None, SynthesisValidationFailure("validated_observation_missing_evidence")
    evidence_windows = [windows.get(str(item.get("window_id") or "")) for item in evidence if isinstance(item, Mapping)]
    if not evidence_windows or any(window is None for window in evidence_windows):
        return None, SynthesisValidationFailure("validated_observation_unknown_window")
    roots = {str(window.get("root_session_id") or "") for window in evidence_windows if window is not None}
    if len(roots) != 1 or not next(iter(roots), ""):
        return None, SynthesisValidationFailure("validated_observation_not_one_root")
    root = next(iter(roots))
    first = next(
        (window for window in evidence_windows if window is not None and not window.get("context_only")),
        next(window for window in evidence_windows if window is not None),
    )
    response_model, response_effort, terminal_windows, model_ambiguous = _terminal_model_attribution(
        observation, windows
    )
    assertion = _assertion_from_observation(observation)
    if not assertion:
        return None, SynthesisValidationFailure("validated_observation_missing_assertion_key")
    server_theme = _server_assertion_theme(observation, windows) or _evidence_family(observation)
    record_hash = _observation_hash(observation, packet, server_theme=server_theme)
    packet_hash = str(packet.get("packet_hash") or _short_hash(packet))
    artifact_hashes = sorted(
        {
            str((window.get("artifact") or {}).get("artifact_hash") or "")
            for window in evidence_windows
            if isinstance(window, Mapping)
        }
        - {""}
    )
    source_hashes = sorted(
        {
            str(message.get("source_hash") or message.get("content_hash") or "")
            for window in evidence_windows
            if isinstance(window, Mapping)
            for message in window.get("messages", [])
            if isinstance(message, Mapping)
        }
        - {""}
    )
    expanded_evidence: list[dict[str, Any]] = []
    observation_timestamps: list[str] = []
    for item, window in zip((item for item in evidence if isinstance(item, Mapping)), evidence_windows):
        if window is None:
            continue
        expanded = dict(item)
        expanded["session_id"] = str(window.get("session_id") or "")
        expanded["root_session_id"] = str(window.get("root_session_id") or "")
        expanded["context_only"] = bool(window.get("context_only"))
        message_id = str(expanded.get("message_id") or "")
        message = next(
            (
                candidate for candidate in window.get("messages", [])
                if isinstance(candidate, Mapping) and str(candidate.get("message_id") or "") == message_id
            ),
            None,
        )
        timestamp = str((message or {}).get("timestamp") or window.get("timestamp") or "")
        expanded["timestamp"] = timestamp
        if timestamp:
            observation_timestamps.append(timestamp)
        expanded_evidence.append(expanded)
    return {
        "observation_id": f"obs_{record_hash[:24]}",
        "exact_hash": record_hash,
        "kind": str(observation.get("kind") or ""),
        "assertion_key": assertion,
        "server_theme": server_theme,
        "evidence_family": _evidence_family(observation),
        "scope": _scope_from_observation(observation),
        "polarity": _polarity_from_observation(observation),
        "confidence": observation.get("confidence"),
        "does_not_prove": _normalized_text(observation.get("does_not_prove")),
        "evidence": expanded_evidence,
        "proof_arcs": [dict(item) for item in observation.get("proof_arcs", []) if isinstance(item, Mapping)],
        "root_session_id": root,
        "observed_at_start": min(observation_timestamps) if observation_timestamps else "",
        "observed_at_end": max(observation_timestamps) if observation_timestamps else "",
        "harness": _normalized_text(first.get("harness")) or "(unknown)",
        "repo": _normalized_text(first.get("repo")) or "(unknown)",
        "model_attribution": {
            "response_model": response_model,
            "response_effort": response_effort,
            "terminal_window_ids": terminal_windows,
            "ambiguous": model_ambiguous,
            "descriptive_only": True,
        },
        "packet_id": str(packet.get("packet_id") or ""),
        "result_id": result_id,
        "luna_producer": dict(luna_producer or {}),
        "provenance": {
            "packet_hash": packet_hash,
            "source_hashes": source_hashes,
            "artifact_hashes": artifact_hashes,
            "redaction_version": str((packet.get("redaction") or {}).get("redaction_version") or REDACTION_VERSION),
        },
    }, None


def _result_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, Mapping):
        return []
    for key in ("results", "validated_results"):
        items = value.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return [dict(value)]


def _packet_map(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    packets: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("packets", []):
        if not isinstance(entry, Mapping):
            continue
        packet_id = str(entry.get("packet_id") or "")
        relative = str(entry.get("path") or "")
        declared_hash = str(entry.get("packet_hash") or "")
        declared_windows = sorted(str(value) for value in entry.get("window_ids", []) if value)
        declared_roots = sorted(str(value) for value in entry.get("root_session_ids", []) if value)
        if not packet_id or not relative or not declared_hash:
            continue
        path = run_dir / relative
        if not path.is_file():
            continue
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if (
            isinstance(loaded, dict)
            and str(loaded.get("packet_id") or "") == packet_id
            and _preprocess_packet_hash_valid(loaded)
            and str(loaded.get("packet_hash") or "") == declared_hash
            and sorted(str(item.get("window_id") or "") for item in loaded.get("windows", []) if isinstance(item, Mapping)) == declared_windows
            and sorted(str(value) for value in loaded.get("root_session_ids", []) if value) == declared_roots
        ):
            packets[packet_id] = loaded
    return packets


def luna_result_paths(run_dir: Path | str) -> list[Path]:
    """Return only legacy-root or explicitly Luna-scoped preprocess results."""
    root = Path(run_dir) / "results"
    if not root.is_dir():
        return []
    legacy = [path for path in root.glob("*.json") if path.is_file()]
    staged = root / "luna"
    nested = (
        [path for path in staged.glob("**/*.json") if path.is_file()]
        if staged.is_dir()
        else []
    )
    return sorted({*legacy, *nested})


def load_validated_observation_records(
    run_dir: Path | str,
    *,
    manifest: Mapping[str, Any] | Path | str | None = None,
    result_paths: Iterable[Path | str] | None = None,
) -> tuple[list[dict[str, Any]], list[SynthesisValidationFailure]]:
    """Load only results that pass the preprocessing validator again.

    Result files are read from legacy ``results/*.json`` and scoped
    ``results/luna/**/*.json`` locations when paths are omitted.
    Re-validating at this boundary prevents synthesis from trusting a stale or
    hand-edited record merely because it has a familiar JSON shape.
    """
    root = Path(run_dir)
    source_manifest = _manifest_from(manifest) if manifest is not None else _manifest_from(root / "manifest.json")
    packets = _packet_map(root, source_manifest)
    paths = [Path(path) for path in result_paths] if result_paths is not None else luna_result_paths(root)
    records: list[dict[str, Any]] = []
    failures: list[SynthesisValidationFailure] = []
    authoritative_results: set[str] = set()
    for path in paths:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append(SynthesisValidationFailure("invalid_result_json"))
            continue
        for raw in _result_objects(parsed):
            packet_id = str(raw.get("packet_id") or "")
            packet = packets.get(packet_id)
            if packet is None:
                failures.append(SynthesisValidationFailure("result_packet_not_in_manifest"))
                continue
            if packet_id in authoritative_results:
                failures.append(SynthesisValidationFailure("multiple_results_for_preprocess_packet"))
                continue
            cleaned, result_failures = validate_coach_result(raw, packet)
            if result_failures or cleaned is None:
                failures.extend(SynthesisValidationFailure(f"preprocess_validation:{failure.reason}") for failure in result_failures)
                continue
            authoritative_results.add(packet_id)
            if cleaned.get("abstain"):
                continue
            for observation in cleaned.get("observations", []):
                record, failure = _record_from_observation(
                    observation,
                    packet,
                    str(cleaned["result_id"]),
                    cleaned.get("producer") if isinstance(cleaned.get("producer"), Mapping) else None,
                )
                if failure is not None:
                    failures.append(failure)
                elif record is not None:
                    records.append(record)
    return exact_deduplicate_observations(records), failures


load_validated_observations = load_validated_observation_records


def _processed_window_scopes(window: Mapping[str, Any]) -> set[str]:
    scopes = {"global"}
    for prefix, value in (
        ("harness", window.get("harness")),
        ("repo", window.get("repo")),
    ):
        component = _scope_component(value)
        if component:
            scopes.add(f"{prefix}_{component}")
    messages = window.get("message_timeline")
    if not isinstance(messages, list):
        messages = window.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, Mapping) or str(message.get("role") or "") != "assistant":
            continue
        component = _scope_component(message.get("model_canonical"))
        if component:
            scopes.add(f"model_{component}")
    return scopes


def summarize_result_processing_coverage(
    run_dir: Path | str,
    *,
    manifest: Mapping[str, Any] | Path | str | None = None,
    result_paths: Iterable[Path | str] | None = None,
) -> tuple[dict[str, Any], list[SynthesisValidationFailure]]:
    """Count only preprocess packets whose one authoritative result validates."""
    root = Path(run_dir)
    source_manifest = _manifest_from(manifest) if manifest is not None else _manifest_from(root / "manifest.json")
    packets = _packet_map(root, source_manifest)
    paths = [Path(path) for path in result_paths] if result_paths is not None else luna_result_paths(root)
    failures: list[SynthesisValidationFailure] = []
    authoritative: set[str] = set()
    valid_packets: set[str] = set()
    processed_windows: set[str] = set()
    processed_roots: set[str] = set()
    processed_roots_by_harness: dict[str, set[str]] = {}
    processed_scope_windows: dict[str, set[str]] = {}
    processed_scope_roots: dict[str, set[str]] = {}
    abstained = 0
    valid_observations = 0
    for path in paths:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append(SynthesisValidationFailure("invalid_result_json")); continue
        for raw in _result_objects(parsed):
            packet_id = str(raw.get("packet_id") or "")
            packet = packets.get(packet_id)
            if packet is None:
                failures.append(SynthesisValidationFailure("result_packet_not_in_manifest")); continue
            if packet_id in authoritative:
                failures.append(SynthesisValidationFailure("multiple_results_for_preprocess_packet")); continue
            authoritative.add(packet_id)
            cleaned, result_failures = validate_coach_result(raw, packet)
            if result_failures or cleaned is None:
                failures.extend(SynthesisValidationFailure(f"preprocess_validation:{failure.reason}") for failure in result_failures)
                continue
            valid_packets.add(packet_id)
            local_windows = {
                str(window.get("window_id") or ""): window
                for window in packet.get("windows", [])
                if isinstance(window, Mapping) and str(window.get("window_id") or "")
            }
            for disposition in cleaned.get("window_dispositions", []):
                if not isinstance(disposition, Mapping):
                    continue
                window_id = str(disposition.get("window_id") or "")
                window = local_windows.get(window_id)
                if window is None:
                    continue
                root_id = str(window.get("root_session_id") or "")
                harness = str(window.get("harness") or "(unknown)")
                processed_windows.add(window_id)
                for scope in _processed_window_scopes(window):
                    processed_scope_windows.setdefault(scope, set()).add(window_id)
                    if root_id:
                        processed_scope_roots.setdefault(scope, set()).add(root_id)
                if root_id:
                    processed_roots.add(root_id)
                    processed_roots_by_harness.setdefault(harness, set()).add(root_id)
            if cleaned.get("abstain"):
                abstained += 1
            else:
                valid_observations += len(cleaned.get("observations", []))
    return {
        "processed_packets": len(valid_packets),
        "processed_windows": len(processed_windows),
        "processed_roots": len(processed_roots),
        "abstained_packets": abstained,
        "valid_observations": valid_observations,
        "processed_roots_by_harness": {
            harness: len(roots) for harness, roots in sorted(processed_roots_by_harness.items())
        },
        "scope_counts": {
            scope: {
                "processed_roots": len(processed_scope_roots.get(scope, set())),
                "processed_windows": len(windows),
            }
            for scope, windows in sorted(processed_scope_windows.items())
        },
    }, failures


def exact_deduplicate_observations(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Collapse only byte-for-byte semantic evidence duplicates, never roots."""
    unique: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        exact_hash = str(record.get("exact_hash") or _sha256(record))
        record["exact_hash"] = exact_hash
        record.setdefault("observation_id", f"obs_{exact_hash[:24]}")
        prior = unique.get(exact_hash)
        if prior is None:
            record["duplicate_count"] = 1
            unique[exact_hash] = record
            continue
        prior["duplicate_count"] = int(prior.get("duplicate_count") or 1) + 1
        prior["duplicate_result_ids"] = sorted(
            set(prior.get("duplicate_result_ids", [])) | {str(record.get("result_id") or "")}
        )
    return [unique[key] for key in sorted(unique)]


def group_observations(
    records: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
    """Create deterministic corpus and metadata-scoped populations."""
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for raw in exact_deduplicate_observations(records):
        assertion = _normalized_part(raw.get("assertion_key"))
        family = _evidence_family(raw)
        theme = _assertion_theme(raw)
        polarity = _normalized_part(raw.get("polarity")) or "unknown"
        if not assertion:
            continue
        record = dict(raw)
        record.update(
            {
                "assertion_key": assertion,
                "evidence_family": family,
                "assertion_theme": theme,
                "scope": _normalized_part(raw.get("scope")) or "corpus",
                "polarity": polarity,
            }
        )
        binding = _scope_binding(record)
        scopes = {"global"}
        if binding["harness"]:
            scopes.add(f"harness_{binding['harness']}")
        if binding["repo"]:
            scopes.add(f"repo_{binding['repo']}")
        if binding["response_model"] and not binding["model_ambiguous"]:
            scopes.add(f"model_{binding['response_model']}")
        for scope in sorted(scopes):
            groups.setdefault((family, theme, scope, polarity), []).append(record)
    for group in groups.values():
        group.sort(key=lambda item: (str(item.get("root_session_id") or ""), str(item.get("observation_id") or "")))
    return dict(sorted(groups.items()))


def _brief_observation(record: Mapping[str, Any], report: RedactionReport) -> dict[str, Any]:
    evidence = []
    for item in record.get("evidence", []):
        if not isinstance(item, Mapping):
            continue
        evidence.append(
            {
                "ref": str(item.get("ref") or f"{item.get('window_id')}:{item.get('message_id')}"),
                "evidence_type": str(item.get("evidence_type") or "message"),
                "window_id": str(item.get("window_id") or ""),
                "session_id": str(item.get("session_id") or ""),
                "root_session_id": str(item.get("root_session_id") or record.get("root_session_id") or ""),
                "context_only": bool(item.get("context_only")),
                "timestamp": str(item.get("timestamp") or ""),
                "message_id": str(item.get("message_id") or ""),
                "tool_event_id": str(item.get("tool_event_id") or ""),
                "skill_exposure_id": str(item.get("skill_exposure_id") or ""),
                "fact": _normalized_text(item.get("fact")),
                "role": str(item.get("role") or ""),
                "seq": item.get("seq"),
                "quote": str(item.get("quote") or ""),
                "quote_start": item.get("quote_start"),
                "quote_end": item.get("quote_end"),
                "source_hash": str(item.get("source_hash") or ""),
                "content_hash": str(item.get("content_hash") or ""),
                "emitted_source_hash": str(item.get("emitted_source_hash") or ""),
                "artifact_id": item.get("artifact_id"),
                "artifact_hash": str(item.get("artifact_hash") or ""),
                "parser_version": str(item.get("parser_version") or ""),
            }
        )
    provenance = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    return {
        "observation_id": str(record.get("observation_id") or ""),
        "kind": str(record.get("kind") or ""),
        "assertion_key": str(record.get("assertion_key") or ""),
        "server_theme": str(record.get("server_theme") or ""),
        "evidence_family": str(record.get("evidence_family") or _evidence_family(record)),
        "assertion_theme": str(record.get("assertion_theme") or _assertion_theme(record)),
        "scope": str(record.get("scope") or ""),
        "polarity": str(record.get("polarity") or ""),
        "root_session_id": str(record.get("root_session_id") or ""),
        "observed_at_start": str(record.get("observed_at_start") or ""),
        "observed_at_end": str(record.get("observed_at_end") or ""),
        "source_packet_id": str(record.get("packet_id") or ""),
        "source_result_id": str(record.get("result_id") or ""),
        "luna_producer": dict(record.get("luna_producer") or {}),
        "harness": redact_text(_normalized_text(record.get("harness")), report),
        "repo": redact_text(_normalized_text(record.get("repo")), report),
        "model_attribution": dict(record.get("model_attribution") or {"descriptive_only": True}),
        "does_not_prove": redact_text(_normalized_text(record.get("does_not_prove")), report),
        "proof_arcs": [dict(arc) for arc in record.get("proof_arcs", []) if isinstance(arc, Mapping)],
        "evidence": evidence,
        "provenance_hashes": {
            "observation_hash": str(record.get("exact_hash") or ""),
            "packet_hash": str(provenance.get("packet_hash") or ""),
            "artifact_hashes": list(provenance.get("artifact_hashes") or []),
            "source_hashes": list(provenance.get("source_hashes") or []),
        },
    }


def _distribution(records: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    roots: dict[str, Mapping[str, Any]] = {}
    for record in records:
        root = str(record.get("root_session_id") or "")
        if root:
            roots.setdefault(root, record)
    harness: dict[str, int] = {}
    repo: dict[str, int] = {}
    for record in roots.values():
        h = _normalized_text(record.get("harness")) or "(unknown)"
        r = _normalized_text(record.get("repo")) or "(unknown)"
        harness[h] = harness.get(h, 0) + 1
        repo[r] = repo.get(r, 0) + 1
    return {"harnesses": dict(sorted(harness.items())), "repos": dict(sorted(repo.items()))}


def _scope_component(value: Any) -> str:
    normalized = _normalized_part(value)
    return "" if normalized in {"", "unknown"} else normalized


def _record_terminal_model(record: Mapping[str, Any]) -> tuple[str, bool]:
    attribution = record.get("model_attribution")
    if not isinstance(attribution, Mapping):
        return "", False
    return (
        _normalized_part(attribution.get("response_model")),
        bool(attribution.get("ambiguous")),
    )


def _scope_binding(record: Mapping[str, Any]) -> dict[str, Any]:
    model, model_ambiguous = _record_terminal_model(record)
    return {
        "scope": _normalized_part(record.get("scope")) or "corpus",
        "harness": _scope_component(record.get("harness")),
        "repo": _scope_component(record.get("repo")),
        "response_model": model,
        "model_ambiguous": model_ambiguous,
    }


def _scope_population_metadata(records: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for record in records:
        observation_id = str(record.get("observation_id") or "")
        if observation_id:
            bindings[observation_id] = _scope_binding(record)
    ordered_bindings = {key: bindings[key] for key in sorted(bindings)}

    def counts(key: str, *, include_empty: bool = False) -> dict[str, int]:
        result: dict[str, int] = {}
        for binding in ordered_bindings.values():
            value = str(binding.get(key) or "")
            if not value and not include_empty:
                continue
            result[value or "(unknown)"] = result.get(value or "(unknown)", 0) + 1
        return dict(sorted(result.items()))

    scope_distribution = {
        "observation_bindings": ordered_bindings,
        "scopes": counts("scope"),
        "harnesses": counts("harness", include_empty=True),
        "repos": counts("repo", include_empty=True),
    }
    model_attribution = {
        "response_models": counts("response_model"),
        "ambiguous_observation_ids": sorted(
            observation_id
            for observation_id, binding in ordered_bindings.items()
            if bool(binding.get("model_ambiguous"))
        ),
        "unattributed_observation_ids": sorted(
            observation_id
            for observation_id, binding in ordered_bindings.items()
            if not str(binding.get("response_model") or "")
        ),
    }
    return scope_distribution, model_attribution


def _population_binding(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    materialized = [dict(record) for record in records]
    scope_distribution, terminal_model_attribution = _scope_population_metadata(materialized)
    observation_ids = sorted(
        str(record.get("observation_id") or "")
        for record in materialized
        if str(record.get("observation_id") or "")
    )
    root_ids = sorted(
        {
            str(record.get("root_session_id") or "")
            for record in materialized
        }
        - {""}
    )
    body = {
        "observation_ids": observation_ids,
        "root_session_ids": root_ids,
        "observation_count": len(observation_ids),
        "root_count": len(root_ids),
        "distribution": _distribution(materialized),
        "scope_distribution": scope_distribution,
        "terminal_model_attribution": terminal_model_attribution,
    }
    return {**body, "hash": _sha256(body)}


def _bounded_stratified_records(records: Iterable[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(record) for record in records),
        key=lambda record: (
            _normalized_text(record.get("harness")), _normalized_text(record.get("repo")),
            str(record.get("root_session_id") or ""), str(record.get("observation_id") or ""),
        ),
    )
    if len(ordered) <= limit:
        return ordered
    strata: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in ordered:
        strata.setdefault(
            (_normalized_text(record.get("harness")), _normalized_text(record.get("repo"))), []
        ).append(record)
    chosen: list[dict[str, Any]] = []
    while len(chosen) < limit and strata:
        for key in sorted(list(strata)):
            if len(chosen) >= limit:
                break
            chosen.append(strata[key].pop(0))
            if not strata[key]:
                del strata[key]
    return chosen


def _inferred_target_kind(path: str) -> str:
    name = Path(path).name.lower()
    if name in {"agents.md", "instructions.md", "claude.md"}:
        return "instruction_file"
    if "skill" in name:
        return "skill"
    if "harness" in name:
        return "harness_rule"
    return "config"


def _config_entries(config_inventory: Any) -> list[dict[str, str]]:
    if config_inventory is None:
        return []
    values: Sequence[Any]
    if isinstance(config_inventory, Mapping):
        if isinstance(config_inventory.get("configs"), list):
            values = config_inventory["configs"]
        elif isinstance(config_inventory.get("items"), list):
            values = config_inventory["items"]
        else:
            values = [
                {"path": str(path), "content": content}
                for path, content in config_inventory.items()
                if isinstance(content, str)
            ]
    elif isinstance(config_inventory, list):
        values = config_inventory
    else:
        return []
    entries: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        content = value.get("content") or value.get("text") or ""
        if not isinstance(content, str):
            continue
        path = str(value.get("path") or value.get("name") or "(unnamed)")
        fingerprint = str(value.get("fingerprint") or value.get("content_hash") or _sha256(content))
        target_kind = _normalized_part(value.get("target_kind")) or _inferred_target_kind(path)
        entries.append({"path": path, "content": content, "fingerprint": fingerprint, "target_kind": target_kind})
    return entries


def _config_target_ref(path: str, fingerprint: str) -> str:
    canonical_path = str(Path(path).expanduser().resolve())
    return "target_" + hashlib.sha256(
        (canonical_path + "\0" + fingerprint).encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def _deduplicated_config_entries(config_inventory: Any) -> list[dict[str, str]]:
    entries: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in _config_entries(config_inventory):
        target_path = str(Path(item["path"]).expanduser().resolve())
        identity = (target_path, item["fingerprint"], item["target_kind"])
        normalized = {**item, "path": target_path}
        existing = entries.get(identity)
        if existing is not None:
            if existing["content"] != normalized["content"]:
                raise ValueError("conflicting config target content for identical target metadata")
            continue
        entries[identity] = normalized
    return [entries[key] for key in sorted(entries)]


def build_config_target_map(config_inventory: Any) -> dict[str, Any]:
    """Keep raw target paths in a local map that is never included in Terra packets."""
    targets: list[dict[str, str]] = []
    for item in _deduplicated_config_entries(config_inventory):
        target_path = item["path"]
        target = {
            "target_ref": _config_target_ref(target_path, item["fingerprint"]),
            "target_path": target_path,
            "fingerprint": item["fingerprint"],
            "target_kind": item["target_kind"],
        }
        targets.append(target)
    return {
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "targets": sorted(
            targets,
            key=lambda item: (
                item["target_ref"],
                item["target_path"],
                item["fingerprint"],
                item["target_kind"],
            ),
        ),
    }


def _config_target_index(config_targets: Any) -> dict[str, dict[str, str]]:
    values = config_targets.get("targets") if isinstance(config_targets, Mapping) else config_targets
    if not isinstance(values, list):
        return {}
    index: dict[str, dict[str, str]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            continue
        target_ref = str(item.get("target_ref") or "")
        target_path = str(item.get("target_path") or "")
        fingerprint = str(item.get("fingerprint") or "")
        target_kind = _normalized_part(item.get("target_kind"))
        if target_ref and target_path and fingerprint and target_kind:
            index[target_ref] = {
                "target_ref": target_ref,
                "target_path": target_path,
                "fingerprint": fingerprint,
                "target_kind": target_kind,
            }
    return index


def _config_overlap(
    assertion_key: str,
    config_inventory: Any,
    report: RedactionReport,
    cfg: SynthesisConfig,
) -> dict[str, Any]:
    tokens = [
        token for token in _normalized_part(assertion_key).split("_")
        if len(token) >= 3 and token not in _STOP_CONFIG_TOKENS
    ]
    entries = _deduplicated_config_entries(config_inventory)
    searched = [
        {
            "target_ref": _config_target_ref(item["path"], item["fingerprint"]),
            "fingerprint": item["fingerprint"],
            "target_kind": item["target_kind"],
        }
        for item in entries
    ]
    matched: list[dict[str, Any]] = []
    for item in entries:
        lines = [line.strip() for line in item["content"].splitlines() if line.strip()]
        overlaps = [line for line in lines if any(token in line.lower() for token in tokens)]
        if not overlaps:
            continue
        snippet = "\n".join(overlaps)[:cfg.max_config_snippet_chars]
        matched.append(
            {
                "target_ref": _config_target_ref(item["path"], item["fingerprint"]),
                "fingerprint": item["fingerprint"],
                "snippet": redact_text(snippet, report),
            }
        )
        if len(matched) >= cfg.max_config_snippets:
            break
    return {"available": bool(entries), "searched": searched, "matches": matched}


def _manifest_config_inventory(manifest: Mapping[str, Any]) -> Any:
    for key in ("config_inventory", "config_snapshots", "configs"):
        if key in manifest:
            return manifest[key]
    return None


def _scope_denominator(
    coverage: Mapping[str, Any],
    scope: str,
    *,
    global_denominator: int,
) -> dict[str, int] | None:
    if scope == "global":
        eligible_windows = _nonnegative_int(coverage.get("eligible_windows"))
        return {
            "eligible_roots": global_denominator,
            "eligible_windows": eligible_windows if eligible_windows is not None else global_denominator,
        }
    raw = coverage.get("scope_denominators")
    entry = raw.get(scope) if isinstance(raw, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    eligible_roots = _positive_int(entry.get("eligible_roots"))
    eligible_windows = _positive_int(entry.get("eligible_windows"))
    if eligible_roots is None or eligible_windows is None:
        return None
    return {"eligible_roots": eligible_roots, "eligible_windows": eligible_windows}


def _record_window_count(records: Iterable[Mapping[str, Any]]) -> int:
    return len(
        {
            str(item.get("window_id") or "")
            for record in records
            for item in record.get("evidence", [])
            if isinstance(item, Mapping) and str(item.get("window_id") or "")
        }
    )


def _scope_processing(
    processing_coverage: Mapping[str, Any] | None,
    scope: str,
    records: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    inferred_records = [dict(record) for record in records]
    raw_counts = processing_coverage.get("scope_counts") if isinstance(processing_coverage, Mapping) else None
    entry = raw_counts.get(scope) if isinstance(raw_counts, Mapping) else None
    if isinstance(entry, Mapping):
        processed_roots = _nonnegative_int(entry.get("processed_roots"))
        processed_windows = _nonnegative_int(entry.get("processed_windows"))
        if processed_roots is not None and processed_windows is not None:
            return {
                "processed_roots": processed_roots,
                "processed_windows": processed_windows,
            }
    if scope != "global" and isinstance(processing_coverage, Mapping) and isinstance(raw_counts, Mapping):
        return {"processed_roots": 0, "processed_windows": 0}
    return {
        "processed_roots": len(
            {
                str(record.get("root_session_id") or "")
                for record in inferred_records
                if str(record.get("root_session_id") or "")
            }
        ),
        "processed_windows": _record_window_count(inferred_records),
    }


def _scope_coverage(
    coverage: Mapping[str, Any],
    scope: str,
    denominator: Mapping[str, int],
    processing: Mapping[str, int],
) -> dict[str, Any]:
    scoped = dict(coverage)
    eligible_roots = int(denominator["eligible_roots"])
    processed_roots = int(processing["processed_roots"])
    full_publication = (
        str(coverage.get("publication_mode") or "") == "full"
        and bool(coverage.get("publication_complete"))
    )
    scoped.update(
        {
            "scope": scope,
            "scope_denominator_bound": True,
            "full_eligible_root_denominator": eligible_roots,
            "eligible_roots": eligible_roots,
            "eligible_windows": int(denominator["eligible_windows"]),
            "processed_roots": processed_roots,
            "processed_windows": int(processing["processed_windows"]),
            "processing_incomplete": processed_roots < eligible_roots,
            "scope_selection_complete": full_publication,
        }
    )
    if full_publication:
        scoped.update(
            {
                "selected_roots": eligible_roots,
                "selected_windows": int(denominator["eligible_windows"]),
            }
        )
    return scoped


def build_synthesis_packets(
    records: Iterable[Mapping[str, Any]],
    coverage_manifest: Mapping[str, Any] | Path | str,
    *,
    config_inventory: Any = None,
    processing_coverage: Mapping[str, Any] | None = None,
    config: SynthesisConfig | None = None,
) -> list[dict[str, Any]]:
    """Create redacted Terra packets grouped by assertion, scope, and polarity."""
    cfg = config or SynthesisConfig()
    if cfg.max_supporting_observations < 1 or cfg.max_counterexample_observations < 1:
        raise ValueError("packet bounds must be positive")
    manifest = _manifest_from(coverage_manifest)
    prompt = load_synthesis_prompt()
    synthesis_assignment = _assignment(
        role="synthesis",
        provider=cfg.producer_provider,
        model=cfg.producer_model,
        worker_id=cfg.producer_worker_id,
        assignment_id=cfg.producer_assignment_id,
        prompt=prompt,
    )
    review_assignment = _assignment(
        role="second_review",
        provider=cfg.reviewer_provider,
        model=cfg.reviewer_model,
        worker_id=cfg.reviewer_worker_id,
        assignment_id=cfg.reviewer_assignment_id,
        prompt=prompt,
    )
    if (
        synthesis_assignment["provider"], synthesis_assignment["model"], synthesis_assignment["worker_id"], synthesis_assignment["assignment_id"]
    ) == (
        review_assignment["provider"], review_assignment["model"], review_assignment["worker_id"], review_assignment["assignment_id"]
    ):
        raise ValueError("synthesis and second-review assignments must use different identities")
    coverage, denominator = _coverage(manifest)
    corpus_snapshot, corpus_snapshot_hash = _corpus_snapshot(manifest)
    deduplicated = exact_deduplicate_observations(records)
    inferred_roots = {str(record.get("root_session_id") or "") for record in deduplicated} - {""}
    processing = {
        "processed_packets": 0,
        "processed_windows": len({str(item.get("window_id") or "") for record in deduplicated for item in record.get("evidence", []) if isinstance(item, Mapping)} - {""}),
        "processed_roots": len(inferred_roots),
        "abstained_packets": 0,
        "valid_observations": len(deduplicated),
    }
    if processing_coverage is not None:
        for key in processing:
            if _positive_int(processing_coverage.get(key)) is not None or processing_coverage.get(key) == 0:
                processing[key] = int(processing_coverage[key])
    coverage = dict(coverage)
    coverage.update(
        {
            "eligible_roots": denominator,
            "processed_roots": int(processing["processed_roots"]),
            "processed_packets": int(processing["processed_packets"]),
            "processed_windows": int(processing["processed_windows"]),
            "abstained_packets": int(processing["abstained_packets"]),
            "valid_observations": int(processing["valid_observations"]),
            "processing_incomplete": int(processing["processed_roots"]) < denominator,
        }
    )
    inferred_by_harness: dict[str, set[str]] = {}
    for record in deduplicated:
        root_id = str(record.get("root_session_id") or "")
        if root_id:
            inferred_by_harness.setdefault(str(record.get("harness") or "(unknown)"), set()).add(root_id)
    reported_by_harness = processing.get("processed_roots_by_harness")
    processed_by_harness = (
        {str(key): int(value) for key, value in reported_by_harness.items()}
        if isinstance(reported_by_harness, Mapping)
        else {key: len(value) for key, value in inferred_by_harness.items()}
    )
    raw_capabilities = coverage.get("proof_capability_by_harness")
    if isinstance(raw_capabilities, Mapping):
        coverage["proof_capability_by_harness"] = {
            str(harness): {
                **dict(entry),
                "processed_roots": int(processed_by_harness.get(str(harness), 0)),
            }
            for harness, entry in sorted(raw_capabilities.items())
            if isinstance(entry, Mapping)
        }
    groups = group_observations(deduplicated)
    configs = _manifest_config_inventory(manifest) if config_inventory is None else config_inventory
    config_target_map = build_config_target_map(configs)
    config_target_map_hash = _sha256(config_target_map)
    packets: list[dict[str, Any]] = []
    for (family, theme, scope, polarity), supporting in groups.items():
        counterexamples = [
            record
            for (other_family, other_theme, other_scope, other_polarity), values in groups.items()
            if other_family == family
            and other_theme == theme
            and other_scope == scope
            and other_polarity != polarity
            for record in values
        ]
        scoped_records = [
            record
            for (other_family, other_theme, other_scope, _), values in groups.items()
            if other_family == family and other_theme == theme and other_scope == scope
            for record in values
        ]
        scope_denominator = _scope_denominator(
            coverage,
            scope,
            global_denominator=denominator,
        )
        if scope_denominator is None:
            continue
        scope_processing = (
            {
                "processed_roots": int(processing["processed_roots"]),
                "processed_windows": int(processing["processed_windows"]),
            }
            if scope == "global"
            else _scope_processing(processing_coverage, scope, scoped_records)
        )
        packet_coverage = _scope_coverage(
            coverage,
            scope,
            scope_denominator,
            scope_processing,
        )
        report = RedactionReport()
        assertion_keys = sorted({str(record["assertion_key"]) for record in supporting})
        packet_supporting = _bounded_stratified_records(supporting, cfg.max_supporting_observations)
        packet_counterexamples = _bounded_stratified_records(counterexamples, cfg.max_counterexample_observations)
        supporting_population = _population_binding(supporting)
        counterexample_population = _population_binding(counterexamples)
        supporting_ids = list(supporting_population["observation_ids"])
        supporting_roots = list(supporting_population["root_session_ids"])
        counterexample_ids = list(counterexample_population["observation_ids"])
        counterexample_roots = list(counterexample_population["root_session_ids"])
        packet_seed = {
            "evidence_family": family,
            "assertion_theme": theme,
            "assertion_keys": assertion_keys,
            "scope": scope,
            "polarity": polarity,
            "supporting_observation_ids": [record["observation_id"] for record in packet_supporting],
            "counterexample_observation_ids": [record["observation_id"] for record in packet_counterexamples],
            "manifest_hash": _sha256(manifest),
        }
        packet_id = f"spkt_{_short_hash(packet_seed)}"
        body: dict[str, Any] = {
            "schema_version": SYNTHESIS_SCHEMA_VERSION,
            "packet_id": packet_id,
            "source_run_id": str(manifest.get("run_id") or ""),
            "corpus_snapshot_hash": corpus_snapshot_hash,
            "corpus_snapshot": corpus_snapshot,
            "config_target_map_hash": config_target_map_hash,
            "synthesis_assignment": synthesis_assignment,
            "review_assignment": review_assignment,
            "producer_contract": {
                "expected": synthesis_assignment,
                "bound": _assignment_matches(synthesis_assignment, synthesis_assignment),
            },
            "second_review_contract": {
                "expected": review_assignment,
                "bound": _assignment_matches(review_assignment, review_assignment),
            },
        "group": {
            "evidence_family": family,
            "assertion_theme": theme,
            "assertion_keys": assertion_keys,
                "scope": scope,
                "polarity": polarity,
            },
            "supporting_observations": [
                _brief_observation(record, report)
                for record in packet_supporting
            ],
            "counterexample_observations": [
                _brief_observation(record, report)
                for record in packet_counterexamples
            ],
            "coverage": packet_coverage,
            "exclusions": list(manifest.get("excluded_roots") or []),
            "distribution": {
                "supporting": _distribution(supporting),
                "counterexamples": _distribution(counterexamples),
                "full_corpus": {
                    "harnesses": dict(manifest.get("per_harness") or {}),
                    "repos": dict(manifest.get("per_repo") or {}),
                },
            },
            "group_counts": {
                "supporting_observations": supporting_population["observation_count"],
                "supporting_roots": supporting_population["root_count"],
                "counterexample_observations": counterexample_population["observation_count"],
                "counterexample_roots": counterexample_population["root_count"],
            },
            "full_population": {
                "supporting": supporting_population,
                "counterexamples": counterexample_population,
            },
            "group_membership": {
                "supporting_observation_ids": supporting_ids,
                "supporting_root_session_ids": supporting_roots,
                "counterexample_observation_ids": counterexample_ids,
                "counterexample_root_session_ids": counterexample_roots,
            },
            "sampling": {
                "supporting_observations_truncated": len(packet_supporting) < len(supporting),
                "supporting_roots_truncated": len({str(record.get("root_session_id") or "") for record in packet_supporting} - {""}) < len(supporting_roots),
                "counterexample_observations_truncated": len(packet_counterexamples) < len(counterexamples),
                "counterexample_roots_truncated": len({str(record.get("root_session_id") or "") for record in packet_counterexamples} - {""}) < len(counterexample_roots),
            },
            "config_overlap": _config_overlap(" ".join(assertion_keys), configs, report, cfg),
            "provenance": {
                "manifest_hash": _sha256(manifest),
                "source_packet_hashes": sorted(
                    {
                        str((record.get("provenance") or {}).get("packet_hash") or "")
                        for record in [*supporting, *counterexamples]
                    }
                    - {""}
                ),
                "observation_hashes": sorted(str(record.get("exact_hash") or "") for record in [*supporting, *counterexamples]),
                "redaction_version": report.version,
                "full_eligible_root_denominator": int(scope_denominator["eligible_roots"]),
                "processed_root_denominator": int(scope_processing["processed_roots"]),
                "corpus_snapshot_hash": corpus_snapshot_hash,
                "config_target_map_hash": config_target_map_hash,
            },
            "redaction": report.to_dict(),
        }
        body["full_population"]["hash"] = _sha256(body["full_population"])
        body["packet_hash"] = _sha256(body)
        packets.append(body)
    return sorted(packets, key=lambda packet: str(packet["packet_id"]))


def emit_synthesis_packets(
    run_dir: Path | str,
    records: Iterable[Mapping[str, Any]] | None = None,
    *,
    coverage_manifest: Mapping[str, Any] | Path | str | None = None,
    config_inventory: Any = None,
    config: SynthesisConfig | None = None,
    luna_results: Iterable[Path | str] | None = None,
) -> dict[str, Any]:
    """Write redacted synthesis packets under a coach run directory."""
    target = assert_writable(Path(run_dir), purpose="coach synthesis run")
    target.mkdir(parents=True, exist_ok=True)
    manifest = _manifest_from(coverage_manifest) if coverage_manifest is not None else _manifest_from(target / "manifest.json")
    if records is None:
        result_paths = tuple(luna_results) if luna_results is not None else None
        source_records, failures = load_validated_observation_records(
            target, manifest=manifest, result_paths=result_paths
        )
        processing_coverage, processing_failures = summarize_result_processing_coverage(
            target, manifest=manifest, result_paths=result_paths
        )
        failures = [*failures, *processing_failures]
    else:
        source_records, failures, processing_coverage = list(records), [], None
    if failures:
        raise ValueError("cannot synthesize invalid observation records: " + ", ".join(f.reason for f in failures))
    configs = _manifest_config_inventory(manifest) if config_inventory is None else config_inventory
    packets = build_synthesis_packets(
        source_records,
        manifest,
        config_inventory=configs,
        processing_coverage=processing_coverage,
        config=config,
    )
    packet_dir = target / "synthesis_packets"
    packet_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for packet in packets:
        path = packet_dir / f"{packet['packet_id']}.json"
        assert_writable(path, purpose="coach synthesis packet")
        write_text(path, json.dumps(packet, indent=2, ensure_ascii=False) + "\n")
        entries.append(
            {
                "packet_id": packet["packet_id"],
                "path": str(path.relative_to(target)),
                "packet_hash": packet["packet_hash"],
                "group": packet["group"],
            }
        )
    output = {
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "source_manifest_hash": _sha256(manifest),
        "corpus_snapshot_hash": _corpus_snapshot(manifest)[1],
        "corpus_snapshot": _corpus_snapshot(manifest)[0],
        "coverage": packets[0]["coverage"] if packets else {
            **_coverage(manifest)[0],
            "eligible_roots": _coverage(manifest)[1],
            "processed_roots": int((processing_coverage or {}).get("processed_roots") or 0),
            "processed_packets": int((processing_coverage or {}).get("processed_packets") or 0),
            "processed_windows": int((processing_coverage or {}).get("processed_windows") or 0),
            "abstained_packets": int((processing_coverage or {}).get("abstained_packets") or 0),
            "valid_observations": int((processing_coverage or {}).get("valid_observations") or 0),
            "processing_incomplete": True,
        },
        "observation_count": len(exact_deduplicate_observations(source_records)),
        "packets": entries,
    }
    target_map = build_config_target_map(configs)
    target_map_path = target / "synthesis_config_targets.json"
    assert_writable(target_map_path, purpose="coach synthesis private config target map")
    write_text(target_map_path, json.dumps(target_map, indent=2, ensure_ascii=False) + "\n")
    output["config_target_map"] = {
        "path": str(target_map_path.relative_to(target)),
        "hash": _sha256(target_map),
    }
    path = target / "synthesis_manifest.json"
    assert_writable(path, purpose="coach synthesis manifest")
    write_text(path, json.dumps(output, indent=2, ensure_ascii=False) + "\n")
    return output


def load_synthesis_prompt() -> str:
    path = Path(__file__).resolve().parent / "prompts" / "terra_synthesis.md"
    return path.read_text(encoding="utf-8")


def _packet_observation_index(packet: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], set[str], set[str]]:
    supporting: dict[str, dict[str, Any]] = {}
    counterexample_ids: set[str] = set()
    support_ids: set[str] = set()
    for bucket, is_counter in (("supporting_observations", False), ("counterexample_observations", True)):
        for observation in packet.get(bucket, []):
            if not isinstance(observation, Mapping):
                continue
            observation_id = str(observation.get("observation_id") or "")
            if not observation_id:
                continue
            supporting[observation_id] = dict(observation)
            (counterexample_ids if is_counter else support_ids).add(observation_id)
    return supporting, support_ids, counterexample_ids


def _population_scope_metadata(
    item: Mapping[str, Any],
    observation_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    raw_distribution = item.get("scope_distribution")
    raw_models = item.get("terminal_model_attribution")
    if not isinstance(raw_distribution, Mapping) or not isinstance(raw_models, Mapping):
        return None
    raw_bindings = raw_distribution.get("observation_bindings")
    if not isinstance(raw_bindings, Mapping) or set(raw_bindings) != set(observation_ids):
        return None
    bindings: dict[str, dict[str, Any]] = {}
    for observation_id in observation_ids:
        raw_binding = raw_bindings.get(observation_id)
        if not isinstance(raw_binding, Mapping) or set(raw_binding) != {
            "scope", "harness", "repo", "response_model", "model_ambiguous",
        }:
            return None
        scope = _normalized_part(raw_binding.get("scope")) or "corpus"
        harness = _scope_component(raw_binding.get("harness"))
        repo = _scope_component(raw_binding.get("repo"))
        response_model = _normalized_part(raw_binding.get("response_model"))
        ambiguous = raw_binding.get("model_ambiguous")
        if (
            not isinstance(ambiguous, bool)
            or str(raw_binding.get("scope") or "") != scope
            or str(raw_binding.get("harness") or "") != harness
            or str(raw_binding.get("repo") or "") != repo
            or str(raw_binding.get("response_model") or "") != response_model
        ):
            return None
        bindings[observation_id] = {
            "scope": scope,
            "harness": harness,
            "repo": repo,
            "response_model": response_model,
            "model_ambiguous": ambiguous,
        }

    def counts(key: str, *, include_empty: bool = False) -> dict[str, int]:
        result: dict[str, int] = {}
        for binding in bindings.values():
            value = str(binding.get(key) or "")
            if not value and not include_empty:
                continue
            result[value or "(unknown)"] = result.get(value or "(unknown)", 0) + 1
        return dict(sorted(result.items()))

    scope_distribution = {
        "observation_bindings": {key: bindings[key] for key in sorted(bindings)},
        "scopes": counts("scope"),
        "harnesses": counts("harness", include_empty=True),
        "repos": counts("repo", include_empty=True),
    }
    terminal_model_attribution = {
        "response_models": counts("response_model"),
        "ambiguous_observation_ids": sorted(
            observation_id
            for observation_id, binding in bindings.items()
            if bool(binding["model_ambiguous"])
        ),
        "unattributed_observation_ids": sorted(
            observation_id
            for observation_id, binding in bindings.items()
            if not str(binding["response_model"])
        ),
    }
    if (
        raw_distribution != scope_distribution
        or raw_models != terminal_model_attribution
    ):
        return None
    return scope_distribution, terminal_model_attribution


def _full_population_from_packet(packet: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = packet.get("full_population")
    if not isinstance(raw, Mapping):
        return None
    body = {key: value for key, value in raw.items() if key != "hash"}
    declared_hash = str(raw.get("hash") or "")
    if not declared_hash or _sha256(body) != declared_hash:
        return None
    normalized: dict[str, Any] = {}
    for label in ("supporting", "counterexamples"):
        item = raw.get(label)
        if not isinstance(item, Mapping):
            return None
        member_body = {key: value for key, value in item.items() if key != "hash"}
        if str(item.get("hash") or "") != _sha256(member_body):
            return None
        observation_ids = item.get("observation_ids")
        root_ids = item.get("root_session_ids")
        observation_count = item.get("observation_count")
        root_count = item.get("root_count")
        distribution = item.get("distribution")
        harness_counts = distribution.get("harnesses") if isinstance(distribution, Mapping) else None
        repo_counts = distribution.get("repos") if isinstance(distribution, Mapping) else None
        valid_counts = (
            isinstance(harness_counts, Mapping)
            and isinstance(repo_counts, Mapping)
            and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in harness_counts.values())
            and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in repo_counts.values())
        )
        if (
            not isinstance(observation_ids, list)
            or not isinstance(root_ids, list)
            or len({str(value) for value in observation_ids if str(value)}) != len(observation_ids)
            or len({str(value) for value in root_ids if str(value)}) != len(root_ids)
            or observation_ids != sorted(str(value) for value in observation_ids)
            or root_ids != sorted(str(value) for value in root_ids)
            or observation_count != len(observation_ids)
            or root_count != len(root_ids)
            or not valid_counts
            or sum(harness_counts.values()) != root_count
            or sum(repo_counts.values()) != root_count
        ):
            return None
        scope_metadata = _population_scope_metadata(item, [str(value) for value in observation_ids])
        if scope_metadata is None:
            return None
        scope_distribution, terminal_model_attribution = scope_metadata
        normalized[label] = {
            "observation_ids": [str(value) for value in observation_ids],
            "root_session_ids": [str(value) for value in root_ids],
            "observation_count": observation_count,
            "root_count": root_count,
            "distribution": {
                "harnesses": {str(key): value for key, value in harness_counts.items()},
                "repos": {str(key): value for key, value in repo_counts.items()},
            },
            "scope_distribution": scope_distribution,
            "terminal_model_attribution": terminal_model_attribution,
            "hash": str(item.get("hash") or ""),
        }
    normalized["hash"] = declared_hash
    membership = packet.get("group_membership")
    if isinstance(membership, Mapping):
        checks = {
            "supporting_observation_ids": normalized["supporting"]["observation_ids"],
            "supporting_root_session_ids": normalized["supporting"]["root_session_ids"],
            "counterexample_observation_ids": normalized["counterexamples"]["observation_ids"],
            "counterexample_root_session_ids": normalized["counterexamples"]["root_session_ids"],
        }
        if any(membership.get(key) != value for key, value in checks.items()):
            return None
    return normalized


def _canonical(candidate: Mapping[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    raw = candidate.get("canonical")
    if not isinstance(raw, Mapping):
        return None, None
    scope = _normalized_part(raw.get("scope"))
    subject = _normalized_part(raw.get("subject"))
    predicate = _normalized_part(raw.get("predicate"))
    polarity = _normalized_part(raw.get("polarity"))
    if not all((scope, subject, predicate, polarity)):
        return None, None
    if scope in {"corpus", "global_corpus"}:
        scope = "global"
    if polarity not in {"positive", "negative", "mixed"}:
        return None, None
    canonical = {"scope": scope, "subject": subject, "predicate": predicate, "polarity": polarity}
    return canonical, ":".join((scope, subject, predicate, polarity))


def _packet_scope(packet: Mapping[str, Any]) -> str:
    group = packet.get("group")
    scope = _normalized_part(group.get("scope")) if isinstance(group, Mapping) else ""
    return "global" if scope in {"", "corpus", "global_corpus"} else scope


def _full_population_matches_scope(
    population: Mapping[str, Any],
    scope: str,
) -> bool:
    bindings = ((population.get("scope_distribution") or {}).get("observation_bindings"))
    if not isinstance(bindings, Mapping) or not bindings:
        return False
    if scope == "global":
        return True
    if scope.startswith("harness_"):
        expected = scope[len("harness_"):]
        return bool(expected) and all(
            isinstance(binding, Mapping)
            and str(binding.get("harness") or "") == expected
            for binding in bindings.values()
        )
    if scope.startswith("repo_"):
        expected = scope[len("repo_"):]
        return bool(expected) and all(
            isinstance(binding, Mapping)
            and str(binding.get("repo") or "") == expected
            for binding in bindings.values()
        )
    if scope.startswith("model_"):
        expected = scope[len("model_"):]
        return bool(expected) and all(
            isinstance(binding, Mapping)
            and not bool(binding.get("model_ambiguous"))
            and str(binding.get("response_model") or "") == expected
            for binding in bindings.values()
        )
    return False


def _candidate_text(candidate: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = candidate.get(key)
    if not required and value in (None, ""):
        return ""
    return _plain_text(value)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'_/-]*", text))


def _contextual_card_text(
    title: str,
    summary: str,
    limitation: str,
    instruction: str,
    *,
    kind: str,
    n: int,
    processed_roots: int,
    denominator: int,
    cited_roots: int | None = None,
    support_is_sampled: bool = False,
) -> bool:
    if not (4 <= len(title) <= 140 and 80 <= len(summary) <= 700 and 45 <= len(limitation) <= 500):
        return False
    if _word_count(summary) < 14 or _word_count(limitation) < 8:
        return False
    summary_lower = summary.lower()
    if _PIPELINE_NARRATION.search(summary):
        return False
    words = set(_normalized_part(word) for word in _word_count_tokens(summary_lower))
    if not words & _CARD_BEHAVIOR_WORDS or not words & _CARD_OUTCOME_WORDS:
        return False
    if not re.search(r"\b(?:when|after|before|during|for|in|while)\b", summary_lower):
        return False
    if processed_roots < denominator:
        if not (
            re.search(rf"\b{n}\s+of\s+{processed_roots}\b", summary_lower)
            and re.search(rf"\b{processed_roots}\s+of\s+{denominator}\b", summary_lower)
            and re.search(r"\b(?:partial|sampled|sample)\b", summary_lower)
        ):
            return False
    elif not (
        f"{n}/{denominator}" in summary
        or re.search(rf"\b{n}\s+of\s+{denominator}\b", summary_lower)
        or (support_is_sampled and re.search(rf"\b{n}\s+(?:supporting\s+)?roots?\s+of\s+{denominator}\b", summary_lower))
    ):
        return False
    if support_is_sampled:
        cited = n if cited_roots is None else cited_roots
        if not (
            re.search(r"\b(?:sampled|sample|cited)\b", summary_lower)
            and re.search(
                rf"\b{cited}\s+(?:cited\s+)?(?:supporting\s+)?roots?\s+of\s+{n}\b",
                summary_lower,
            )
        ):
            return False
    limitation_lower = limitation.lower()
    if not re.search(r"\b(?:does not|cannot|not enough|cannot establish|cannot determine)\b", limitation_lower):
        return False
    if kind != "coach_proposal":
        return not instruction
    return (
        24 <= len(instruction) <= 500
        and bool(re.match(r"^(?:add|archive|check|enforce|record|require|run|update|verify)\b", instruction.lower()))
        and bool(re.search(r"\b(?:before|after|when|must|only if)\b", instruction.lower()))
        and not bool(re.search(r"(?:;|\b(?:and|then|also)\b)", instruction.lower()))
    )


def _word_count_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'_/-]*", text)


def _semantic_canonical_key(kind: str, canonical: Mapping[str, str]) -> str:
    def normalize(value: str) -> str:
        normalized = _normalized_part(value)
        if normalized in _CANONICAL_TERM_ALIASES:
            return _CANONICAL_TERM_ALIASES[normalized]
        if normalized in {"instruction_miss", "delivery_gap", "instruction_follow"}:
            return normalized
        return "_".join(
            dict.fromkeys(
                _CANONICAL_TERM_ALIASES.get(token, token)
                for token in normalized.split("_")
                if token
            )
        )
    return ":".join((kind, str(canonical["scope"]), normalize(str(canonical["subject"])), normalize(str(canonical["predicate"])), str(canonical["polarity"])))


def _candidate_links_assertion_theme(
    canonical: Mapping[str, str],
    support_ids: Iterable[str],
    observations: Mapping[str, Mapping[str, Any]],
) -> bool:
    canonical_terms = {
        _THEME_ALIASES.get(token, token)
        for value in (canonical["subject"], canonical["predicate"])
        for token in _normalized_part(value).split("_")
        if token and token not in _THEME_NOISE
    }
    themes = {
        _assertion_theme(observations[observation_id])
        for observation_id in support_ids
        if observation_id in observations
    }
    return bool(canonical_terms) and all(
        canonical_terms
        & {
            _THEME_ALIASES.get(token, token)
            for token in theme.split("_")
            if token and token not in _THEME_NOISE
        }
        for theme in themes
    )


def _observation_roles(observation: Mapping[str, Any]) -> dict[str, set[str]]:
    roles: dict[str, set[str]] = {}
    for evidence in observation.get("evidence", []):
        if not isinstance(evidence, Mapping):
            continue
        ref = str(evidence.get("ref") or f"{evidence.get('window_id')}:{evidence.get('message_id')}")
        roles.setdefault(ref, set()).add(str(evidence.get("role") or ""))
    return roles


def _proof_context(observation: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, int]]:
    evidence_by_ref = {
        str(evidence.get("ref") or f"{evidence.get('window_id')}:{evidence.get('message_id')}"): evidence
        for evidence in observation.get("evidence", [])
        if isinstance(evidence, Mapping)
    }
    ordered_windows = sorted(
        {
            str(evidence.get("window_id") or "")
            for evidence in evidence_by_ref.values()
            if str(evidence.get("window_id") or "")
        },
        key=lambda window_id: (
            min(
                _timestamp_sort_key(evidence.get("timestamp"))
                for evidence in evidence_by_ref.values()
                if str(evidence.get("window_id") or "") == window_id
            ),
            window_id,
        ),
    )
    ranks: dict[tuple[int, str], int] = {}
    return evidence_by_ref, {
        window_id: ranks.setdefault(
            min(
                _timestamp_sort_key(evidence.get("timestamp"))
                for evidence in evidence_by_ref.values()
                if str(evidence.get("window_id") or "") == window_id
            ),
            len(ranks),
        )
        for window_id in ordered_windows
    }


def has_completion_evidence(observation: Mapping[str, Any]) -> bool:
    """Use the shared result predicate; assistant prose never proves completion."""
    evidence_by_ref, window_order = _proof_context(observation)
    outcome_evidence: list[Mapping[str, Any]] = []
    request_evidence: list[Mapping[str, Any]] = []
    request_windows: set[str] = set()
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        refs = arc.get("evidence_refs") or []
        if label in {"request", "expectation", "verification_request"}:
            request_windows.update(
                str(evidence_by_ref[str(ref)].get("window_id") or "")
                for ref in refs
                if str(ref) in evidence_by_ref
            )
            request_evidence.extend(
                evidence_by_ref[str(ref)] for ref in refs if str(ref) in evidence_by_ref
            )
        if label not in {"outcome", "verification_result", "delivery", "artifact"}:
            continue
        for ref in refs:
            evidence = evidence_by_ref.get(str(ref))
            if evidence is None:
                continue
            outcome_evidence.append(evidence)
    return supports_successful_result(
        outcome_evidence,
        request_window_ids=request_windows,
        window_order=window_order,
        request_evidence=request_evidence,
    )


_has_completion_evidence = has_completion_evidence


def has_bounded_gap_evidence(observation: Mapping[str, Any]) -> bool:
    evidence_by_ref, window_order = _proof_context(observation)
    request_labels = {"request", "expectation", "verification_request"}
    terminal_labels = {"gap", "delivery", "verification_result"}
    request_windows: set[str] = set()
    terminal_evidence: list[Mapping[str, Any]] = []
    request_evidence: list[Mapping[str, Any]] = []
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        refs = arc.get("evidence_refs") or []
        if label in request_labels:
            request_windows.update(
                str(evidence_by_ref[str(ref)].get("window_id") or "")
                for ref in refs
                if str(ref) in evidence_by_ref
            )
            request_evidence.extend(
                evidence_by_ref[str(ref)] for ref in refs if str(ref) in evidence_by_ref
            )
        if label in terminal_labels:
            terminal_evidence.extend(
                evidence_by_ref[str(ref)] for ref in refs if str(ref) in evidence_by_ref
            )
    return supports_bounded_gap(
        terminal_evidence,
        request_window_ids=request_windows - {""},
        window_order=window_order,
        request_evidence=request_evidence,
    )


def _uses_category_attribution(observation: Mapping[str, Any]) -> bool:
    evidence_by_ref, _ = _proof_context(observation)
    requests: list[Mapping[str, Any]] = []
    terminals: list[Mapping[str, Any]] = []
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        entries = [
            evidence_by_ref[str(ref)]
            for ref in arc.get("evidence_refs") or []
            if str(ref) in evidence_by_ref
        ]
        if label in {"request", "expectation", "verification_request"}:
            requests.extend(entries)
        if label in {"outcome", "gap", "delivery", "verification_result"}:
            terminals.extend(entries)
    return any(result_uses_category_attribution(item, requests) for item in terminals)


def _arc_evidence(
    observation: Mapping[str, Any],
    labels: set[str],
) -> tuple[list[Mapping[str, Any]], set[str], dict[str, int], list[Mapping[str, Any]]]:
    evidence_by_ref, window_order = _proof_context(observation)
    request_windows: set[str] = set()
    request_evidence: list[Mapping[str, Any]] = []
    selected: list[Mapping[str, Any]] = []
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        refs = arc.get("evidence_refs") or []
        if label in {"request", "expectation", "verification_request", "skill_request"}:
            request_windows.update(
                str(evidence_by_ref[str(ref)].get("window_id") or "")
                for ref in refs
                if str(ref) in evidence_by_ref
            )
            request_evidence.extend(
                evidence_by_ref[str(ref)] for ref in refs if str(ref) in evidence_by_ref
            )
        if label in labels:
            selected.extend(evidence_by_ref[str(ref)] for ref in refs if str(ref) in evidence_by_ref)
    return selected, request_windows - {""}, window_order, request_evidence


def _successful_arc_evidence(observation: Mapping[str, Any], labels: set[str]) -> bool:
    selected, request_windows, window_order, request_evidence = _arc_evidence(observation, labels)
    return supports_successful_result(
        selected,
        request_window_ids=request_windows,
        window_order=window_order,
        request_evidence=request_evidence,
    )


def _verification_arc_evidence(observation: Mapping[str, Any]) -> bool:
    selected, request_windows, window_order, request_evidence = _arc_evidence(observation, {"verification_result"})
    return supports_verification_result(
        selected,
        request_window_ids=request_windows,
        window_order=window_order,
        request_evidence=request_evidence,
    )


def _skill_arc_evidence(observation: Mapping[str, Any]) -> bool:
    evidence_by_ref, _ = _proof_context(observation)
    entries: dict[str, list[Mapping[str, Any]]] = {
        "skill_request": [], "skill_evidence": [], "skill_action": [],
    }
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        if label in entries:
            entries[label].extend(
                evidence_by_ref[str(ref)]
                for ref in arc.get("evidence_refs") or []
                if str(ref) in evidence_by_ref
            )
    return supports_skill_action(
        entries["skill_action"],
        skill_evidence=entries["skill_evidence"],
        request_evidence=entries["skill_request"],
    )


def _process_fact_arc_evidence(observation: Mapping[str, Any]) -> bool:
    evidence_by_ref, _ = _proof_context(observation)
    entries: dict[str, list[Mapping[str, Any]]] = {"action": [], "artifact": []}
    for arc in observation.get("proof_arcs", []):
        if not isinstance(arc, Mapping):
            continue
        label = _normalized_part(arc.get("arc"))
        if label in entries:
            entries[label].extend(
                evidence_by_ref[str(ref)]
                for ref in arc.get("evidence_refs") or []
                if str(ref) in evidence_by_ref
            )
    action = entries["action"]
    artifact = entries["artifact"]
    action_ids = {
        str(item.get("tool_event_id") or "")
        for item in action
        if str(item.get("tool_event_id") or "")
    }
    artifact_ids = {
        str(item.get("tool_event_id") or "")
        for item in artifact
        if str(item.get("tool_event_id") or "")
    }
    return bool(
        action
        and artifact
        and action_ids & artifact_ids
        and all(is_successful_artifact_result(item) for item in action + artifact)
    )


def _observation_terminal_proof(observation: Mapping[str, Any]) -> bool:
    kind = str(observation.get("kind") or "")
    if kind == "instruction_follow":
        return has_completion_evidence(observation)
    if kind == "verification":
        return _verification_arc_evidence(observation)
    if kind in {"instruction_miss", "delivery_gap"}:
        return has_bounded_gap_evidence(observation)
    if kind == "skill_use":
        return _skill_arc_evidence(observation)
    if kind == "process_fact":
        return _process_fact_arc_evidence(observation)
    return kind == "repeated_ask"


def _miss_proof_arc(observation: Mapping[str, Any], arc_name: str) -> bool:
    kind = str(observation.get("kind") or "")
    allowed = {
        "instruction_miss": {"gap"},
        "delivery_gap": {"delivery"},
        "repeated_ask": {"request_2"},
    }
    if arc_name not in allowed.get(kind, set()):
        return False
    arcs = [arc for arc in observation.get("proof_arcs", []) if isinstance(arc, Mapping)]
    selected = next((arc for arc in arcs if _normalized_part(arc.get("arc")) == arc_name), None)
    if selected is None:
        return False
    if kind == "repeated_ask":
        return True
    evidence_by_ref, window_order = _proof_context(observation)
    request_label = "request" if kind == "instruction_miss" else "expectation"
    request_refs = next(
        (
            arc.get("evidence_refs") or []
            for arc in arcs
            if _normalized_part(arc.get("arc")) == request_label
        ),
        [],
    )
    request_windows = {
        str(evidence_by_ref[str(ref)].get("window_id") or "")
        for ref in request_refs
        if str(ref) in evidence_by_ref
    } - {""}
    request_evidence = [
        evidence_by_ref[str(ref)]
        for ref in request_refs
        if str(ref) in evidence_by_ref
    ]
    selected_evidence = [
        evidence_by_ref[str(ref)]
        for ref in selected.get("evidence_refs") or []
        if str(ref) in evidence_by_ref
    ]
    return supports_bounded_gap(
        selected_evidence,
        request_window_ids=request_windows,
        window_order=window_order,
        request_evidence=request_evidence,
    )


def _distribution_from_ids(ids: Iterable[str], observations: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, int], int]:
    by_root: dict[str, Mapping[str, Any]] = {}
    for observation_id in ids:
        observation = observations.get(observation_id)
        if observation is None:
            continue
        root = str(observation.get("root_session_id") or "")
        if root:
            by_root.setdefault(root, observation)
    harness: dict[str, int] = {}
    repo: dict[str, int] = {}
    for observation in by_root.values():
        h = _normalized_text(observation.get("harness")) or "(unknown)"
        r = _normalized_text(observation.get("repo")) or "(unknown)"
        harness[h] = harness.get(h, 0) + 1
        repo[r] = repo.get(r, 0) + 1
    return dict(sorted(harness.items())), dict(sorted(repo.items())), len(by_root)


def _rewrite_signature(candidate: Mapping[str, Any]) -> str:
    return _canonical_json(
        {
            key: candidate.get(key)
            for key in (
                "kind", "canonical_key", "title", "summary", "instruction_text", "does_not_prove",
                "pattern_canonical_key", "target_ref", "config_target_map_hash", "target_kind", "action",
                "base_content_hash", "config_gap",
                "processed_roots", "eligible_roots",
                "population_hash", "cited_supporting_roots", "counterevidence_roots",
                "counterevidence_observations",
                "source_packet_ids", "source_packet_coverage", "source_packet_sampling",
                "source_packet_group_membership", "source_packet_population",
            )
        }
    )


def validate_terra_result(
    raw: Any,
    packet: Mapping[str, Any],
    *,
    config_targets: Any = None,
) -> tuple[dict[str, Any] | None, list[SynthesisValidationFailure]]:
    """Hard-validate bounded Terra output against one immutable packet."""
    failures: list[SynthesisValidationFailure] = []
    if not isinstance(raw, Mapping):
        return None, [SynthesisValidationFailure("result_not_object")]
    packet_id = str(packet.get("packet_id") or "")
    if not packet_id or str(raw.get("packet_id") or "") != packet_id:
        failures.append(SynthesisValidationFailure("packet_id_mismatch"))
    if not _synthesis_packet_hash_valid(packet):
        failures.append(SynthesisValidationFailure("synthesis_packet_hash_mismatch"))
    try:
        corpus_snapshot, corpus_snapshot_hash = _corpus_snapshot(packet)
    except ValueError:
        corpus_snapshot, corpus_snapshot_hash = {}, ""
        failures.append(SynthesisValidationFailure("synthesis_packet_snapshot_mismatch"))
    expected_producer = packet.get("synthesis_assignment")
    if not _assignment_matches(raw.get("producer"), expected_producer):
        failures.append(SynthesisValidationFailure("synthesis_producer_assignment_mismatch"))
    result_id = _normalized_text(raw.get("result_id"))
    if not result_id:
        failures.append(SynthesisValidationFailure("missing_result_id"))
    if raw.get("abstain") is True:
        if not _plain_text(raw.get("abstain_reason")):
            failures.append(SynthesisValidationFailure("missing_abstain_reason"))
        if raw.get("candidates") not in (None, [], ()):
            failures.append(SynthesisValidationFailure("abstention_has_candidates"))
        if failures:
            return None, failures
        return {
            "packet_id": packet_id,
            "result_id": result_id,
            "producer": dict(expected_producer),
            "review_assignment": dict(packet.get("review_assignment") or {}),
            "abstain": True,
            "abstain_reason": _normalized_text(raw.get("abstain_reason")),
            "observation_index": {},
            "coverage": dict(packet.get("coverage") or {}),
            "provenance": dict(packet.get("provenance") or {}),
            "packet_hash": str(packet.get("packet_hash") or ""),
            "corpus_snapshot": corpus_snapshot,
            "corpus_snapshot_hash": corpus_snapshot_hash,
        }, []
    if raw.get("abstain") is not False:
        failures.append(SynthesisValidationFailure("abstain_not_boolean"))
    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        failures.append(SynthesisValidationFailure("candidates_not_nonempty_list"))
        return None, failures
    observations, support_ids, counterexample_ids = _packet_observation_index(packet)
    full_population = _full_population_from_packet(packet)
    if full_population is None:
        failures.append(SynthesisValidationFailure("packet_full_population_invalid"))
        return None, failures
    if (
        not support_ids <= set(full_population["supporting"]["observation_ids"])
        or not counterexample_ids <= set(full_population["counterexamples"]["observation_ids"])
    ):
        failures.append(SynthesisValidationFailure("packet_full_population_membership_mismatch"))
        return None, failures
    denominator = _positive_int((packet.get("coverage") or {}).get("full_eligible_root_denominator"))
    processed_roots = _nonnegative_int((packet.get("coverage") or {}).get("processed_roots"))
    if denominator is None or processed_roots is None:
        failures.append(SynthesisValidationFailure("packet_missing_full_denominator"))
        return None, failures
    cleaned: list[dict[str, Any]] = []
    canonical_seen: dict[str, str] = {}
    semantic_seen: dict[str, str] = {}
    for index, raw_candidate in enumerate(candidates):
        if not isinstance(raw_candidate, Mapping):
            failures.append(SynthesisValidationFailure("candidate_not_object", index)); continue
        kind = str(raw_candidate.get("kind") or "")
        if kind not in CANDIDATE_KINDS:
            failures.append(SynthesisValidationFailure("unknown_candidate_kind", index)); continue
        canonical, canonical_key = _canonical(raw_candidate)
        if canonical is None or canonical_key is None or not _SCOPE.match(canonical["scope"]):
            failures.append(SynthesisValidationFailure("invalid_canonical", index)); continue
        raw_key = raw_candidate.get("canonical_key")
        if raw_key is not None and str(raw_key) != canonical_key:
            failures.append(SynthesisValidationFailure("canonical_key_mismatch", index)); continue
        title = _candidate_text(raw_candidate, "title")
        summary = _candidate_text(raw_candidate, "summary")
        limitation = _candidate_text(raw_candidate, "does_not_prove")
        instruction = _candidate_text(raw_candidate, "instruction_text", required=kind == "coach_proposal")
        if title is None or summary is None or limitation is None or instruction is None:
            failures.append(SynthesisValidationFailure("candidate_text_missing_or_bulleted", index)); continue
        if _text_has_sentiment(title, summary, limitation, instruction, canonical_key):
            failures.append(SynthesisValidationFailure("sentiment_forbidden", index)); continue
        support = raw_candidate.get("supporting_observation_ids")
        counter = raw_candidate.get("counterevidence_observation_ids")
        if counter is None:
            counter = raw_candidate.get("counterexample_observation_ids")
        if not isinstance(support, list) or not support:
            failures.append(SynthesisValidationFailure("missing_supporting_observation_ids", index)); continue
        if not isinstance(counter, list):
            failures.append(SynthesisValidationFailure("missing_counterevidence_observation_ids", index)); continue
        support_list = [str(value) for value in support]
        counter_list = [str(value) for value in counter]
        if len(set(support_list)) != len(support_list) or len(set(counter_list)) != len(counter_list):
            failures.append(SynthesisValidationFailure("duplicate_observation_reference", index)); continue
        if not set(support_list) <= support_ids:
            failures.append(SynthesisValidationFailure("unknown_supporting_observation_id", index)); continue
        if not set(counter_list) <= counterexample_ids:
            failures.append(SynthesisValidationFailure("unknown_counterevidence_observation_id", index)); continue
        if set(support_list) & set(counter_list):
            failures.append(SynthesisValidationFailure("observation_is_support_and_counterevidence", index)); continue
        if kind in {"corpus_pattern", "coach_proposal"} and counterexample_ids and not counter_list:
            failures.append(SynthesisValidationFailure("same_theme_counterevidence_omitted", index)); continue
        if counter_list and not re.search(r"\b(?:counter\w*|contrast\w*|mixed|opposite)\b", limitation, re.IGNORECASE):
            failures.append(SynthesisValidationFailure("counterevidence_not_reflected_in_limitation", index)); continue
        cited_harnesses, cited_repos, cited_n = _distribution_from_ids(support_list, observations)
        full_support = full_population["supporting"]
        full_counter = full_population["counterexamples"]
        if kind != "observed_instance":
            if canonical["scope"] != _packet_scope(packet):
                failures.append(SynthesisValidationFailure("candidate_scope_population_mismatch", index)); continue
            if not _full_population_matches_scope(full_support, canonical["scope"]):
                reason = (
                    "model_scope_full_population_mismatch"
                    if canonical["scope"].startswith("model_")
                    else "scope_full_population_mismatch"
                )
                failures.append(SynthesisValidationFailure(reason, index)); continue
        if kind == "observed_instance":
            harnesses, repos, n = cited_harnesses, cited_repos, cited_n
            counter_n = _distribution_from_ids(counter_list, observations)[2]
            population_hash = ""
        else:
            population_hash = str(raw_candidate.get("population_hash") or "")
            if population_hash != str(full_population["hash"]):
                failures.append(SynthesisValidationFailure("candidate_population_hash_mismatch", index)); continue
            harnesses = dict(full_support["distribution"]["harnesses"])
            repos = dict(full_support["distribution"]["repos"])
            n = int(full_support["root_count"])
            counter_n = int(full_counter["root_count"])
            if _nonnegative_int(raw_candidate.get("cited_supporting_roots")) != cited_n:
                failures.append(SynthesisValidationFailure("dishonest_cited_supporting_roots", index)); continue
            if _nonnegative_int(raw_candidate.get("counterevidence_roots")) != counter_n:
                failures.append(SynthesisValidationFailure("dishonest_counterevidence_roots", index)); continue
            if _nonnegative_int(raw_candidate.get("counterevidence_observations")) != int(full_counter["observation_count"]):
                failures.append(SynthesisValidationFailure("dishonest_counterevidence_observations", index)); continue
        declared_n = _positive_int(raw_candidate.get("n"))
        declared_denominator = _positive_int(raw_candidate.get("denominator"))
        declared_processed = _nonnegative_int(raw_candidate.get("processed_roots"))
        declared_eligible = _positive_int(raw_candidate.get("eligible_roots"))
        if declared_n != n:
            failures.append(SynthesisValidationFailure("dishonest_n", index)); continue
        if declared_denominator != denominator:
            failures.append(SynthesisValidationFailure("dishonest_full_denominator", index)); continue
        if declared_processed != processed_roots or declared_eligible != denominator:
            failures.append(SynthesisValidationFailure("dishonest_processing_coverage", index)); continue
        if n > processed_roots or processed_roots > denominator:
            failures.append(SynthesisValidationFailure("n_exceeds_processing_coverage", index)); continue
        if kind == "coach_proposal" and processed_roots < denominator:
            failures.append(SynthesisValidationFailure("proposal_requires_complete_processing", index)); continue
        if kind == "coach_proposal":
            capabilities = (packet.get("coverage") or {}).get("proof_capability_by_harness")
            if not isinstance(capabilities, Mapping):
                failures.append(SynthesisValidationFailure("proposal_proof_capability_unbound", index)); continue
            support_harnesses = set(harnesses)
            if any(
                not isinstance(capabilities.get(harness), Mapping)
                or str(capabilities[harness].get("adapter_capability") or capabilities[harness].get("capability") or "") != "supported"
                or int(capabilities[harness].get("processed_roots") or 0) < int(capabilities[harness].get("eligible_roots") or 0)
                for harness in support_harnesses
            ):
                failures.append(SynthesisValidationFailure("proposal_requires_supported_adapter_capability", index)); continue
        if not _candidate_links_assertion_theme(canonical, support_list, observations):
            failures.append(SynthesisValidationFailure("candidate_not_linked_to_assertion_theme", index)); continue
        if not _contextual_card_text(
            title,
            summary,
            limitation,
            instruction,
            kind=kind,
            n=n,
            processed_roots=processed_roots,
            denominator=denominator,
            cited_roots=cited_n,
            support_is_sampled=cited_n < n,
        ):
            failures.append(SynthesisValidationFailure("candidate_text_too_thin_or_unbounded", index)); continue
        if any(_uses_category_attribution(observations[observation_id]) for observation_id in support_list) and not re.search(
            r"(?:\b(?:exact|specific)\s+(?:target|suite|task)\b.*\b(?:not|unproven|cannot)\b|\b(?:not|unproven|cannot)\b.*\b(?:exact|specific)\s+(?:target|suite|task)\b)",
            limitation,
            re.IGNORECASE,
        ):
            failures.append(SynthesisValidationFailure("category_attribution_target_unproven_missing", index)); continue
        if not all(_observation_terminal_proof(observations[observation_id]) for observation_id in support_list):
            failures.append(SynthesisValidationFailure("candidate_missing_typed_terminal_proof", index)); continue
        if kind == "observed_instance" and n != 1:
            failures.append(SynthesisValidationFailure("instance_requires_one_root", index)); continue
        if kind == "corpus_pattern" and n < 5:
            failures.append(SynthesisValidationFailure("pattern_requires_n_ge_5", index)); continue
        miss_arcs: list[dict[str, str]] = []
        proposal_fields: dict[str, str] = {}
        if kind == "coach_proposal":
            if n < 10:
                failures.append(SynthesisValidationFailure("proposal_requires_n_ge_10", index)); continue
            pattern_key = _normalized_text(raw_candidate.get("pattern_canonical_key"))
            target_ref = _normalized_text(raw_candidate.get("target_ref"))
            target_kind = _normalized_part(raw_candidate.get("target_kind"))
            action = _normalized_part(raw_candidate.get("action"))
            base_content_hash = _normalized_text(raw_candidate.get("base_content_hash"))
            raw_config_gap = raw_candidate.get("config_gap")
            config_overlap = packet.get("config_overlap")
            searched = config_overlap.get("searched") if isinstance(config_overlap, Mapping) else None
            target_record = next(
                (
                    item for item in searched or []
                    if isinstance(item, Mapping) and str(item.get("target_ref") or "") == target_ref
                ),
                None,
            )
            if (
                not pattern_key or not _SCOPE.match(pattern_key)
                or not target_ref or not target_kind
                or action != "add"
                or not base_content_hash or not isinstance(raw_config_gap, Mapping)
            ):
                failures.append(SynthesisValidationFailure("proposal_config_contract_invalid", index)); continue
            if target_record is None:
                failures.append(SynthesisValidationFailure("proposal_target_not_in_config_search", index)); continue
            if str(target_record.get("fingerprint") or "") != base_content_hash:
                failures.append(SynthesisValidationFailure("proposal_base_hash_mismatch", index)); continue
            expected_target_map_hash = str(packet.get("config_target_map_hash") or "")
            actual_target_map_hash = _sha256(config_targets) if config_targets is not None else ""
            if not expected_target_map_hash or actual_target_map_hash != expected_target_map_hash:
                failures.append(SynthesisValidationFailure("proposal_config_target_map_hash_mismatch", index)); continue
            target = _config_target_index(config_targets).get(target_ref)
            if target is None:
                failures.append(SynthesisValidationFailure("proposal_config_target_map_required", index)); continue
            if target["fingerprint"] != base_content_hash:
                failures.append(SynthesisValidationFailure("proposal_private_target_hash_mismatch", index)); continue
            if target_kind not in {"instruction_file", "skill", "harness_rule"}:
                failures.append(SynthesisValidationFailure("proposal_target_kind_invalid", index)); continue
            if target_kind != target["target_kind"]:
                failures.append(SynthesisValidationFailure("proposal_target_kind_mismatch", index)); continue
            selected_matches = [
                item for item in (config_overlap or {}).get("matches", [])
                if isinstance(item, Mapping) and str(item.get("target_ref") or "") == target_ref
            ]
            if selected_matches:
                failures.append(SynthesisValidationFailure("proposal_target_has_existing_config_overlap", index)); continue
            if _text_has_sentiment(pattern_key, target_kind):
                failures.append(SynthesisValidationFailure("proposal_config_contract_sentiment", index)); continue
            derived_gap = {
                "available": bool((config_overlap or {}).get("available")),
                "searched": [
                    {
                        "target_ref": str(item.get("target_ref") or ""),
                        "fingerprint": str(item.get("fingerprint") or ""),
                        "target_kind": str(item.get("target_kind") or ""),
                    }
                    for item in searched or []
                    if isinstance(item, Mapping)
                ],
                "matches": [
                    dict(item) for item in (config_overlap or {}).get("matches", [])
                    if isinstance(item, Mapping)
                ],
                "selected_target": {
                    "target_ref": target_ref,
                    "fingerprint": base_content_hash,
                    "target_kind": target["target_kind"],
                },
            }
            proposal_fields = {
                "pattern_canonical_key": pattern_key,
                "target_ref": target_ref,
                "config_target_map_hash": expected_target_map_hash,
                "target_kind": target["target_kind"],
                "action": action,
                "base_content_hash": base_content_hash,
                "config_gap": derived_gap,
            }
            raw_arcs = raw_candidate.get("miss_proof_arcs")
            if not isinstance(raw_arcs, list):
                failures.append(SynthesisValidationFailure("proposal_missing_miss_proof_arcs", index)); continue
            seen_arcs: set[tuple[str, str]] = set()
            for arc in raw_arcs:
                if not isinstance(arc, Mapping):
                    failures.append(SynthesisValidationFailure("malformed_miss_proof_arc", index)); continue
                observation_id = str(arc.get("observation_id") or "")
                arc_name = _normalized_part(arc.get("arc"))
                if observation_id not in support_list or not _miss_proof_arc(observations[observation_id], arc_name):
                    failures.append(SynthesisValidationFailure("unverified_miss_proof_arc", index)); continue
                seen_arcs.add((observation_id, arc_name))
            if len(seen_arcs) < 3:
                failures.append(SynthesisValidationFailure("proposal_requires_three_miss_proof_arcs", index)); continue
            if len({str(observations[observation_id].get("root_session_id") or "") for observation_id, _ in seen_arcs} - {""}) < 3:
                failures.append(SynthesisValidationFailure("proposal_miss_proof_arcs_not_independent", index)); continue
            miss_arcs = [{"observation_id": oid, "arc": arc} for oid, arc in sorted(seen_arcs)]
        if canonical["scope"].startswith("model_"):
            expected_model = canonical["scope"][len("model_"):]
            if any(
                bool((observations[observation_id].get("model_attribution") or {}).get("ambiguous"))
                for observation_id in support_list
            ):
                failures.append(SynthesisValidationFailure("model_scope_terminal_attribution_ambiguous", index)); continue
            supporting_models = {
                _normalized_part((observations[observation_id].get("model_attribution") or {}).get("response_model"))
                for observation_id in support_list
            } - {""}
            if supporting_models != {expected_model}:
                failures.append(SynthesisValidationFailure("model_scope_not_supported_by_evidence", index)); continue
        if canonical["scope"] in {"global", "corpus", "global_corpus"}:
            largest_harness = max(harnesses.values(), default=0) / n
            largest_repo = max(repos.values(), default=0) / n
            if (
                n < 15 or len(harnesses) < 2
                or "(unknown)" in harnesses or "(unknown)" in repos
                or largest_harness > 0.70 or largest_repo > 0.70
            ):
                failures.append(SynthesisValidationFailure("global_routing_gate_failed", index)); continue
        candidate_id = f"coach_{kind}_{_short_hash(canonical_key)}"
        candidate = {
            "candidate_id": candidate_id,
            "kind": kind,
            "canonical": canonical,
            "canonical_key": canonical_key,
            "title": title,
            "summary": summary,
            "instruction_text": instruction,
            "does_not_prove": limitation,
            "supporting_observation_ids": support_list,
            "counterevidence_observation_ids": counter_list,
            "n": n,
            "cited_supporting_roots": cited_n,
            "counterevidence_roots": counter_n,
            "counterevidence_observations": int(full_counter["observation_count"])
            if kind != "observed_instance" else len(counter_list),
            "population_hash": population_hash,
            "denominator": denominator,
            "processed_roots": processed_roots,
            "eligible_roots": denominator,
            "distribution": {"harnesses": harnesses, "repos": repos},
            "miss_proof_arcs": miss_arcs,
            "source_packet_ids": [packet_id],
            "source_packet_coverage": dict(packet.get("coverage") or {}),
            "source_packet_sampling": dict(packet.get("sampling") or {}),
            "source_packet_group_membership": dict(packet.get("group_membership") or {}),
            "source_packet_population": dict(full_population),
            **proposal_fields,
        }
        signature = _rewrite_signature(candidate)
        identity = f"{kind}:{canonical_key}"
        semantic_identity = _semantic_canonical_key(kind, canonical)
        prior_semantic = semantic_seen.get(semantic_identity)
        if prior_semantic is not None and prior_semantic != canonical_key:
            failures.append(SynthesisValidationFailure("semantically_duplicate_canonical_rewrite", index, candidate_id)); continue
        prior = canonical_seen.get(identity)
        if prior is not None and prior != signature:
            failures.append(SynthesisValidationFailure("conflicting_canonical_rewrite", index, candidate_id)); continue
        if prior == signature:
            continue
        canonical_seen[identity] = signature
        semantic_seen[semantic_identity] = canonical_key
        cleaned.append(candidate)
    if failures:
        return None, failures
    catalog_observation_index = {
        observation_id: {
            **dict(observation),
            "source_synthesis_packet_id": packet_id,
            "source_synthesis_packet_hash": str(packet.get("packet_hash") or ""),
            "source_synthesis_packet_ids": [packet_id],
            "source_synthesis_packets": [
                {
                    "packet_id": packet_id,
                    "packet_hash": str(packet.get("packet_hash") or ""),
                }
            ],
        }
        for observation_id, observation in observations.items()
    }
    return {
        "packet_id": packet_id,
        "result_id": result_id,
        "producer": dict(expected_producer),
        "review_assignment": dict(packet.get("review_assignment") or {}),
        "abstain": False,
        "candidates": cleaned,
        "observation_index": catalog_observation_index,
        "coverage": dict(packet.get("coverage") or {}),
        "provenance": dict(packet.get("provenance") or {}),
        "packet_hash": str(packet.get("packet_hash") or ""),
        "corpus_snapshot": corpus_snapshot,
        "corpus_snapshot_hash": corpus_snapshot_hash,
    }, []


validate_synthesis_result = validate_terra_result


def _merge_candidate(previous: dict[str, Any], current: Mapping[str, Any]) -> dict[str, Any] | None:
    if _rewrite_signature(previous) != _rewrite_signature(current):
        return None
    exact_fields = (
        "supporting_observation_ids", "counterevidence_observation_ids", "miss_proof_arcs",
        "n", "denominator", "distribution", "source_packet_ids", "source_packet_coverage",
        "source_packet_sampling", "source_packet_group_membership",
        "population_hash", "cited_supporting_roots", "counterevidence_roots",
        "counterevidence_observations", "source_packet_population",
    )
    if any(previous.get(field) != current.get(field) for field in exact_fields):
        return None
    return dict(previous)


def _merge_catalog_observation(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> dict[str, Any] | None:
    membership_fields = {
        "source_synthesis_packet_id",
        "source_synthesis_packet_hash",
        "source_synthesis_packet_ids",
        "source_synthesis_packets",
    }
    prior_body = {key: value for key, value in previous.items() if key not in membership_fields}
    current_body = {key: value for key, value in current.items() if key not in membership_fields}
    if _canonical_json(prior_body) != _canonical_json(current_body):
        return None
    members: dict[str, str] = {}
    for observation in (previous, current):
        entries = observation.get("source_synthesis_packets")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, Mapping) and str(entry.get("packet_id") or ""):
                    members[str(entry["packet_id"])] = str(entry.get("packet_hash") or "")
        packet_id = str(observation.get("source_synthesis_packet_id") or "")
        if packet_id:
            members.setdefault(packet_id, str(observation.get("source_synthesis_packet_hash") or ""))
    if not members:
        return None
    memberships = [
        {"packet_id": packet_id, "packet_hash": members[packet_id]}
        for packet_id in sorted(members)
    ]
    primary = memberships[0]
    return {
        **prior_body,
        "source_synthesis_packet_id": primary["packet_id"],
        "source_synthesis_packet_hash": primary["packet_hash"],
        "source_synthesis_packet_ids": [entry["packet_id"] for entry in memberships],
        "source_synthesis_packets": memberships,
    }


def build_candidate_catalog(
    synthesis_manifest: Mapping[str, Any] | Path | str,
    validated_results: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[SynthesisValidationFailure]]:
    """Collapse duplicate rewrites and reject canonical conflicts across packets."""
    manifest = _manifest_from(synthesis_manifest)
    try:
        corpus_snapshot, corpus_snapshot_hash = _corpus_snapshot(manifest)
    except ValueError:
        return None, [SynthesisValidationFailure("catalog_snapshot_lineage_missing")]
    candidates: dict[str, dict[str, Any]] = {}
    observation_index: dict[str, dict[str, Any]] = {}
    coverage_by_packet: dict[str, dict[str, Any]] = {}
    packet_provenance: dict[str, dict[str, Any]] = {}
    synthesis_assignments: dict[str, dict[str, Any]] = {}
    review_assignments: dict[str, dict[str, Any]] = {}
    luna_producers: dict[str, dict[str, Any]] = {}
    failures: list[SynthesisValidationFailure] = []
    for result in validated_results:
        if not isinstance(result, Mapping):
            failures.append(SynthesisValidationFailure("catalog_result_not_object")); continue
        packet_id = str(result.get("packet_id") or "")
        producer = result.get("producer")
        review_assignment = result.get("review_assignment")
        if not isinstance(producer, Mapping) or not isinstance(review_assignment, Mapping):
            failures.append(SynthesisValidationFailure("catalog_result_assignment_missing")); continue
        synthesis_assignments[_canonical_json(producer)] = dict(producer)
        review_assignments[_canonical_json(review_assignment)] = dict(review_assignment)
        if (
            str(result.get("corpus_snapshot_hash") or "") != corpus_snapshot_hash
            or _canonical_json(result.get("corpus_snapshot") or {}) != _canonical_json(corpus_snapshot)
        ):
            failures.append(SynthesisValidationFailure("catalog_snapshot_lineage_mismatch")); continue
        for observation_id, observation in (result.get("observation_index") or {}).items():
            if not isinstance(observation, Mapping):
                failures.append(SynthesisValidationFailure("catalog_observation_not_object")); continue
            existing = observation_index.get(str(observation_id))
            if existing is None:
                observation_index[str(observation_id)] = dict(observation)
                luna = observation.get("luna_producer")
                if isinstance(luna, Mapping) and luna:
                    luna_producers[_canonical_json(luna)] = dict(luna)
                continue
            merged_observation = _merge_catalog_observation(existing, observation)
            if merged_observation is None:
                failures.append(SynthesisValidationFailure("catalog_observation_identity_conflict")); continue
            observation_index[str(observation_id)] = merged_observation
        coverage = result.get("coverage")
        if isinstance(coverage, Mapping):
            coverage_by_packet[packet_id] = dict(coverage)
        provenance = result.get("provenance")
        if isinstance(provenance, Mapping):
            packet_provenance[packet_id] = {
                "packet_hash": str(result.get("packet_hash") or ""),
                "result_id": str(result.get("result_id") or ""),
                "producer": dict(producer),
                "review_assignment": dict(review_assignment),
                **dict(provenance),
            }
        if result.get("abstain"):
            continue
        for item in result.get("candidates", []):
            if not isinstance(item, Mapping):
                failures.append(SynthesisValidationFailure("catalog_candidate_not_object")); continue
            candidate = dict(item)
            canonical_key = str(candidate.get("canonical_key") or "")
            kind = str(candidate.get("kind") or "")
            key = f"{kind}:{canonical_key}"
            if not canonical_key or kind not in CANDIDATE_KINDS or not str(candidate.get("candidate_id") or ""):
                failures.append(SynthesisValidationFailure("catalog_candidate_missing_identity")); continue
            prior = candidates.get(key)
            if prior is None:
                candidates[key] = candidate
                continue
            merged = _merge_candidate(prior, candidate)
            if merged is None:
                failures.append(SynthesisValidationFailure("conflicting_canonical_rewrite", candidate_id=str(candidate.get("candidate_id"))))
            else:
                candidates[key] = merged
    pattern_keys = {
        str(candidate.get("canonical_key") or "")
        for candidate in candidates.values()
        if candidate.get("kind") == "corpus_pattern"
    }
    semantic_catalog: dict[str, str] = {}
    for candidate in candidates.values():
        canonical = candidate.get("canonical")
        if not isinstance(canonical, Mapping):
            failures.append(SynthesisValidationFailure("catalog_candidate_canonical_missing")); continue
        semantic_identity = _semantic_canonical_key(str(candidate.get("kind") or ""), canonical)
        canonical_key = str(candidate.get("canonical_key") or "")
        prior_key = semantic_catalog.get(semantic_identity)
        if prior_key is not None and prior_key != canonical_key:
            failures.append(SynthesisValidationFailure("semantically_duplicate_canonical_rewrite", candidate_id=str(candidate.get("candidate_id") or "")))
            continue
        semantic_catalog[semantic_identity] = canonical_key
    if len(synthesis_assignments) > 1 or len(review_assignments) > 1:
        failures.append(SynthesisValidationFailure("catalog_assignment_conflict"))
    target_map_binding = manifest.get("config_target_map")
    target_map_hash = str(target_map_binding.get("hash") or "") if isinstance(target_map_binding, Mapping) else ""
    for candidate in candidates.values():
        if candidate.get("kind") == "coach_proposal" and str(candidate.get("pattern_canonical_key") or "") not in pattern_keys:
            failures.append(
                SynthesisValidationFailure(
                    "proposal_pattern_reference_missing",
                    candidate_id=str(candidate.get("candidate_id") or ""),
                )
            )
        if candidate.get("kind") == "coach_proposal" and (
            not target_map_hash
            or str(candidate.get("config_target_map_hash") or "") != target_map_hash
        ):
            failures.append(
                SynthesisValidationFailure(
                    "proposal_config_target_binding_missing",
                    candidate_id=str(candidate.get("candidate_id") or ""),
                )
            )
    if failures:
        return None, failures
    catalog_body = {
        "schema_version": CATALOG_VERSION,
        "source_synthesis_manifest_hash": _sha256(manifest),
        "corpus_snapshot_hash": corpus_snapshot_hash,
        "corpus_snapshot": corpus_snapshot,
        "synthesis_assignment": next(iter(synthesis_assignments.values()), {}),
        "review_assignment": next(iter(review_assignments.values()), {}),
        "luna_producers": [luna_producers[key] for key in sorted(luna_producers)],
        "candidates": [candidates[key] for key in sorted(candidates)],
        "observation_index": {key: observation_index[key] for key in sorted(observation_index)},
        "coverage": {key: coverage_by_packet[key] for key in sorted(coverage_by_packet)},
        "provenance": {
            "packets": {key: packet_provenance[key] for key in sorted(packet_provenance)},
            "private_config_target_map": dict(manifest.get("config_target_map") or {}),
        },
    }
    catalog_body["catalog_id"] = f"catalog_{_short_hash(catalog_body)}"
    catalog_body["catalog_hash"] = _sha256(catalog_body)
    return catalog_body, []


def emit_candidate_catalog(
    run_dir: Path | str,
    synthesis_manifest: Mapping[str, Any] | Path | str,
    validated_results: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[SynthesisValidationFailure]]:
    """Emit only a fully validated candidate catalog under the current run."""
    catalog, failures = build_candidate_catalog(synthesis_manifest, validated_results)
    if catalog is None:
        return None, failures
    target = assert_writable(Path(run_dir) / "candidate_catalog.json", purpose="coach candidate catalog")
    write_text(target, json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    return catalog, []


def _write_run_bundle(
    target: Path,
    synthesis_manifest: Mapping[str, Any],
    *,
    validated_results: Sequence[Mapping[str, Any]] | None = None,
    catalog: Mapping[str, Any] | None = None,
    second_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result_entry: dict[str, Any] | None = None
    if validated_results is not None:
        result_path = target / "synthesis_results" / "validated_results.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_body = {
            "schema_version": TERRA_RESULT_VERSION,
            "results": [dict(result) for result in validated_results],
        }
        result_body["results_hash"] = _sha256(result_body)
        assert_writable(result_path, purpose="coach validated Terra results")
        write_text(result_path, json.dumps(result_body, indent=2, ensure_ascii=False) + "\n")
        result_entry = {"path": str(result_path.relative_to(target)), "hash": result_body["results_hash"]}
    review_entry: dict[str, Any] | None = None
    if second_review is not None:
        review_path = target / "second_review.json"
        review_body = dict(second_review)
        review_body["review_hash"] = _sha256(review_body)
        assert_writable(review_path, purpose="coach second review")
        write_text(review_path, json.dumps(review_body, indent=2, ensure_ascii=False) + "\n")
        review_entry = {"path": str(review_path.relative_to(target)), "hash": review_body["review_hash"]}
    catalog_entry = (
        {"path": "candidate_catalog.json", "hash": str(catalog.get("catalog_hash") or "")}
        if catalog is not None else None
    )
    bundle = {
        "schema_version": SYNTHESIS_SCHEMA_VERSION,
        "synthesis_manifest": {
            "path": "synthesis_manifest.json",
            "hash": _sha256(synthesis_manifest),
        },
        "source_preprocess_manifest": {
            "path": "manifest.json",
            "hash": str(synthesis_manifest.get("source_manifest_hash") or ""),
        },
        "config_target_map": dict(synthesis_manifest.get("config_target_map") or {}),
        "corpus_snapshot_hash": str(synthesis_manifest.get("corpus_snapshot_hash") or ""),
        "validated_results": result_entry,
        "candidate_catalog": catalog_entry,
        "second_review": review_entry,
    }
    bundle["bundle_id"] = f"bundle_{_short_hash(bundle)}"
    bundle["bundle_hash"] = _sha256(bundle)
    bundle_path = target / "synthesis_run_bundle.json"
    assert_writable(bundle_path, purpose="coach synthesis run bundle")
    write_text(bundle_path, json.dumps(bundle, indent=2, ensure_ascii=False) + "\n")
    return bundle


def validate_second_review_result(
    raw: Any,
    catalog: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[SynthesisValidationFailure]]:
    """Validate an accept/reject review that can only reuse catalog evidence."""
    if not isinstance(raw, Mapping):
        return None, [SynthesisValidationFailure("review_not_object")]
    failures: list[SynthesisValidationFailure] = []
    catalog_id = str(catalog.get("catalog_id") or "")
    if not catalog_id or str(raw.get("catalog_id") or "") != catalog_id:
        failures.append(SynthesisValidationFailure("catalog_id_mismatch"))
    catalog_body = dict(catalog)
    declared_catalog_hash = str(catalog_body.pop("catalog_hash", "") or "")
    if not declared_catalog_hash or declared_catalog_hash != _sha256(catalog_body):
        failures.append(SynthesisValidationFailure("catalog_hash_mismatch"))
    try:
        corpus_snapshot, corpus_snapshot_hash = _corpus_snapshot(catalog)
    except ValueError:
        corpus_snapshot, corpus_snapshot_hash = {}, ""
        failures.append(SynthesisValidationFailure("review_snapshot_lineage_missing"))
    expected_reviewer = catalog.get("review_assignment")
    synthesis_producer = catalog.get("synthesis_assignment")
    if not _assignment_matches(raw.get("producer"), expected_reviewer):
        failures.append(SynthesisValidationFailure("review_producer_assignment_mismatch"))
    if isinstance(expected_reviewer, Mapping) and isinstance(synthesis_producer, Mapping) and (
        str(expected_reviewer.get("worker_id") or "")
        == str(synthesis_producer.get("worker_id") or "")
    ):
        failures.append(SynthesisValidationFailure("reviewer_not_independent"))
    review_id = _normalized_text(raw.get("review_id"))
    if not review_id:
        failures.append(SynthesisValidationFailure("missing_review_id"))
    decisions = raw.get("decisions")
    if not isinstance(decisions, list):
        failures.append(SynthesisValidationFailure("review_decisions_not_list"))
        return None, failures
    candidates = {
        str(candidate.get("candidate_id") or ""): candidate
        for candidate in catalog.get("candidates", [])
        if isinstance(candidate, Mapping)
    }
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(decisions):
        if not isinstance(item, Mapping):
            failures.append(SynthesisValidationFailure("review_decision_not_object", index)); continue
        candidate_id = str(item.get("candidate_id") or "")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            failures.append(SynthesisValidationFailure("review_unknown_candidate", index, candidate_id)); continue
        if candidate_id in seen:
            failures.append(SynthesisValidationFailure("review_duplicate_candidate", index, candidate_id)); continue
        seen.add(candidate_id)
        if str(item.get("canonical_key") or "") != str(candidate.get("canonical_key") or ""):
            failures.append(SynthesisValidationFailure("review_canonical_key_mismatch", index, candidate_id)); continue
        decision = str(item.get("decision") or "")
        if decision not in {"accept", "reject"}:
            failures.append(SynthesisValidationFailure("review_decision_invalid", index, candidate_id)); continue
        observation_ids = item.get("observation_ids")
        allowed = set(candidate.get("supporting_observation_ids") or []) | set(candidate.get("counterevidence_observation_ids") or [])
        if not isinstance(observation_ids, list) or {str(value) for value in observation_ids} != allowed:
            failures.append(SynthesisValidationFailure("review_must_reuse_exact_candidate_evidence", index, candidate_id)); continue
        reason = _plain_text(item.get("reason")) if item.get("reason") not in (None, "") else ""
        if reason is None or _text_has_sentiment(reason):
            failures.append(SynthesisValidationFailure("review_reason_invalid", index, candidate_id)); continue
        cleaned.append(
            {
                "candidate_id": candidate_id,
                "canonical_key": str(candidate["canonical_key"]),
                "decision": decision,
                "observation_ids": sorted(allowed),
                "reason": reason,
            }
        )
    if set(candidates) != seen:
        failures.append(SynthesisValidationFailure("review_must_decide_every_candidate"))
    if failures:
        return None, failures
    return {
        "schema_version": SECOND_REVIEW_VERSION,
        "catalog_id": catalog_id,
        "catalog_hash": str(catalog.get("catalog_hash") or ""),
        "corpus_snapshot_hash": corpus_snapshot_hash,
        "corpus_snapshot": corpus_snapshot,
        "review_id": review_id,
        "producer": dict(expected_reviewer),
        "decisions": sorted(cleaned, key=lambda item: item["candidate_id"]),
    }, []


validate_second_terra_review = validate_second_review_result


def _synthesis_packet_map(run_dir: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    packets: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("packets", []):
        if not isinstance(entry, Mapping):
            continue
        packet_id = str(entry.get("packet_id") or "")
        relative = str(entry.get("path") or "")
        if not packet_id or not relative:
            continue
        path = run_dir / relative
        if not path.is_file():
            continue
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(packet, dict)
            and str(packet.get("packet_id") or "") == packet_id
            and _synthesis_packet_hash_valid(packet)
        ):
            packets[packet_id] = packet
    return packets


def _terra_result_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping) and isinstance(value.get("results"), list):
        return [item for item in value["results"] if isinstance(item, dict)]
    return _result_objects(value)


def run_synthesis_pipeline(
    run_dir: Path | str,
    *,
    records: Iterable[Mapping[str, Any]] | None = None,
    coverage_manifest: Mapping[str, Any] | Path | str | None = None,
    config_inventory: Any = None,
    luna_results: Iterable[Path | str] | None = None,
    terra_results: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    second_review: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the deterministic handoff flow without calling a model or a DB.

    The caller supplies Terra JSON after packet emission.  Invalid Terra output
    never produces a catalog; an optional review is checked only after a
    catalog exists.  The returned summary contains failures instead of hiding
    an incomplete handoff.
    """
    target = Path(run_dir)
    synthesis_manifest = emit_synthesis_packets(
        target,
        records,
        coverage_manifest=coverage_manifest,
        config_inventory=config_inventory,
        luna_results=luna_results,
    )
    summary: dict[str, Any] = {
        "synthesis_manifest": synthesis_manifest,
        "validated_results": [],
        "validation_failures": [],
        "catalog": None,
        "second_review": None,
        "run_bundle": None,
    }
    if terra_results is None:
        summary["run_bundle"] = _write_run_bundle(target, synthesis_manifest)
        return summary
    packets = _synthesis_packet_map(target, synthesis_manifest)
    target_map_entry = synthesis_manifest.get("config_target_map")
    target_map_path = (
        target / str(target_map_entry.get("path") or "")
        if isinstance(target_map_entry, Mapping) else None
    )
    try:
        config_targets = json.loads(target_map_path.read_text(encoding="utf-8")) if target_map_path and target_map_path.is_file() else None
    except (OSError, json.JSONDecodeError):
        config_targets = None
    raw_results = _terra_result_objects(terra_results)
    validated: list[dict[str, Any]] = []
    authoritative_packets: set[str] = set()
    for raw in raw_results:
        packet_id = str(raw.get("packet_id") or "")
        packet = packets.get(packet_id)
        if packet is None:
            summary["validation_failures"].append(SynthesisValidationFailure("terra_result_unknown_packet").to_dict())
            continue
        if packet_id in authoritative_packets:
            summary["validation_failures"].append(SynthesisValidationFailure("multiple_results_for_synthesis_packet").to_dict())
            continue
        authoritative_packets.add(packet_id)
        cleaned, failures = validate_terra_result(raw, packet, config_targets=config_targets)
        if cleaned is None:
            summary["validation_failures"].extend(failure.to_dict() for failure in failures)
        else:
            validated.append(cleaned)
    summary["validated_results"] = validated
    expected_packets = {
        str(entry.get("packet_id") or "")
        for entry in synthesis_manifest.get("packets", [])
        if isinstance(entry, Mapping) and str(entry.get("packet_id") or "")
    }
    unreadable_packets = sorted(expected_packets - set(packets))
    if unreadable_packets:
        summary["validation_failures"].append(
            SynthesisValidationFailure("synthesis_manifest_packet_unavailable").to_dict()
        )
        summary["unavailable_synthesis_packet_ids"] = unreadable_packets
    missing_packets = sorted(expected_packets - authoritative_packets)
    if missing_packets:
        summary["validation_failures"].append(
            SynthesisValidationFailure("missing_synthesis_packet_results").to_dict()
        )
        summary["missing_synthesis_packet_ids"] = missing_packets
    if summary["validation_failures"]:
        summary["run_bundle"] = _write_run_bundle(
            target,
            synthesis_manifest,
            validated_results=validated,
        )
        return summary
    catalog, failures = emit_candidate_catalog(target, synthesis_manifest, validated)
    if catalog is None:
        summary["validation_failures"].extend(failure.to_dict() for failure in failures)
        summary["run_bundle"] = _write_run_bundle(
            target,
            synthesis_manifest,
            validated_results=validated,
        )
        return summary
    summary["catalog"] = catalog
    if second_review is None:
        summary["run_bundle"] = _write_run_bundle(
            target,
            synthesis_manifest,
            validated_results=validated,
            catalog=catalog,
        )
        return summary
    review, failures = validate_second_review_result(second_review, catalog)
    if review is None:
        summary["validation_failures"].extend(failure.to_dict() for failure in failures)
        summary["run_bundle"] = _write_run_bundle(
            target,
            synthesis_manifest,
            validated_results=validated,
            catalog=catalog,
        )
        return summary
    summary["second_review"] = review
    summary["run_bundle"] = _write_run_bundle(
        target,
        synthesis_manifest,
        validated_results=validated,
        catalog=catalog,
        second_review=review,
    )
    return summary


orchestrate_synthesis = run_synthesis_pipeline
