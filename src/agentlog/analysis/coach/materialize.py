"""Materialize independently reviewed coach candidates into the local ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agentlog.analysis.claims.models import Claim, ClaimEvidence, Proposal
from agentlog.analysis.claims.packets import PROPOSAL_PACKET_VALIDATOR_VERSION
from agentlog.analysis.claims.proposals import unified_diff
from agentlog.analysis.claims.store import (
    get_claim,
    get_proposal,
    upsert_claims,
    upsert_proposals,
)
from agentlog.analysis.coach.synthesis import (
    CATALOG_VERSION,
    SYNTHESIS_SCHEMA_VERSION,
    build_candidate_catalog,
    build_synthesis_packets,
    exact_deduplicate_observations,
    load_validated_observation_records,
    luna_result_paths,
    summarize_result_processing_coverage,
    validate_second_review_result,
    validate_terra_result,
)
from agentlog.analysis.coach.redaction import COACH_REDACTION_VERSION, redact_locator_text
from agentlog.analysis.coach.proof import (
    EVIDENCE_MESSAGE,
    EVIDENCE_SKILL,
    EVIDENCE_TOOL,
    is_successful_artifact_result,
    supports_bounded_gap,
    supports_skill_action,
    supports_successful_result,
    supports_verification_result,
)
from agentlog.analysis.coach.preprocess import (
    COACH_PROMPT,
    PROMPT_VERSION,
    SCHEMA_VERSION as PREPROCESS_SCHEMA_VERSION,
    CoachPreprocessConfig,
    _synthetic_request_kind,
    build_corpus_snapshot,
    build_eligibility_commitment,
    build_packetized_window_index,
    build_preprocess_coverage,
    build_root_request_index,
)
from agentlog.source_reader import CachedSourceTranscriptReader
from agentlog.safety.redaction import REDACTION_VERSION
from agentlog.registry import supports as harness_supports
from agentlog.session_identity import (
    build_identity_context,
    logical_root_session_id,
    provider_backing_shadow_ids,
    resolve_implicit_parent_ids,
)


MATERIALIZER_VERSION = "coach.materializer.v1"
_PRODUCT_KINDS = frozenset({"observed_instance", "corpus_pattern"})
_ACTIVE_CLAIM_STATUSES = frozenset({"candidate", "approved", "published"})
_SPACE = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_CANONICAL_PART = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_PREPROCESS_PACKET_ID = re.compile(r"^cpkt_[0-9]{4,}$")
_SYNTHESIS_PACKET_ID = re.compile(r"^spkt_[0-9a-f]{24}$")
_BULLET_MARKER = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
_THEME_TOKEN = re.compile(r"[a-z0-9]+")
_THEME_NOISE = frozenset(
    {
        "a",
        "after",
        "before",
        "completed",
        "completion",
        "delivery",
        "done",
        "explicit",
        "for",
        "gap",
        "instruction",
        "miss",
        "missing",
        "please",
        "request",
        "requested",
        "result",
        "run",
        "the",
        "use",
        "work",
        "again",
    }
)
_THEME_ALIASES = {
    "deploy": "deployment",
    "deployed": "deployment",
    "patch": "configuration",
    "patched": "configuration",
    "pytest": "verification",
    "test": "verification",
    "tested": "verification",
    "testing": "verification",
    "tests": "verification",
    "verify": "verification",
    "verified": "verification",
}
_REPEATED_GENERIC_TERMS = frozenset({"artifact", "process", "verification", "write"})
_RULE_THEME_NOISE = frozenset(
    {
        "add",
        "after",
        "before",
        "complete",
        "completed",
        "completion",
        "enforce",
        "evidence",
        "explicit",
        "mark",
        "marking",
        "must",
        "only",
        "pass",
        "passed",
        "passes",
        "proof",
        "record",
        "require",
        "required",
        "requires",
        "result",
        "successful",
        "terminal",
        "when",
    }
)
_REQUEST_ARCS = frozenset(
    {"request", "expectation", "verification_request", "skill_request", "request_1"}
)
_ATOMIC_RULE_START = re.compile(
    r"^(?:add|archive|check|enforce|record|require|run|update|verify)\b",
    re.IGNORECASE,
)
_ATOMIC_RULE_TRIGGER = re.compile(r"\b(?:before|after|when|must|only if)\b", re.IGNORECASE)
_ATOMIC_RULE_OUTCOME = re.compile(
    r"\b(?:artifact|check|commit|file|output|pass(?:ed)?|result|success(?:ful)?|test|verif(?:y|ied|ication))\b",
    re.IGNORECASE,
)
_MISS_ARCS = {
    "instruction_miss": frozenset({"gap"}),
    "delivery_gap": frozenset({"delivery"}),
    "repeated_ask": frozenset({"request_2"}),
}
_REQUIRED_OBSERVATION_ARCS = {
    "instruction_follow": frozenset({"request", "response", "outcome"}),
    "instruction_miss": frozenset({"request", "response", "gap"}),
    "repeated_ask": frozenset({"request_1", "request_2"}),
    "skill_use": frozenset({"skill_request", "skill_evidence", "skill_action"}),
    "delivery_gap": frozenset({"expectation", "delivery"}),
    "verification": frozenset({"verification_request", "verification_result"}),
    "process_fact": frozenset({"action", "artifact"}),
}


class MaterializationError(ValueError):
    pass


@dataclass(frozen=True)
class MaterializationSkip:
    candidate_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, "reason": self.reason}


@dataclass(frozen=True)
class VerifiedCoachRun:
    run_dir: Path
    bundle_hash: str
    catalog: Mapping[str, Any]
    second_review: Mapping[str, Any]
    config_target_map: Mapping[str, Any]
    replay_provenance: Mapping[str, Any]


@dataclass
class MaterializationPlan:
    catalog_id: str
    catalog_hash: str
    review_id: str
    corpus_snapshot_hash: str = ""
    run_dir: str = ""
    run_bundle_hash: str = ""
    target_preconditions: list[dict[str, str]] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    supersede_claim_ids: list[str] = field(default_factory=list)
    supersede_proposal_ids: list[str] = field(default_factory=list)
    unchanged_claim_ids: list[str] = field(default_factory=list)
    unchanged_proposal_ids: list[str] = field(default_factory=list)
    skipped: list[MaterializationSkip] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_hash": self.catalog_hash,
            "review_id": self.review_id,
            "corpus_snapshot_hash": self.corpus_snapshot_hash,
            "run_dir": self.run_dir,
            "run_bundle_hash": self.run_bundle_hash,
            "target_preconditions": [dict(item) for item in self.target_preconditions],
            "claims": [claim.id for claim in self.claims],
            "proposals": [proposal.id for proposal in self.proposals],
            "supersede_claim_ids": sorted(set(self.supersede_claim_ids)),
            "supersede_proposal_ids": sorted(set(self.supersede_proposal_ids)),
            "unchanged_claim_ids": sorted(set(self.unchanged_claim_ids)),
            "unchanged_proposal_ids": sorted(set(self.unchanged_proposal_ids)),
            "skipped": [item.to_dict() for item in self.skipped],
        }


def _claim_integrity_payload(claim: Claim) -> dict[str, Any]:
    payload = claim.to_dict()
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    return payload


def _proposal_integrity_payload(proposal: Proposal) -> dict[str, Any]:
    payload = proposal.to_dict(include_claims=False)
    payload.pop("created_at", None)
    payload.pop("updated_at", None)
    payload["claims"] = [
        _claim_integrity_payload(claim) for claim in proposal.claims
    ]
    return payload


def _plan_integrity_payload(plan: MaterializationPlan) -> dict[str, Any]:
    payload = plan.to_dict()
    payload["claims"] = [_claim_integrity_payload(claim) for claim in plan.claims]
    payload["proposals"] = [
        _proposal_integrity_payload(proposal) for proposal in plan.proposals
    ]
    return payload


def _claim_id_for_lineage(claim: Claim, predecessor_id: str | None) -> str:
    identity = str(claim.confidence_basis.get("semantic_identity") or "")
    version_hash = str(claim.confidence_basis.get("version_hash") or "")
    kind = claim.kind.removeprefix("coach_")
    seed = f"{identity}:{version_hash}"
    if predecessor_id:
        seed += f":supersedes:{predecessor_id}"
    return f"coach:{kind}:{_sha256(seed)[:24]}"


def _bind_claim_lineage(claim: Claim, predecessor_id: str | None) -> None:
    claim.supersedes_id = predecessor_id
    claim.confidence_basis["lineage_predecessor_id"] = predecessor_id
    claim.id = _claim_id_for_lineage(claim, predecessor_id)


def _stored_claim_matches(conn: sqlite3.Connection, expected: Claim) -> bool:
    actual = get_claim(conn, expected.id)
    if actual is None:
        return False
    actual_payload = _claim_integrity_payload(actual)
    expected_payload = _claim_integrity_payload(expected)
    for payload in (actual_payload, expected_payload):
        payload.pop("status", None)
    if actual_payload != expected_payload:
        return False
    if not actual.supersedes_id:
        return True
    predecessor = conn.execute(
        "SELECT status, confidence_basis_json FROM claims WHERE id = ?",
        (actual.supersedes_id,),
    ).fetchone()
    return bool(
        predecessor
        and str(predecessor["status"] or "") == "superseded"
        and str(
            _json_object(predecessor["confidence_basis_json"]).get(
                "semantic_identity"
            )
            or ""
        )
        == str(expected.confidence_basis.get("semantic_identity") or "")
    )


def _bind_existing_claim_lineage(
    conn: sqlite3.Connection,
    expected: Claim,
    row: sqlite3.Row,
) -> bool:
    actual = get_claim(conn, str(row["id"]))
    if actual is None:
        return False
    predecessor = actual.confidence_basis.get("lineage_predecessor_id")
    if predecessor is not None and not isinstance(predecessor, str):
        return False
    predecessor_id = str(predecessor) if predecessor else None
    _bind_claim_lineage(expected, predecessor_id)
    return expected.id == actual.id and _stored_claim_matches(conn, expected)


def _stored_proposal_matches(conn: sqlite3.Connection, expected: Proposal) -> bool:
    actual = get_proposal(conn, expected.id, include_claims=False)
    if actual is None:
        return False
    actual_payload = _proposal_integrity_payload(actual)
    expected_payload = _proposal_integrity_payload(expected)
    for payload in (actual_payload, expected_payload):
        payload.pop("status", None)
        payload.pop("decided_at", None)
        payload.pop("decision_note", None)
        payload.pop("claims", None)
    return actual_payload == expected_payload


@dataclass(frozen=True)
class LegacyQuarantinePlan:
    claim_ids: tuple[str, ...]
    proposal_ids: tuple[str, ...]
    reasons: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_ids": list(self.claim_ids),
            "proposal_ids": list(self.proposal_ids),
            "reasons": dict(self.reasons),
        }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _timestamp_key(value: Any) -> tuple[datetime, str]:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterializationError("evidence timestamp is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MaterializationError("evidence timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc), raw


def _timestamp_range(values: list[str]) -> tuple[str, str]:
    if not values:
        raise MaterializationError("evidence timestamp range is empty")
    ordered = sorted(values, key=_timestamp_key)
    return ordered[0], ordered[-1]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _theme_terms(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        value = _canonical_json(value)
    tokens = {
        _THEME_ALIASES.get(token, token)
        for token in _THEME_TOKEN.findall(str(value or "").casefold())
    }
    return {token for token in tokens if token not in _THEME_NOISE and len(token) >= 3}


def _instruction_theme(value: Any) -> set[str]:
    return _theme_terms(value) - _RULE_THEME_NOISE


def _instruction_semantically_present(instruction: str, content: str) -> bool:
    expected = _instruction_theme(instruction)
    if not expected:
        return False
    for raw_line in content.splitlines():
        line = re.sub(
            r"^(?:#+\s*|[-*+]\s+|\d+[.)]\s+)", "", raw_line.strip()
        )
        if _ATOMIC_RULE_START.match(line) and expected <= _instruction_theme(line):
            return True
    return False


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _load_object(value: Mapping[str, Any] | Path | str, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"invalid {label}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise MaterializationError(f"{label} must be a JSON object")
    return loaded


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterializationError(f"invalid {label}: {exc}") from exc


def _fixed_bundle_entry(
    bundle: Mapping[str, Any],
    key: str,
    expected_path: str,
) -> tuple[Path, str]:
    entry = bundle.get(key)
    if not isinstance(entry, Mapping):
        raise MaterializationError(f"run bundle has no {key} entry")
    relative = str(entry.get("path") or "")
    declared_hash = str(entry.get("hash") or "")
    if relative != expected_path or not declared_hash:
        raise MaterializationError(f"run bundle {key} path or hash is invalid")
    return Path(relative), declared_hash


def _verified_config_inventory(
    target_map: Mapping[str, Any],
) -> list[dict[str, str]]:
    targets = target_map.get("targets")
    if not isinstance(targets, list):
        raise MaterializationError("private config target map has no targets")
    inventory: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in targets:
        if not isinstance(raw, Mapping):
            raise MaterializationError("private config target entry is invalid")
        target_ref = str(raw.get("target_ref") or "")
        target_path = str(raw.get("target_path") or "")
        fingerprint = str(raw.get("fingerprint") or "")
        target_kind = str(raw.get("target_kind") or "")
        path = Path(target_path).expanduser()
        if (
            not target_ref
            or target_ref in seen
            or not path.is_absolute()
            or target_ref != _target_ref(target_path, fingerprint)
            or target_kind
            not in {"instruction_file", "config", "skill", "harness_rule"}
        ):
            raise MaterializationError("private config target entry is not self-consistent")
        seen.add(target_ref)
        try:
            data = path.read_bytes() if path.is_file() else b""
        except OSError as exc:
            raise MaterializationError(f"private config target is unreadable: {exc}") from exc
        if not _hash_matches(data, fingerprint):
            raise MaterializationError("private config target changed after synthesis")
        inventory.append(
            {
                "path": str(path.resolve()),
                "content": data.decode("utf-8", errors="replace"),
                "fingerprint": fingerprint,
                "target_kind": target_kind,
            }
        )
    return inventory


def _verify_preprocess_coverage(
    conn: sqlite3.Connection,
    manifest: Mapping[str, Any],
) -> CoachPreprocessConfig:
    selection = manifest.get("selection_config")
    if not isinstance(selection, Mapping) or set(selection) != {
        "publication_mode",
        "max_windows_per_root",
        "max_windows_per_packet",
        "max_packets",
        "max_quote_chars",
        "max_packet_chars",
    }:
        raise MaterializationError("coach preprocess selection config is missing")
    max_windows_per_root = selection.get("max_windows_per_root")
    max_windows_per_packet = selection.get("max_windows_per_packet")
    max_packets = selection.get("max_packets")
    max_quote_chars = selection.get("max_quote_chars")
    max_packet_chars = selection.get("max_packet_chars")
    publication_mode = str(selection.get("publication_mode") or "")
    if (
        publication_mode not in {"full", "sampled"}
        or (
            max_windows_per_root is not None
            and (
                not isinstance(max_windows_per_root, int)
                or isinstance(max_windows_per_root, bool)
                or max_windows_per_root < 1
            )
        )
        or not isinstance(max_windows_per_packet, int)
        or isinstance(max_windows_per_packet, bool)
        or max_windows_per_packet < 1
        or (
            max_quote_chars is not None
            and (
                not isinstance(max_quote_chars, int)
                or isinstance(max_quote_chars, bool)
                or max_quote_chars < 1
            )
        )
        or (
            max_packets is not None
            and (
                not isinstance(max_packets, int)
                or isinstance(max_packets, bool)
                or max_packets < 0
            )
        )
        or (
            max_packet_chars is not None
            and (
                not isinstance(max_packet_chars, int)
                or isinstance(max_packet_chars, bool)
                or max_packet_chars < 1
            )
        )
    ):
        raise MaterializationError("coach preprocess selection config is invalid")
    config = CoachPreprocessConfig(
        publication_mode=publication_mode,
        max_windows_per_root=max_windows_per_root,
        max_windows_per_packet=max_windows_per_packet,
        max_packets=max_packets,
        max_quote_chars=max_quote_chars,
        max_packet_chars=max_packet_chars,
    )
    expected = build_eligibility_commitment(conn, config=config)
    if manifest.get("eligibility_commitment") != expected:
        raise MaterializationError(
            "coach preprocess eligibility commitment differs from the ledger"
        )
    raw_capability = expected.get("proof_capability")
    capability_by_harness = (
        raw_capability.get("by_harness")
        if isinstance(raw_capability, Mapping)
        else None
    )
    if (
        not isinstance(capability_by_harness, Mapping)
        or manifest.get("proof_capability_by_harness") != capability_by_harness
    ):
        raise MaterializationError(
            "coach preprocess proof capability differs from the ledger"
        )
    coverage = manifest.get("coverage")
    if not isinstance(coverage, Mapping):
        raise MaterializationError("coach preprocess coverage is missing")
    if dict(coverage) != build_preprocess_coverage(conn, config=config):
        raise MaterializationError("coach preprocess coverage differs from the ledger")
    expected_counts = {
        "eligible": len(expected["eligible_window_ids"]),
        "eligible_windows": len(expected["eligible_window_ids"]),
        "selected": len(expected["selected_window_ids"]),
        "selected_windows": len(expected["selected_window_ids"]),
        "packetized": len(expected["packetized_window_ids"]),
        "packetized_windows": len(expected["packetized_window_ids"]),
        "eligible_roots": len(expected["eligible_root_ids"]),
        "selected_roots": len(expected["selected_root_ids"]),
        "packetized_roots": len(expected["packetized_root_ids"]),
    }
    if any(coverage.get(key) != value for key, value in expected_counts.items()):
        raise MaterializationError("coach preprocess coverage differs from the ledger")
    packetized_windows = build_packetized_window_index(conn, config=config)
    source_truncated_messages = sum(
        1
        for window in packetized_windows.values()
        for message in window.get("messages", [])
        if isinstance(message, Mapping) and message.get("source_truncated") is True
    )
    publication_complete = (
        expected["selected_window_ids"] == expected["eligible_window_ids"]
        and expected["packetized_window_ids"] == expected["eligible_window_ids"]
        and expected["selected_root_ids"] == expected["eligible_root_ids"]
        and expected["packetized_root_ids"] == expected["eligible_root_ids"]
        and source_truncated_messages == 0
    )
    if (
        coverage.get("publication_mode") != publication_mode
        or coverage.get("publication_complete") is not publication_complete
        or coverage.get("source_truncated_messages") != source_truncated_messages
        or coverage.get("excluded_synthetic_windows")
        != len(expected.get("excluded_synthetic_window_ids", []))
    ):
        raise MaterializationError(
            "coach preprocess publication coverage differs from the ledger"
        )
    if coverage.get("proof_capability_by_harness") != capability_by_harness:
        raise MaterializationError("coach preprocess capability coverage is inconsistent")
    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        raise MaterializationError("coach preprocess routing coverage is missing")
    if (
        manifest.get("per_harness") != expected["packetized_per_harness"]
        or manifest.get("harness_counts") != expected["packetized_per_harness"]
        or counts.get("by_harness") != expected["packetized_per_harness"]
        or manifest.get("per_repo") != expected["packetized_per_repo"]
        or manifest.get("repo_counts") != expected["packetized_per_repo"]
        or counts.get("by_repo") != expected["packetized_per_repo"]
        or manifest.get("excluded_roots") != expected["excluded_root_ids"]
    ):
        raise MaterializationError("coach preprocess routing coverage differs from the ledger")
    packet_windows = sorted(
        str(window_id)
        for entry in manifest.get("packets", [])
        if isinstance(entry, Mapping)
        for window_id in entry.get("window_ids", [])
    )
    packet_roots = sorted(
        {
            str(root_id)
            for entry in manifest.get("packets", [])
            if isinstance(entry, Mapping)
            for root_id in entry.get("root_session_ids", [])
        }
    )
    if (
        packet_windows != expected["packetized_window_ids"]
        or packet_roots != expected["packetized_root_ids"]
    ):
        raise MaterializationError(
            "coach preprocess packet membership differs from the ledger selection"
        )
    manifest_packets = manifest.get("packets")
    expected_groups = expected.get("packet_groups")
    if (
        not isinstance(manifest_packets, list)
        or not isinstance(expected_groups, list)
        or len(manifest_packets) != len(expected_groups)
    ):
        raise MaterializationError("coach preprocess packet groups differ from the ledger")
    for index, (entry, group) in enumerate(
        zip(manifest_packets, expected_groups, strict=True), start=1
    ):
        if (
            not isinstance(entry, Mapping)
            or not isinstance(group, Mapping)
            or str(entry.get("packet_id") or "") != f"cpkt_{index:04d}"
            or entry.get("window_ids") != group.get("window_ids")
            or entry.get("root_session_ids") != group.get("root_session_ids")
        ):
            raise MaterializationError(
                "coach preprocess packet groups differ from the ledger"
            )
    return config


def _verify_preprocess_manifest_envelope(manifest: Mapping[str, Any]) -> None:
    defaults = CoachPreprocessConfig()
    prompt_hash = hashlib.sha256(COACH_PROMPT.encode("utf-8")).hexdigest()[:24]
    expected_producer = {
        "provider": defaults.producer_provider,
        "model": defaults.producer_model,
        "worker_id": defaults.producer_worker_id,
        "assignment_id": defaults.producer_assignment_id,
        "prompt_hash": prompt_hash,
    }
    expected_contract = {
        "required": list(expected_producer),
        "expected": expected_producer,
        "bound": True,
    }
    coverage = manifest.get("coverage")
    eligible = coverage.get("eligible") if isinstance(coverage, Mapping) else None
    selected = coverage.get("selected") if isinstance(coverage, Mapping) else None
    suffix = (
        hashlib.sha256(f"{eligible}{selected}".encode("utf-8")).hexdigest()[:8]
        if isinstance(eligible, int) and isinstance(selected, int)
        else ""
    )
    if (
        manifest.get("schema_version") != PREPROCESS_SCHEMA_VERSION
        or manifest.get("coach_redaction_version") != COACH_REDACTION_VERSION
        or manifest.get("safety_redaction_version") != REDACTION_VERSION
        or manifest.get("prompt_hash") != prompt_hash
        or manifest.get("producer_contract") != expected_contract
        or not re.fullmatch(
            rf"coach_[0-9]{{8}}T[0-9]{{6}}Z_{re.escape(suffix)}",
            str(manifest.get("run_id") or ""),
        )
    ):
        raise MaterializationError("coach preprocess manifest envelope is invalid")


def _verify_preprocess_packet_windows(
    conn: sqlite3.Connection,
    root: Path,
    manifest: Mapping[str, Any],
    config: CoachPreprocessConfig,
) -> None:
    expected = build_packetized_window_index(conn, config=config)
    expected_requests = build_root_request_index(conn, config=config)
    snapshot = manifest.get("corpus_snapshot")
    if not isinstance(snapshot, Mapping):
        raise MaterializationError("coach preprocess manifest snapshot is missing")
    packet_snapshot = {
        "snapshot_hash": snapshot.get("snapshot_hash"),
        "counts": snapshot.get("counts"),
        "high_water": snapshot.get("high_water"),
    }
    stored: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("packets", []):
        if not isinstance(entry, Mapping):
            raise MaterializationError("coach preprocess manifest packet is malformed")
        packet_path = root / str(entry.get("path") or "")
        packet = _load_object(packet_path, "coach preprocess packet")
        serialized = (
            json.dumps(packet, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        try:
            stored_bytes = packet_path.read_bytes()
        except OSError as exc:
            raise MaterializationError("cannot read coach preprocess packet bytes") from exc
        serialized_bytes = entry.get("serialized_bytes")
        if (
            not isinstance(serialized_bytes, int)
            or isinstance(serialized_bytes, bool)
            or serialized_bytes != len(serialized)
            or stored_bytes != serialized
            or (
                config.max_packet_chars is not None
                and serialized_bytes > config.max_packet_chars
            )
        ):
            raise MaterializationError("coach preprocess packet byte budget is invalid")
        windows = packet.get("windows")
        packet_body = dict(packet)
        declared_packet_hash = str(packet_body.pop("packet_hash", "") or "")
        computed_packet_hash = hashlib.sha256(
            json.dumps(packet_body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]
        expected_envelope = {
            "schema_version": PREPROCESS_SCHEMA_VERSION,
            "packet_id": str(entry.get("packet_id") or ""),
            "run_id": str(manifest.get("run_id") or ""),
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": str(manifest.get("prompt_hash") or ""),
            "coach_redaction_version": COACH_REDACTION_VERSION,
            "safety_redaction_version": REDACTION_VERSION,
            "producer_contract": manifest.get("producer_contract"),
            "corpus_snapshot_hash": manifest.get("corpus_snapshot_hash"),
            "corpus_snapshot": packet_snapshot,
            "root_session_ids": entry.get("root_session_ids"),
            "publication_mode": config.publication_mode,
            "redaction": manifest.get("redaction"),
            "provenance": {
                "selection": "score_then_temporal_strata",
                "source": "sqlite",
                "parser": "stored artifact parser_version",
            },
        }
        actual_envelope = {
            key: packet.get(key) for key in expected_envelope
        }
        if (
            str(packet.get("packet_id") or "") != str(entry.get("packet_id") or "")
            or packet.get("root_session_ids") != entry.get("root_session_ids")
            or declared_packet_hash != str(entry.get("packet_hash") or "")
            or declared_packet_hash != computed_packet_hash
            or actual_envelope != expected_envelope
            or not isinstance(windows, list)
            or [str(window.get("window_id") or "") for window in windows if isinstance(window, Mapping)]
            != entry.get("window_ids")
        ):
            raise MaterializationError("coach preprocess packet windows are malformed")
        for raw_window in windows:
            if not isinstance(raw_window, Mapping):
                raise MaterializationError("coach preprocess packet window is malformed")
            window_id = str(raw_window.get("window_id") or "")
            if not window_id or window_id in stored:
                raise MaterializationError("coach preprocess packet window identity is invalid")
            stored[window_id] = dict(raw_window)
        packet_roots = [str(value) for value in entry.get("root_session_ids", [])]
        expected_packet_requests = {
            root_id: expected_requests.get(root_id, []) for root_id in packet_roots
        }
        if packet.get("root_request_index") != expected_packet_requests:
            raise MaterializationError(
                "coach preprocess root request context differs from the current ledger"
            )
        serialized_requests = json.dumps(
            expected_packet_requests,
            sort_keys=True,
            ensure_ascii=False,
        )
        expected_request_hash = hashlib.sha256(
            serialized_requests.encode("utf-8")
        ).hexdigest()[:24]
        if str(entry.get("root_request_index_hash") or "") != expected_request_hash:
            raise MaterializationError(
                "coach preprocess root request context hash is invalid"
            )
    if stored != expected:
        raise MaterializationError(
            "coach preprocess packet content differs from the current ledger"
        )


def _verified_proof_capability(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    raw = manifest.get("proof_capability_by_harness")
    if not isinstance(raw, Mapping) or not raw:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for harness, value in raw.items():
        if not isinstance(value, Mapping):
            raise MaterializationError("coach preprocess proof capability is malformed")
        eligible = value.get("eligible_roots")
        processed = value.get("packetized_roots")
        levels = value.get("levels")
        if (
            not isinstance(eligible, int)
            or isinstance(eligible, bool)
            or eligible < 0
            or not isinstance(processed, int)
            or isinstance(processed, bool)
            or processed < 0
            or processed > eligible
            or not isinstance(levels, Mapping)
        ):
            raise MaterializationError("coach preprocess proof capability is invalid")
        deterministic = levels.get("deterministic_terminal")
        owner_only = levels.get("owner_message_only")
        unknown = levels.get("unknown")
        if (
            not isinstance(deterministic, int)
            or isinstance(deterministic, bool)
            or deterministic < 0
            or deterministic > eligible
            or not isinstance(owner_only, int)
            or isinstance(owner_only, bool)
            or owner_only < 0
            or not isinstance(unknown, int)
            or isinstance(unknown, bool)
            or unknown < 0
            or deterministic + owner_only + unknown != eligible
        ):
            raise MaterializationError("coach preprocess proof capability level is invalid")
        adapter_capability = harness_supports(str(harness), "tool_events")
        observed_coverage = (
            "complete"
            if deterministic == eligible and eligible
            else "partial"
            if deterministic
            else "absent"
            if owner_only
            else "unknown"
        )
        if (
            value.get("adapter_capability") != adapter_capability
            or value.get("observed_proof_coverage") != observed_coverage
            or value.get("capability") != adapter_capability
        ):
            raise MaterializationError("coach preprocess proof capability is dishonest")
        normalized[str(harness)] = {
            "eligible_roots": eligible,
            "processed_roots": processed,
            "proof_capable_roots": deterministic,
            "levels": dict(levels),
            "adapter_capability": adapter_capability,
            "observed_proof_coverage": observed_coverage,
            "capability": adapter_capability,
            "capability_complete": (
                eligible == processed
                and eligible == deterministic
                and value.get("capability") == "supported"
            ),
        }
    return {key: normalized[key] for key in sorted(normalized)}


def verify_coach_run(
    conn: sqlite3.Connection,
    run_dir: Path | str,
) -> VerifiedCoachRun:
    root = Path(run_dir).expanduser().resolve()
    bundle_path = root / "synthesis_run_bundle.json"
    bundle = _load_object(bundle_path, "coach synthesis run bundle")
    if bundle.get("schema_version") != SYNTHESIS_SCHEMA_VERSION:
        raise MaterializationError("unsupported coach synthesis run bundle")
    declared_bundle_hash = str(bundle.get("bundle_hash") or "")
    bundle_body = dict(bundle)
    bundle_body.pop("bundle_hash", None)
    if not declared_bundle_hash or declared_bundle_hash != _sha256(bundle_body):
        raise MaterializationError("coach synthesis run bundle hash mismatch")
    declared_bundle_id = str(bundle_body.pop("bundle_id", "") or "")
    if declared_bundle_id != f"bundle_{_sha256(bundle_body)[:24]}":
        raise MaterializationError("coach synthesis run bundle identity mismatch")

    manifest_relative, manifest_hash = _fixed_bundle_entry(
        bundle, "source_preprocess_manifest", "manifest.json"
    )
    manifest = _load_object(root / manifest_relative, "coach preprocess manifest")
    if _sha256(manifest) != manifest_hash:
        raise MaterializationError("coach preprocess manifest hash mismatch")
    raw_manifest_packets = manifest.get("packets")
    if not isinstance(raw_manifest_packets, list) or any(
        not isinstance(item, Mapping) for item in raw_manifest_packets
    ):
        raise MaterializationError("coach preprocess manifest packet index is invalid")
    manifest_packets = [dict(item) for item in raw_manifest_packets]
    _verify_preprocess_manifest_envelope(manifest)
    manifest_packet_ids = [str(item.get("packet_id") or "") for item in manifest_packets]
    if (
        any(not _PREPROCESS_PACKET_ID.fullmatch(packet_id) for packet_id in manifest_packet_ids)
        or len(set(manifest_packet_ids)) != len(manifest_packet_ids)
        or any(
            str(item.get("path") or "") != f"packets/{item['packet_id']}.json"
            for item in manifest_packets
        )
        or any(not str(item.get("packet_hash") or "") for item in manifest_packets)
        or any(
            not isinstance(item.get("window_ids"), list)
            or not item["window_ids"]
            or len({str(value) for value in item["window_ids"]})
            != len(item["window_ids"])
            or not isinstance(item.get("root_session_ids"), list)
            or not item["root_session_ids"]
            or len({str(value) for value in item["root_session_ids"]})
            != len(item["root_session_ids"])
            for item in manifest_packets
        )
    ):
        raise MaterializationError("coach preprocess manifest packet index is invalid")
    preprocess_config = _verify_preprocess_coverage(conn, manifest)
    _verify_preprocess_packet_windows(conn, root, manifest, preprocess_config)
    records, observation_failures = load_validated_observation_records(
        root, manifest=manifest
    )
    processing, processing_failures = summarize_result_processing_coverage(
        root, manifest=manifest
    )
    failures = [*observation_failures, *processing_failures]
    if failures:
        raise MaterializationError(
            "coach preprocess replay failed: "
            + ", ".join(failure.reason for failure in failures)
        )
    if int(processing.get("processed_packets") or 0) != len(manifest_packet_ids):
        raise MaterializationError("coach run is missing a Luna result or abstention")
    preprocess_result_hashes: dict[str, str] = {}
    luna_results: list[dict[str, Any]] = []
    result_paths = luna_result_paths(root)
    legacy_result_paths = {
        root / "results" / f"{packet_id}.json" for packet_id in manifest_packet_ids
    }
    scoped_result_paths = {
        root / "results" / "luna" / f"{packet_id}.json"
        for packet_id in manifest_packet_ids
    }
    if (
        set(result_paths) != legacy_result_paths
        and set(result_paths) != scoped_result_paths
    ):
        raise MaterializationError("coach preprocess result paths are incomplete or unexpected")
    for path in result_paths:
        parsed = _read_json(path, "coach preprocess result")
        preprocess_result_hashes[str(path.relative_to(root))] = _sha256(parsed)
        if isinstance(parsed, Mapping):
            luna_results.append(
                {
                    "packet_id": str(parsed.get("packet_id") or ""),
                    "result_id": str(parsed.get("result_id") or ""),
                    "producer": dict(parsed.get("producer") or {}),
                    "abstain": parsed.get("abstain") is True,
                }
            )

    synthesis_relative, synthesis_hash = _fixed_bundle_entry(
        bundle, "synthesis_manifest", "synthesis_manifest.json"
    )
    synthesis_manifest = _load_object(
        root / synthesis_relative, "coach synthesis manifest"
    )
    if _sha256(synthesis_manifest) != synthesis_hash:
        raise MaterializationError("coach synthesis manifest hash mismatch")
    if (
        synthesis_manifest.get("schema_version") != SYNTHESIS_SCHEMA_VERSION
        or str(synthesis_manifest.get("source_manifest_hash") or "") != manifest_hash
        or str(synthesis_manifest.get("corpus_snapshot_hash") or "")
        != str(bundle.get("corpus_snapshot_hash") or "")
    ):
        raise MaterializationError("coach synthesis manifest lineage mismatch")
    if (
        synthesis_manifest.get("corpus_snapshot") != manifest.get("corpus_snapshot")
        or synthesis_manifest.get("corpus_snapshot_hash")
        != manifest.get("corpus_snapshot_hash")
    ):
        raise MaterializationError("coach synthesis snapshot differs from preprocess")

    target_relative, target_hash = _fixed_bundle_entry(
        bundle, "config_target_map", "synthesis_config_targets.json"
    )
    target_map = _load_object(root / target_relative, "private config target map")
    if (
        _sha256(target_map) != target_hash
        or synthesis_manifest.get("config_target_map") != bundle.get("config_target_map")
    ):
        raise MaterializationError("private config target map hash mismatch")
    config_inventory = _verified_config_inventory(target_map)

    expected_packets = build_synthesis_packets(
        records,
        manifest,
        config_inventory=config_inventory,
        processing_coverage=processing,
    )
    expected_by_id = {
        str(packet["packet_id"]): packet for packet in expected_packets
    }
    raw_synthesis_entries = synthesis_manifest.get("packets")
    if not isinstance(raw_synthesis_entries, list) or any(
        not isinstance(item, Mapping) for item in raw_synthesis_entries
    ):
        raise MaterializationError("coach synthesis packet index is invalid")
    synthesis_entries = [dict(item) for item in raw_synthesis_entries]
    synthesis_packet_ids = [str(item.get("packet_id") or "") for item in synthesis_entries]
    if (
        any(not _SYNTHESIS_PACKET_ID.fullmatch(packet_id) for packet_id in synthesis_packet_ids)
        or set(synthesis_packet_ids) != set(expected_by_id)
        or len(synthesis_packet_ids) != len(set(synthesis_packet_ids))
    ):
        raise MaterializationError("coach synthesis packet set is incomplete or unexpected")
    stored_packets: dict[str, dict[str, Any]] = {}
    for entry in synthesis_entries:
        packet_id = str(entry["packet_id"])
        if str(entry.get("path") or "") != f"synthesis_packets/{packet_id}.json":
            raise MaterializationError("coach synthesis packet path is not fixed")
        packet = _load_object(root / str(entry["path"]), "coach synthesis packet")
        if (
            str(packet.get("packet_hash") or "") != str(entry.get("packet_hash") or "")
            or packet != expected_by_id[packet_id]
        ):
            raise MaterializationError("coach synthesis packet replay mismatch")
        stored_packets[packet_id] = packet
    expected_coverage = (
        expected_packets[0]["coverage"]
        if expected_packets
        else {
            **dict(manifest.get("coverage") or {}),
            "eligible_roots": int((manifest.get("coverage") or {}).get("eligible_roots") or 0),
            "processed_roots": int(processing.get("processed_roots") or 0),
            "processed_packets": int(processing.get("processed_packets") or 0),
            "processed_windows": int(processing.get("processed_windows") or 0),
            "abstained_packets": int(processing.get("abstained_packets") or 0),
            "valid_observations": int(processing.get("valid_observations") or 0),
            "processing_incomplete": True,
        }
    )
    if (
        synthesis_manifest.get("coverage") != expected_coverage
        or int(synthesis_manifest.get("observation_count") or 0)
        != len(exact_deduplicate_observations(records))
    ):
        raise MaterializationError("coach synthesis coverage replay mismatch")

    results_relative, results_hash = _fixed_bundle_entry(
        bundle, "validated_results", "synthesis_results/validated_results.json"
    )
    result_body = _load_object(root / results_relative, "validated Terra results")
    declared_results_hash = str(result_body.pop("results_hash", "") or "")
    if declared_results_hash != results_hash or _sha256(result_body) != results_hash:
        raise MaterializationError("validated Terra results hash mismatch")
    terra_results = result_body.get("results")
    if not isinstance(terra_results, list):
        raise MaterializationError("validated Terra results are not a list")
    result_packet_ids = [
        str(item.get("packet_id") or "")
        for item in terra_results
        if isinstance(item, Mapping)
    ]
    if (
        len(result_packet_ids) != len(terra_results)
        or len(set(result_packet_ids)) != len(result_packet_ids)
        or set(result_packet_ids) != set(stored_packets)
    ):
        raise MaterializationError("coach run is missing a Terra result or abstention")
    validated_results: list[dict[str, Any]] = []
    for raw_result in terra_results:
        packet_id = str(raw_result.get("packet_id") or "")
        validated, terra_failures = validate_terra_result(
            raw_result,
            stored_packets[packet_id],
            config_targets=target_map,
        )
        if terra_failures or validated is None or validated != raw_result:
            reasons = ", ".join(failure.reason for failure in terra_failures)
            raise MaterializationError(
                f"validated Terra result replay failed: {reasons or 'non-canonical result'}"
            )
        validated_results.append(validated)

    catalog_relative, catalog_hash = _fixed_bundle_entry(
        bundle, "candidate_catalog", "candidate_catalog.json"
    )
    catalog = _load_object(root / catalog_relative, "candidate catalog")
    rebuilt_catalog, catalog_failures = build_candidate_catalog(
        synthesis_manifest, validated_results
    )
    if (
        catalog_failures
        or rebuilt_catalog is None
        or catalog != rebuilt_catalog
        or str(catalog.get("catalog_hash") or "") != catalog_hash
    ):
        raise MaterializationError("candidate catalog replay mismatch")

    review_relative, review_hash = _fixed_bundle_entry(
        bundle, "second_review", "second_review.json"
    )
    review_body = _load_object(root / review_relative, "second coach review")
    declared_review_hash = str(review_body.pop("review_hash", "") or "")
    if declared_review_hash != review_hash or _sha256(review_body) != review_hash:
        raise MaterializationError("second coach review hash mismatch")
    validated_review, review_failures = validate_second_review_result(
        review_body, catalog
    )
    if review_failures or validated_review is None or validated_review != review_body:
        reasons = ", ".join(failure.reason for failure in review_failures)
        raise MaterializationError(
            f"second coach review replay failed: {reasons or 'non-canonical review'}"
        )

    _catalog_integrity(catalog)
    _verify_corpus_snapshot(conn, catalog)
    _verify_catalog_evidence(conn, catalog)
    proof_capability = _verified_proof_capability(manifest)
    replay_provenance = {
        "run_bundle_hash": declared_bundle_hash,
        "preprocess_manifest_hash": manifest_hash,
        "preprocess_result_hashes": preprocess_result_hashes,
        "luna_results": sorted(
            luna_results, key=lambda item: (item["packet_id"], item["result_id"])
        ),
        "luna_producers": [
            dict(value)
            for _, value in sorted(
                {
                    _canonical_json(item["producer"]): item["producer"]
                    for item in luna_results
                }.items()
            )
        ],
        "synthesis_manifest_hash": synthesis_hash,
        "synthesis_packet_hashes": {
            packet_id: str(packet["packet_hash"])
            for packet_id, packet in sorted(stored_packets.items())
        },
        "terra_results_hash": results_hash,
        "terra_synthesis_results": [
            {
                "packet_id": str(result.get("packet_id") or ""),
                "result_id": str(result.get("result_id") or ""),
                "producer": dict(result.get("producer") or {}),
                "abstain": result.get("abstain") is True,
            }
            for result in sorted(
                validated_results, key=lambda item: str(item.get("packet_id") or "")
            )
        ],
        "terra_synthesis_producer": dict(catalog.get("synthesis_assignment") or {}),
        "catalog_hash": catalog_hash,
        "second_review_hash": review_hash,
        "terra_review_producer": dict(validated_review.get("producer") or {}),
        "terra_review_id": str(validated_review.get("review_id") or ""),
        "config_target_map_hash": target_hash,
        "selection_method": "score_then_temporal_strata",
        "selection_config": dict(manifest["selection_config"]),
        "selection_caveat": (
            "All eligible windows and full redacted message text were processed."
            if (manifest.get("coverage") or {}).get("publication_complete") is True
            else "Coverage is bounded to deterministic signal-ranked windows per logical root."
        ),
        "publication_mode": str((manifest.get("coverage") or {}).get("publication_mode") or ""),
        "publication_complete": (manifest.get("coverage") or {}).get("publication_complete"),
        "source_truncated_messages": (manifest.get("coverage") or {}).get(
            "source_truncated_messages"
        ),
        "eligibility_commitment_hash": str(
            (manifest.get("eligibility_commitment") or {}).get("hash") or ""
        ),
        "proof_capability_by_harness": proof_capability,
    }
    return VerifiedCoachRun(
        run_dir=root,
        bundle_hash=declared_bundle_hash,
        catalog=catalog,
        second_review=validated_review,
        config_target_map=target_map,
        replay_provenance=replay_provenance,
    )


def _catalog_integrity(catalog: Mapping[str, Any]) -> None:
    if catalog.get("schema_version") != CATALOG_VERSION:
        raise MaterializationError("unsupported candidate catalog schema")
    declared = str(catalog.get("catalog_hash") or "")
    body = dict(catalog)
    body.pop("catalog_hash", None)
    if not declared or declared != _sha256(body):
        raise MaterializationError("candidate catalog hash mismatch")


def _verify_corpus_snapshot(
    conn: sqlite3.Connection,
    catalog: Mapping[str, Any],
) -> str:
    snapshot = catalog.get("corpus_snapshot")
    declared = str(catalog.get("corpus_snapshot_hash") or "")
    if not isinstance(snapshot, Mapping) or not declared:
        raise MaterializationError("candidate catalog has no corpus snapshot lineage")
    if str(snapshot.get("snapshot_hash") or "") != declared:
        raise MaterializationError("candidate catalog snapshot identity is inconsistent")
    current = build_corpus_snapshot(conn)
    if (
        str(current.get("snapshot_hash") or "") != declared
        or _canonical_json(current) != _canonical_json(snapshot)
    ):
        raise MaterializationError("candidate catalog corpus snapshot is stale or forged")
    return declared


def _session_roots(
    conn: sqlite3.Connection,
) -> tuple[dict[str, sqlite3.Row], dict[str, str], set[str]]:
    rows = conn.execute(
        "SELECT id, harness, external_id, parent_session_id, artifact_id, "
        "started_at, repo FROM sessions ORDER BY id"
    ).fetchall()
    sessions = {str(row["id"]): row for row in rows}
    parents = resolve_implicit_parent_ids(rows)
    physical_roots: dict[str, str] = {}
    for session_id, row in sessions.items():
        current = session_id
        seen: set[str] = set()
        while current in sessions:
            if current in seen:
                raise MaterializationError("coach session lineage contains a cycle")
            seen.add(current)
            parent = parents.get(current)
            if parent is None:
                break
            current = parent
        physical_roots[session_id] = current
    identity = build_identity_context(conn)
    logical_roots = {
        session_id: logical_root_session_id(
            conn, physical_roots[session_id], context=identity
        )
        for session_id in sessions
    }
    return sessions, logical_roots, provider_backing_shadow_ids(conn, context=identity)


def _repo_label(value: Any) -> str:
    raw = str(value or "")
    return f"repo:{_sha256(raw)[:24]}" if raw else "(unknown)"


def _fact_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _artifact_matches(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    evidence: Mapping[str, Any],
) -> bool:
    artifact_id = session["artifact_id"]
    declared_id = evidence.get("artifact_id")
    if artifact_id is None:
        return declared_id in (None, "") and not evidence.get("artifact_hash") and not evidence.get("parser_version")
    if str(declared_id) != str(artifact_id):
        return False
    artifact = conn.execute(
        "SELECT content_hash, parser_version FROM artifacts WHERE id = ?",
        (artifact_id,),
    ).fetchone()
    return bool(
        artifact
        and str(evidence.get("artifact_hash") or "") == str(artifact["content_hash"] or "")
        and str(evidence.get("parser_version") or "") == str(artifact["parser_version"] or "")
    )


def _source_replay_messages(
    conn: sqlite3.Connection,
    session_id: str,
    cache: dict[str, Any],
) -> dict[str, Mapping[str, Any]] | None:
    row = conn.execute(
        "SELECT transcript_storage FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise MaterializationError("source-backed evidence session does not exist")
    if str(row["transcript_storage"] or "legacy_materialized") != "source_backed":
        return None
    if session_id not in cache:
        reader = cache.get("__artifact_reader__")
        if not isinstance(reader, CachedSourceTranscriptReader):
            reader = CachedSourceTranscriptReader()
            cache["__artifact_reader__"] = reader
        result = reader(conn, session_id)
        if str(getattr(result, "status", "source_unavailable")) != "ready":
            raise MaterializationError("source-backed evidence source is unavailable")
        messages = getattr(result, "messages", None)
        if not isinstance(messages, list):
            raise MaterializationError("source-backed evidence source is invalid")
        indexed: dict[str, Mapping[str, Any]] = {}
        for message in messages:
            if not isinstance(message, Mapping):
                raise MaterializationError("source-backed evidence source is invalid")
            message_id = str(message.get("id") or "")
            if not message_id or message_id in indexed:
                raise MaterializationError("source-backed evidence source is invalid")
            indexed[message_id] = dict(message, session_id=session_id)
        persisted = conn.execute(
            "SELECT id, seq, role, timestamp, content_hash, is_tool_plumbing, authored_by_agent "
            "FROM messages WHERE session_id = ? ORDER BY seq, id",
            (session_id,),
        ).fetchall()
        source_ordered = sorted(
            indexed.values(), key=lambda message: (int(message["seq"]), str(message["id"]))
        )
        if len(persisted) != len(source_ordered):
            raise MaterializationError("source-backed evidence differs from the ledger")
        for stored, source in zip(persisted, source_ordered):
            if (
                str(stored["id"]) != str(source["id"])
                or int(stored["seq"]) != int(source["seq"])
                or str(stored["role"] or "") != str(source["role"] or "")
                or str(stored["timestamp"] or "") != str(source["timestamp"] or "")
                or str(stored["content_hash"] or "") != str(source["content_hash"] or "")
                or bool(stored["is_tool_plumbing"]) != bool(source["is_tool_plumbing"])
                or bool(stored["authored_by_agent"]) != bool(source["authored_by_agent"])
            ):
                raise MaterializationError("source-backed evidence differs from the ledger")
        cache[session_id] = indexed
    return cache[session_id]


def _replay_message(
    conn: sqlite3.Connection,
    session_id: str,
    message_id: str,
    cache: dict[str, Any],
) -> Mapping[str, Any] | None:
    source_messages = _source_replay_messages(conn, session_id, cache)
    if source_messages is not None:
        return source_messages.get(message_id)
    return conn.execute(
        "SELECT session_id, seq, role, timestamp, text, content_hash, "
        "is_tool_plumbing, authored_by_agent FROM messages WHERE id = ?",
        (message_id,),
    ).fetchone()


def _window_message_bounds(
    conn: sqlite3.Connection,
    session_id: str,
    window: sqlite3.Row,
    source_cache: dict[str, Any] | None = None,
) -> tuple[int, int, int]:
    source_cache = source_cache if source_cache is not None else {}
    source_messages = _source_replay_messages(conn, session_id, source_cache)
    if source_messages is None:
        rows = conn.execute(
            "SELECT id, session_id, seq, role, text, is_tool_plumbing, authored_by_agent "
            "FROM messages WHERE id IN (?, ?)",
            (window["request_message_id"], window["response_message_id"]),
        ).fetchall()
        by_id: Mapping[str, Mapping[str, Any]] = {str(row["id"]): row for row in rows}
        session_messages: Iterable[Mapping[str, Any]] = conn.execute(
            "SELECT id, seq, role, text, is_tool_plumbing, authored_by_agent "
            "FROM messages WHERE session_id = ? AND seq > ? AND role = 'user' "
            "ORDER BY seq, id",
            (session_id, 0),
        ).fetchall()
    else:
        by_id = source_messages
        session_messages = source_messages.values()
    request = by_id.get(str(window["request_message_id"]))
    response = by_id.get(str(window["response_message_id"]))
    if (
        request is None
        or response is None
        or str(request["session_id"]) != session_id
        or str(response["session_id"]) != session_id
        or str(request["role"] or "") != "user"
        or str(response["role"] or "") != "assistant"
        or not isinstance(request["seq"], int)
        or not isinstance(response["seq"], int)
        or int(request["seq"]) > int(response["seq"])
    ):
        raise MaterializationError("window message boundaries differ from the ledger")
    next_owner = next(
        (
            row
            for row in session_messages
            if int(row["seq"]) > int(request["seq"])
            and str(row["role"] or "") == "user"
            and not bool(row["is_tool_plumbing"])
            and not bool(row["authored_by_agent"])
            and not _synthetic_request_kind(str(row["text"] or ""))
        ),
        None,
    )
    window_end = int(next_owner["seq"]) if next_owner is not None else 2**63 - 1
    if int(response["seq"]) >= window_end:
        raise MaterializationError("window response lies outside its owner request")
    return int(request["seq"]), int(response["seq"]), window_end


def _verify_message_evidence(
    conn: sqlite3.Connection,
    evidence: Mapping[str, Any],
    session_id: str,
    window: sqlite3.Row,
    source_cache: dict[str, Any] | None = None,
) -> str:
    source_cache = source_cache if source_cache is not None else {}
    message_id = str(evidence.get("message_id") or "")
    if not message_id or evidence.get("tool_event_id") or evidence.get("skill_exposure_id"):
        raise MaterializationError("message evidence is not owned by its window")
    message = _replay_message(conn, session_id, message_id, source_cache)
    request_seq, _response_seq, window_end = _window_message_bounds(
        conn, session_id, window, source_cache
    )
    if (
        message is None
        or str(message["session_id"]) != session_id
        or not isinstance(message["seq"], int)
        or not request_seq <= int(message["seq"]) < window_end
    ):
        raise MaterializationError("message evidence does not exist in its physical session")
    raw_text = str(message["text"] or "")
    expected_content_hash = str(message["content_hash"] or _sha256(raw_text))
    if (
        str(evidence.get("role") or "") != str(message["role"] or "")
        or evidence.get("seq") != message["seq"]
        or str(evidence.get("timestamp") or "") != str(message["timestamp"] or "")
        or str(evidence.get("source_hash") or "") != _sha256(raw_text)
        or str(evidence.get("content_hash") or "") != expected_content_hash
        or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("emitted_source_hash") or ""))
    ):
        raise MaterializationError("message evidence metadata differs from the ledger")
    quote = str(evidence.get("quote") or "")
    redacted = redact_locator_text(raw_text)
    start = evidence.get("quote_start")
    end = evidence.get("quote_end")
    if (
        not quote
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end != start + len(quote)
        or redacted[start:end] != quote
    ):
        raise MaterializationError("message evidence quote is not an exact redacted source span")
    return str(message["timestamp"] or "")


def _verify_tool_evidence(
    conn: sqlite3.Connection,
    evidence: Mapping[str, Any],
    session_id: str,
    window: sqlite3.Row,
    window_timestamp: str,
    source_cache: dict[str, Any] | None = None,
) -> str:
    source_cache = source_cache if source_cache is not None else {}
    tool_event_id = str(evidence.get("tool_event_id") or "")
    if not tool_event_id or evidence.get("skill_exposure_id"):
        raise MaterializationError("tool evidence identity is malformed")
    tool_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(tool_events)")
    }
    has_operation_kind = "operation_kind" in tool_columns
    operation_select = ", operation_kind" if has_operation_kind else ""
    event = conn.execute(
        "SELECT id, session_id, message_id, seq, tool_name, action, success, duration_ms"
        f"{operation_select} FROM tool_events WHERE id = ?",
        (tool_event_id,),
    ).fetchone()
    _request_seq, _response_seq, window_end = _window_message_bounds(
        conn, session_id, window, source_cache
    )
    linked_message = (
        conn.execute(
            "SELECT session_id, seq, role, timestamp, is_tool_plumbing FROM messages WHERE id = ?",
            (event["message_id"],),
        ).fetchone()
        if event is not None and event["message_id"]
        else None
    )
    if (
        event is None
        or str(event["session_id"]) != session_id
        or not event["message_id"]
        or linked_message is None
        or str(linked_message["session_id"]) != session_id
        or not isinstance(linked_message["seq"], int)
        or not _request_seq <= int(linked_message["seq"]) < window_end
        or not (
            str(linked_message["role"] or "") == "assistant"
            or (
                str(linked_message["role"] or "") == "user"
                and bool(linked_message["is_tool_plumbing"])
            )
        )
        or (
            evidence.get("message_id") is not None
            and str(evidence.get("message_id")) != str(event["message_id"])
        )
    ):
        raise MaterializationError("tool evidence does not exist in its physical window")
    fact = _fact_object(evidence.get("fact"))
    expected = {
        "tool_event_id": str(event["id"]),
        "message_id": event["message_id"],
        "seq": event["seq"],
        "tool_name": redact_locator_text(str(event["tool_name"] or "")),
        "action": redact_locator_text(str(event["action"] or "")),
        "success": event["success"],
        "duration_ms": event["duration_ms"],
        "operation_kind": (
            str(event["operation_kind"] or "unknown")
            if has_operation_kind else "unknown"
        ),
        "message_seq": int(linked_message["seq"]),
        "message_role": str(linked_message["role"] or ""),
        "message_is_tool_plumbing": bool(linked_message["is_tool_plumbing"]),
        "message_timestamp": linked_message["timestamp"],
        "request_seq": _request_seq,
        "window_end_seq": None if window_end == 2**63 - 1 else window_end - 1,
    }
    if fact != expected or str(evidence.get("timestamp") or "") != window_timestamp:
        raise MaterializationError("tool evidence fact differs from the ledger")
    return window_timestamp


def _verify_skill_evidence(
    conn: sqlite3.Connection,
    evidence: Mapping[str, Any],
    session_id: str,
    window: sqlite3.Row,
    window_timestamp: str,
    source_cache: dict[str, Any] | None = None,
) -> str:
    source_cache = source_cache if source_cache is not None else {}
    exposure_id = str(evidence.get("skill_exposure_id") or "")
    if not exposure_id or evidence.get("tool_event_id"):
        raise MaterializationError("skill evidence identity is malformed")
    exposure = conn.execute(
        "SELECT id, session_id, message_id, skill_name, exposure_type "
        "FROM skill_exposures WHERE id = ?",
        (exposure_id,),
    ).fetchone()
    request_seq, _response_seq, window_end = _window_message_bounds(
        conn, session_id, window, source_cache
    )
    linked_message = (
        conn.execute(
            "SELECT session_id, seq, role, timestamp, is_tool_plumbing, authored_by_agent "
            "FROM messages WHERE id = ?",
            (exposure["message_id"],),
        ).fetchone()
        if exposure is not None and exposure["message_id"]
        else None
    )
    if (
        exposure is None
        or str(exposure["session_id"]) != session_id
        or not exposure["message_id"]
        or linked_message is None
        or str(linked_message["session_id"]) != session_id
        or not isinstance(linked_message["seq"], int)
        or not request_seq <= int(linked_message["seq"]) < window_end
        or (
            evidence.get("message_id") is not None
            and str(evidence.get("message_id")) != str(exposure["message_id"])
        )
    ):
        raise MaterializationError("skill evidence does not exist in its physical window")
    exposure_type = str(exposure["exposure_type"] or "").strip().lower().replace("-", "_")
    role = str(linked_message["role"] or "")
    plumbing = bool(linked_message["is_tool_plumbing"])
    attributable = (
        exposure_type == "attached"
        and str(exposure["message_id"] or "") == str(window["request_message_id"])
        and role == "user"
        and not plumbing
        and not bool(linked_message["authored_by_agent"])
    ) or (
        exposure_type == "injected" and role == "user" and plumbing
    ) or (
        exposure_type in {"tool_use", "loaded", "activated", "invoked"}
        and role == "assistant"
    )
    if not attributable:
        raise MaterializationError("skill evidence is not attributable within its window")
    fact = _fact_object(evidence.get("fact"))
    expected = {
        "skill_exposure_id": str(exposure["id"]),
        "message_id": exposure["message_id"],
        "skill_name": redact_locator_text(str(exposure["skill_name"] or "")),
        "exposure_type": redact_locator_text(str(exposure["exposure_type"] or "")),
        "message_seq": int(linked_message["seq"]),
        "message_role": role,
        "message_is_tool_plumbing": plumbing,
        "message_timestamp": linked_message["timestamp"],
        "request_message_id": str(window["request_message_id"]),
    }
    if fact != expected or str(evidence.get("timestamp") or "") != window_timestamp:
        raise MaterializationError("skill evidence fact differs from the ledger")
    return window_timestamp


def _verify_catalog_evidence(
    conn: sqlite3.Connection,
    catalog: Mapping[str, Any],
) -> None:
    observation_index = catalog.get("observation_index")
    if not isinstance(observation_index, Mapping) or not observation_index:
        raise MaterializationError("candidate catalog has no immutable observation index")
    coverage = catalog.get("coverage")
    provenance = catalog.get("provenance")
    packet_provenance = (
        provenance.get("packets") if isinstance(provenance, Mapping) else None
    )
    if not isinstance(coverage, Mapping) or not isinstance(packet_provenance, Mapping):
        raise MaterializationError("candidate catalog packet provenance is missing")
    sessions, logical_roots, backing_shadows = _session_roots(conn)
    source_cache: dict[str, Any] = {}
    for observation_id, raw_observation in observation_index.items():
        if not isinstance(raw_observation, Mapping):
            raise MaterializationError("catalog observation is not an object")
        observation = dict(raw_observation)
        if str(observation.get("observation_id") or "") != str(observation_id):
            raise MaterializationError("catalog observation identity is inconsistent")
        memberships = observation.get("source_synthesis_packets")
        membership_ids = observation.get("source_synthesis_packet_ids")
        if (
            not isinstance(memberships, list)
            or not memberships
            or not isinstance(membership_ids, list)
            or {str(value) for value in membership_ids}
            != {
                str(item.get("packet_id") or "")
                for item in memberships
                if isinstance(item, Mapping)
            }
        ):
            raise MaterializationError("catalog observation synthesis packet lineage is missing")
        for membership in memberships:
            if not isinstance(membership, Mapping):
                raise MaterializationError("catalog observation synthesis packet lineage is malformed")
            packet_id = str(membership.get("packet_id") or "")
            packet_hash = str(membership.get("packet_hash") or "")
            declared_packet = packet_provenance.get(packet_id)
            if (
                not packet_id
                or not packet_hash
                or packet_id not in coverage
                or not isinstance(declared_packet, Mapping)
                or str(declared_packet.get("packet_hash") or "") != packet_hash
            ):
                raise MaterializationError("catalog observation synthesis packet hash is inconsistent")
        evidence_values = observation.get("evidence")
        if not isinstance(evidence_values, list) or not evidence_values:
            raise MaterializationError("catalog observation has no evidence")
        refs: set[str] = set()
        observed_timestamps: list[str] = []
        root_id = str(observation.get("root_session_id") or "")
        for raw_evidence in evidence_values:
            if not isinstance(raw_evidence, Mapping):
                raise MaterializationError("catalog evidence is not an object")
            evidence = dict(raw_evidence)
            ref = str(evidence.get("ref") or "")
            session_id = str(evidence.get("session_id") or "")
            window_id = str(evidence.get("window_id") or "")
            evidence_type = str(evidence.get("evidence_type") or "")
            if not ref or ref in refs:
                raise MaterializationError("catalog evidence ref is missing or repeated")
            refs.add(ref)
            session = sessions.get(session_id)
            window = conn.execute(
                "SELECT id, session_id, request_message_id, response_message_id "
                "FROM exchange_windows WHERE id = ?",
                (window_id,),
            ).fetchone()
            if session is None or window is None or str(window["session_id"]) != session_id:
                raise MaterializationError("catalog evidence physical session or window does not exist")
            expected_root = logical_roots.get(session_id, "")
            if (
                not expected_root
                or root_id != expected_root
                or str(evidence.get("root_session_id") or "") != expected_root
            ):
                raise MaterializationError("catalog evidence logical root differs from the ledger")
            expected_harness = "t3code" if session_id in backing_shadows else str(session["harness"] or "")
            if (
                str(observation.get("harness") or "") != expected_harness
                or str(observation.get("repo") or "") != _repo_label(session["repo"])
            ):
                raise MaterializationError("catalog observation routing differs from the ledger")
            if not _artifact_matches(conn, session, evidence):
                raise MaterializationError("catalog evidence artifact differs from the ledger")
            request = conn.execute(
                "SELECT timestamp FROM messages WHERE id = ?",
                (window["request_message_id"],),
            ).fetchone()
            window_timestamp = str((request["timestamp"] if request else None) or session["started_at"] or "")
            if evidence_type == "message":
                timestamp = _verify_message_evidence(
                    conn, evidence, session_id, window, source_cache
                )
            elif evidence_type == "tool":
                timestamp = _verify_tool_evidence(
                    conn, evidence, session_id, window, window_timestamp, source_cache
                )
            elif evidence_type == "skill":
                timestamp = _verify_skill_evidence(
                    conn, evidence, session_id, window, window_timestamp, source_cache
                )
            else:
                raise MaterializationError("catalog evidence type is unsupported")
            if timestamp:
                observed_timestamps.append(timestamp)
        for arc in observation.get("proof_arcs", []):
            if not isinstance(arc, Mapping):
                raise MaterializationError("catalog proof arc is malformed")
            arc_refs = arc.get("evidence_refs")
            if (
                not str(arc.get("arc") or "")
                or not isinstance(arc_refs, list)
                or not arc_refs
                or any(str(value) not in refs for value in arc_refs)
            ):
                raise MaterializationError("catalog proof arc references unverified evidence")
        if not observed_timestamps:
            raise MaterializationError("catalog observation source time range differs from the ledger")
        observed_start, observed_end = _timestamp_range(observed_timestamps)
        if (
            str(observation.get("observed_at_start") or "") != observed_start
            or str(observation.get("observed_at_end") or "") != observed_end
        ):
            raise MaterializationError("catalog observation source time range differs from the ledger")
        _validate_observation_proof(observation)


def _scope(
    candidate: Mapping[str, Any],
    supporting: list[dict[str, Any]] | None = None,
) -> tuple[str, str | None]:
    if candidate.get("kind") == "observed_instance" and supporting:
        repo = str(supporting[0].get("repo") or "")
        if repo and repo != "(unknown)":
            return "repo", repo
        harness = str(supporting[0].get("harness") or "")
        if harness and harness != "(unknown)":
            return "harness", harness
        raise MaterializationError("observed instance has no bounded repo or harness scope")
    canonical = candidate.get("canonical")
    raw = str(canonical.get("scope") or "") if isinstance(canonical, Mapping) else ""
    if raw in {"global", "corpus", "global_corpus"}:
        return "global", None
    for prefix, scope_type in (
        ("repo_", "repo"),
        ("harness_", "harness"),
        ("model_", "model"),
        ("skill_", "skill"),
        ("user_rules_", "user_rules"),
    ):
        if raw.startswith(prefix) and raw[len(prefix) :]:
            return scope_type, raw[len(prefix) :]
    if raw == "user_rules":
        return "user_rules", None
    raise MaterializationError(f"unsupported canonical scope: {raw or '(missing)'}")


def _observation_theme(observation: Mapping[str, Any]) -> set[str]:
    return _theme_terms(
        observation.get("server_theme")
        or observation.get("assertion_theme")
        or observation.get("assertion_key")
        or observation.get("evidence_family")
    )


def _validate_theme_binding(
    candidate: Mapping[str, Any],
    observations: list[dict[str, Any]],
) -> None:
    canonical = candidate.get("canonical")
    if not isinstance(canonical, Mapping):
        raise MaterializationError("candidate canonical identity is missing")
    candidate_terms = _theme_terms(
        f"{canonical.get('subject') or ''} {canonical.get('predicate') or ''}"
    )
    if not candidate_terms:
        raise MaterializationError("candidate has no normalized assertion theme")
    approved_theme_sets: list[set[str]] = []
    for observation in observations:
        theme = _observation_theme(observation)
        evidence_by_ref = {
            str(item.get("ref") or ""): item
            for item in observation.get("evidence", [])
            if isinstance(item, Mapping) and str(item.get("ref") or "")
        }
        request_evidence: list[Mapping[str, Any]] = []
        for arc in observation.get("proof_arcs", []):
            if not isinstance(arc, Mapping) or str(arc.get("arc") or "") not in _REQUEST_ARCS:
                continue
            request_evidence.extend(
                evidence_by_ref[str(ref)]
                for ref in arc.get("evidence_refs", [])
                if str(ref) in evidence_by_ref
            )
        if request_evidence and not any(
            candidate_terms & _theme_terms(item.get("quote"))
            for item in request_evidence
        ):
            raise MaterializationError(
                "candidate theme is not linked to its owner request evidence"
            )
        tool_evidence = [
            item
            for item in evidence_by_ref.values()
            if item.get("evidence_type") == "tool"
        ]
        skill_evidence = [
            item
            for item in evidence_by_ref.values()
            if item.get("evidence_type") == EVIDENCE_SKILL
        ]
        request_terms = set().union(
            *(_theme_terms(item.get("quote")) for item in request_evidence)
        )
        request_refs = {str(item.get("ref") or "") for item in request_evidence}
        owner_result_evidence = [
            item
            for item in evidence_by_ref.values()
            if item.get("evidence_type") == EVIDENCE_MESSAGE
            and item.get("role") == "user"
            and str(item.get("ref") or "") not in request_refs
        ]
        result_terms = (
            set().union(
                *(
                    _theme_terms(_fact_object(item.get("fact")))
                    for item in [*tool_evidence, *skill_evidence]
                )
            )
            | set().union(
                *(_theme_terms(item.get("quote")) for item in owner_result_evidence)
            )
        )
        approved_terms = request_terms | result_terms
        specific_terms = candidate_terms - _REPEATED_GENERIC_TERMS
        if (
            not theme
            or not candidate_terms <= approved_terms
            or not specific_terms <= result_terms
        ):
            raise MaterializationError(
                "candidate theme differs from its supporting observations"
            )
        approved_theme_sets.append(approved_terms)
        if tool_evidence and not candidate_terms & result_terms:
            raise MaterializationError(
                "candidate theme is not linked to its deterministic tool evidence"
            )
    if candidate.get("kind") == "coach_proposal":
        common_terms = set.intersection(*approved_theme_sets) if approved_theme_sets else set()
        instruction_terms = _instruction_theme(candidate.get("instruction_text"))
        if not instruction_terms or not instruction_terms <= common_terms:
            raise MaterializationError(
                "proposal instruction is not linked to its approved evidence theme"
            )


def _validate_atomic_instruction(value: Any) -> str:
    instruction = _normalized_instruction(value)
    if (
        not instruction
        or "\n" in str(value or "")
        or _BULLET_MARKER.match(instruction)
        or not _ATOMIC_RULE_START.match(instruction)
        or not _ATOMIC_RULE_TRIGGER.search(instruction)
        or not _ATOMIC_RULE_OUTCOME.search(instruction)
        or re.search(
            r"(?:[;,&]|\b(?:and|or|then|also|while|plus|but|whereas|although)\b|"
            r"\bas\s+well\s+as\b)",
            instruction,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:delete|deleting|erase|erasing|remove|removing|deploy|deploying|"
            r"modify|modifying)\b",
            instruction.partition(" ")[2],
            re.IGNORECASE,
        )
        or len(_ATOMIC_RULE_TRIGGER.findall(instruction)) != 1
        or len(
            re.findall(
                r"\b(?:add|archive|check|enforce|record|require|run|update|verify)\b",
                instruction,
                re.IGNORECASE,
            )
        )
        != 1
        or len(re.findall(r"[.!?](?:\s|$)", instruction)) > 1
    ):
        raise MaterializationError(
            "proposal instruction is not one atomic trigger-to-outcome rule"
        )
    return instruction


def _validate_proposal_destination(target_kind: Any, target_path: Any = "") -> None:
    if str(target_kind or "") not in {
        "instruction_file",
        "skill",
        "harness_rule",
    }:
        raise MaterializationError("proposal target kind is not supported")
    if target_path:
        path = Path(str(target_path))
        if not path.is_absolute() or path.suffix.casefold() not in {".md", ".markdown"}:
            raise MaterializationError("proposal target must be an absolute Markdown file")


def _candidate_observations(
    candidate: Mapping[str, Any],
    observation_index: Mapping[str, Any],
    catalog_coverage: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    support_ids = candidate.get("supporting_observation_ids")
    counter_ids = candidate.get("counterevidence_observation_ids")
    if not isinstance(support_ids, list) or not support_ids:
        raise MaterializationError("candidate has no supporting observations")
    if not isinstance(counter_ids, list):
        raise MaterializationError("candidate counterevidence is not a list")
    if len(set(map(str, support_ids))) != len(support_ids):
        raise MaterializationError("candidate repeats supporting observations")
    if len(set(map(str, counter_ids))) != len(counter_ids):
        raise MaterializationError("candidate repeats counterevidence observations")
    if set(map(str, support_ids)) & set(map(str, counter_ids)):
        raise MaterializationError("candidate reuses support as counterevidence")

    def resolve(values: list[Any]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for value in values:
            observation = observation_index.get(str(value))
            if not isinstance(observation, Mapping):
                raise MaterializationError(f"candidate references unknown observation: {value}")
            if str(observation.get("observation_id") or "") != str(value):
                raise MaterializationError("observation index identity is inconsistent")
            resolved.append(dict(observation))
        return resolved

    supporting = resolve(support_ids)
    counter = resolve(counter_ids)
    canonical = candidate.get("canonical")
    canonical_scope = (
        str(canonical.get("scope") or "") if isinstance(canonical, Mapping) else ""
    )
    canonical_polarity = (
        str(canonical.get("polarity") or "") if isinstance(canonical, Mapping) else ""
    )
    if any(
        str(item.get("polarity") or "") != canonical_polarity
        for item in supporting
    ):
        raise MaterializationError("candidate scope or polarity differs from its observations")
    if canonical_scope.startswith("harness_"):
        expected_harness = canonical_scope.removeprefix("harness_")
        if any(
            _NON_WORD.sub("_", str(item.get("harness") or "").casefold()).strip("_")
            != expected_harness
            for item in supporting
        ):
            raise MaterializationError("candidate harness scope differs from its observations")
    elif canonical_scope.startswith("repo_"):
        expected_repo = canonical_scope.removeprefix("repo_")
        if any(
            _NON_WORD.sub("_", str(item.get("repo") or "").casefold()).strip("_")
            != expected_repo
            for item in supporting
        ):
            raise MaterializationError("candidate repo scope differs from its observations")
    elif canonical_scope.startswith("model_"):
        expected_model = canonical_scope.removeprefix("model_")
        if any(
            not isinstance(item.get("model_attribution"), Mapping)
            or bool(item["model_attribution"].get("ambiguous"))
            or _NON_WORD.sub(
                "_",
                str(item["model_attribution"].get("response_model") or "").casefold(),
            ).strip("_")
            != expected_model
            for item in supporting
        ):
            raise MaterializationError("candidate model scope differs from its observations")
    elif canonical_scope not in {"global", "corpus", "global_corpus"} and any(
        str(item.get("scope") or "") != canonical_scope for item in supporting
    ):
        raise MaterializationError("candidate scope differs from its observations")
    declared_packet_ids = candidate.get("source_packet_ids")
    if (
        not isinstance(declared_packet_ids, list)
        or not declared_packet_ids
        or len(set(map(str, declared_packet_ids))) != len(declared_packet_ids)
    ):
        raise MaterializationError("candidate source packet binding is missing")
    packet_ids = {str(value) for value in declared_packet_ids}
    supporting_memberships = [
        {str(value) for value in item.get("source_synthesis_packet_ids", [])}
        for item in supporting
    ]
    counter_memberships = [
        {str(value) for value in item.get("source_synthesis_packet_ids", [])}
        for item in counter
    ]
    if (
        any(not memberships or not packet_ids <= memberships for memberships in supporting_memberships)
        or any(not memberships for memberships in counter_memberships)
    ):
        raise MaterializationError("candidate observations do not match source packet binding")
    _validate_theme_binding(candidate, supporting)
    support_themes = {
        frozenset(_observation_theme(observation)) for observation in supporting
    }
    expected_counter_ids = {
        str(observation_id)
        for observation_id, raw_observation in observation_index.items()
        if isinstance(raw_observation, Mapping)
        and packet_ids
        <= {
            str(value)
            for value in raw_observation.get("source_synthesis_packet_ids", [])
        }
        and str(raw_observation.get("polarity") or "") != canonical_polarity
        and frozenset(_observation_theme(raw_observation)) in support_themes
    }
    if {str(value) for value in counter_ids} != expected_counter_ids:
        raise MaterializationError(
            "candidate does not bind all same-theme counterevidence"
        )
    if counter and not re.search(
        r"\b(?:counter|contrast|mixed|opposite)\b",
        str(candidate.get("does_not_prove") or ""),
        re.IGNORECASE,
    ):
        raise MaterializationError(
            "candidate limitation does not describe its counterevidence"
        )
    packet_coverages: list[dict[str, Any]] = []
    for packet_id in sorted(packet_ids):
        raw_coverage = catalog_coverage.get(packet_id)
        if not isinstance(raw_coverage, Mapping):
            raise MaterializationError("candidate source packet coverage is missing")
        packet_coverages.append(dict(raw_coverage))
    declared_source_coverage = candidate.get("source_packet_coverage")
    if len(packet_coverages) != 1 or declared_source_coverage != packet_coverages[0]:
        raise MaterializationError("candidate source packet coverage is inconsistent")
    roots = {str(item.get("root_session_id") or "") for item in supporting}
    declared_n = candidate.get("n")
    if "" in roots or not isinstance(declared_n, int) or isinstance(declared_n, bool):
        raise MaterializationError("candidate root-cluster count is dishonest")
    harnesses, repos = _distribution(supporting)
    if candidate.get("kind") != "observed_instance":
        population = _validated_candidate_population(candidate)
        support_population = population["supporting"]
        counter_population = population["counterexamples"]
        membership = candidate.get("source_packet_group_membership")
        expected_membership = {
            "supporting_observation_ids": support_population["observation_ids"],
            "supporting_root_session_ids": support_population["root_session_ids"],
            "counterexample_observation_ids": counter_population["observation_ids"],
            "counterexample_root_session_ids": counter_population["root_session_ids"],
        }
        if membership != expected_membership:
            raise MaterializationError("candidate population membership is inconsistent")
        if (
            not set(map(str, support_ids)) <= set(support_population["observation_ids"])
            or not set(map(str, counter_ids)) <= set(counter_population["observation_ids"])
            or candidate.get("cited_supporting_roots") != len(roots)
            or candidate.get("counterevidence_roots") != counter_population["root_count"]
            or candidate.get("counterevidence_observations")
            != counter_population["observation_count"]
        ):
            raise MaterializationError("candidate citations differ from its full population")
        harnesses = dict(support_population["distribution"]["harnesses"])
        repos = dict(support_population["distribution"]["repos"])
        if canonical_scope.startswith("harness_"):
            expected_harness = canonical_scope.removeprefix("harness_")
            if {
                _NON_WORD.sub("_", str(value).casefold()).strip("_")
                for value in harnesses
            } != {expected_harness}:
                raise MaterializationError(
                    "candidate harness scope differs from its full population"
                )
        elif canonical_scope.startswith("repo_"):
            expected_repo = canonical_scope.removeprefix("repo_")
            if {
                _NON_WORD.sub("_", str(value).casefold()).strip("_")
                for value in repos
            } != {expected_repo}:
                raise MaterializationError(
                    "candidate repo scope differs from its full population"
                )
        elif canonical_scope.startswith("model_"):
            expected_model = canonical_scope.removeprefix("model_")
            bindings = support_population["scope_distribution"][
                "observation_bindings"
            ]
            terminal_models = support_population["terminal_model_attribution"]
            response_models = terminal_models.get("response_models")
            if (
                not bindings
                or any(
                    bool(binding.get("model_ambiguous"))
                    or _NON_WORD.sub(
                        "_", str(binding.get("response_model") or "").casefold()
                    ).strip("_")
                    != expected_model
                    for binding in bindings.values()
                )
                or not isinstance(response_models, Mapping)
                or {
                    _NON_WORD.sub("_", str(model).casefold()).strip("_")
                    for model, count in response_models.items()
                    if isinstance(count, int) and count > 0
                }
                != {expected_model}
                or terminal_models.get("ambiguous_observation_ids") not in ([], ())
                or terminal_models.get("unattributed_observation_ids") not in ([], ())
            ):
                raise MaterializationError(
                    "candidate model scope differs from its full population"
                )
        if declared_n != support_population["root_count"]:
            raise MaterializationError("candidate root-cluster count is dishonest")
    elif len(roots) != declared_n:
        raise MaterializationError("candidate root-cluster count is dishonest")
    if candidate.get("distribution") != {"harnesses": harnesses, "repos": repos}:
        raise MaterializationError("candidate root distribution is dishonest")
    denominator = candidate.get("denominator")
    if not isinstance(denominator, int) or isinstance(denominator, bool):
        raise MaterializationError("candidate denominator is invalid")
    eligible = packet_coverages[0].get("eligible_roots")
    processed = packet_coverages[0].get("processed_roots")
    if denominator != eligible or declared_n > denominator:
        raise MaterializationError("candidate denominator does not match coverage")
    declared_processed = candidate.get("processed_roots")
    declared_eligible = candidate.get("eligible_roots")
    if (
        not isinstance(declared_processed, int)
        or isinstance(declared_processed, bool)
        or not isinstance(processed, int)
        or isinstance(processed, bool)
        or not isinstance(eligible, int)
        or isinstance(eligible, bool)
        or declared_eligible != eligible
        or declared_processed != processed
        or declared_n > processed
    ):
        raise MaterializationError("candidate processing coverage is dishonest")
    return supporting, counter


def _validated_candidate_population(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw = candidate.get("source_packet_population")
    if not isinstance(raw, Mapping):
        raise MaterializationError("candidate full population is missing")
    body = {key: value for key, value in raw.items() if key != "hash"}
    declared_hash = str(raw.get("hash") or "")
    if (
        not declared_hash
        or declared_hash != _sha256(body)
        or str(candidate.get("population_hash") or "") != declared_hash
    ):
        raise MaterializationError("candidate full population hash is invalid")
    normalized: dict[str, Any] = {}
    for label in ("supporting", "counterexamples"):
        member = raw.get(label)
        if not isinstance(member, Mapping):
            raise MaterializationError("candidate full population is malformed")
        member_body = {key: value for key, value in member.items() if key != "hash"}
        observation_ids = member.get("observation_ids")
        root_ids = member.get("root_session_ids")
        distribution = member.get("distribution")
        observation_count = member.get("observation_count")
        root_count = member.get("root_count")
        if (
            str(member.get("hash") or "") != _sha256(member_body)
            or not isinstance(observation_ids, list)
            or not isinstance(root_ids, list)
            or any(not str(value) for value in [*observation_ids, *root_ids])
            or [str(value) for value in observation_ids]
            != sorted({str(value) for value in observation_ids})
            or [str(value) for value in root_ids]
            != sorted({str(value) for value in root_ids})
            or observation_count != len(observation_ids)
            or root_count != len(root_ids)
            or not isinstance(distribution, Mapping)
            or not isinstance(distribution.get("harnesses"), Mapping)
            or not isinstance(distribution.get("repos"), Mapping)
        ):
            raise MaterializationError("candidate full population is malformed")
        harnesses = dict(distribution["harnesses"])
        repos = dict(distribution["repos"])
        scope_distribution = member.get("scope_distribution")
        terminal_models = member.get("terminal_model_attribution")
        bindings = (
            scope_distribution.get("observation_bindings")
            if isinstance(scope_distribution, Mapping)
            else None
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in [*harnesses.values(), *repos.values()]
        ) or sum(harnesses.values()) != root_count or sum(repos.values()) != root_count:
            raise MaterializationError("candidate population distribution is invalid")
        if (
            not isinstance(bindings, Mapping)
            or set(map(str, bindings)) != set(map(str, observation_ids))
            or not isinstance(terminal_models, Mapping)
            or any(
                not isinstance(binding, Mapping)
                or set(binding)
                != {
                    "scope",
                    "harness",
                    "repo",
                    "response_model",
                    "model_ambiguous",
                }
                or not isinstance(binding.get("model_ambiguous"), bool)
                for binding in bindings.values()
            )
        ):
            raise MaterializationError("candidate population routing is malformed")
        normalized[label] = {
            "observation_ids": [str(value) for value in observation_ids],
            "root_session_ids": [str(value) for value in root_ids],
            "observation_count": observation_count,
            "root_count": root_count,
            "distribution": {"harnesses": harnesses, "repos": repos},
            "scope_distribution": dict(scope_distribution),
            "terminal_model_attribution": dict(terminal_models),
            "hash": str(member["hash"]),
        }
    normalized["hash"] = declared_hash
    return normalized


def _canonical_contract(candidate: Mapping[str, Any]) -> None:
    kind = str(candidate.get("kind") or "")
    if kind not in {*_PRODUCT_KINDS, "coach_proposal"}:
        raise MaterializationError("candidate kind is not materializable")
    canonical = candidate.get("canonical")
    if not isinstance(canonical, Mapping):
        raise MaterializationError("candidate canonical identity is missing")
    parts = [str(canonical.get(key) or "") for key in ("scope", "subject", "predicate", "polarity")]
    if (
        any(not _CANONICAL_PART.fullmatch(value) for value in parts)
        or parts[-1] not in {"positive", "negative", "mixed"}
        or str(candidate.get("canonical_key") or "") != ":".join(parts)
    ):
        raise MaterializationError("candidate canonical identity is inconsistent")
    for key in ("title", "summary", "does_not_prove"):
        value = _normalized_instruction(candidate.get(key))
        if not value or _BULLET_MARKER.match(value):
            raise MaterializationError(f"candidate {key} is empty or bulleted")


def _validate_sampling_gate(candidate: Mapping[str, Any]) -> None:
    if candidate.get("kind") == "observed_instance":
        return
    sampling = candidate.get("source_packet_sampling")
    fields = {
        "supporting_observations_truncated",
        "supporting_roots_truncated",
        "counterexample_observations_truncated",
        "counterexample_roots_truncated",
    }
    if (
        not isinstance(sampling, Mapping)
        or set(sampling) != fields
        or any(not isinstance(sampling.get(field), bool) for field in fields)
    ):
        raise MaterializationError(
            "candidate citation sampling declaration is malformed"
        )
    _validated_candidate_population(candidate)


def _validate_complete_processing(candidate: Mapping[str, Any]) -> None:
    if candidate.get("kind") == "observed_instance":
        return
    coverage = candidate.get("source_packet_coverage")
    if not isinstance(coverage, Mapping):
        raise MaterializationError("candidate source packet coverage is missing")
    fields = (
        "eligible_roots",
        "processed_roots",
        "eligible_windows",
        "selected_windows",
        "processed_windows",
    )
    values = [coverage.get(field) for field in fields]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in values
    ):
        raise MaterializationError("candidate processing coverage is invalid")
    (
        eligible_roots,
        processed_roots,
        eligible_windows,
        selected_windows,
        processed_windows,
    ) = values
    if (
        coverage.get("publication_mode") != "full"
        or coverage.get("publication_complete") is not True
        or coverage.get("source_truncated_messages") != 0
        or
        processed_roots != eligible_roots
        or selected_windows != eligible_windows
        or processed_windows != eligible_windows
    ):
        raise MaterializationError(
            "partial root or window processing cannot promote a pattern or proposal"
        )


def _normalize_candidate_scope(candidate: dict[str, Any]) -> dict[str, Any]:
    canonical = candidate.get("canonical")
    if not isinstance(canonical, Mapping):
        return candidate
    normalized = dict(canonical)
    if normalized.get("scope") in {"corpus", "global_corpus"}:
        normalized["scope"] = "global"
        candidate["canonical"] = normalized
        candidate["canonical_key"] = ":".join(
            str(normalized.get(key) or "")
            for key in ("scope", "subject", "predicate", "polarity")
        )
        pattern_key = str(candidate.get("pattern_canonical_key") or "")
        if pattern_key.startswith("corpus:"):
            candidate["pattern_canonical_key"] = "global:" + pattern_key.split(":", 1)[1]
        elif pattern_key.startswith("global_corpus:"):
            candidate["pattern_canonical_key"] = "global:" + pattern_key.split(":", 1)[1]
    return candidate


def _distribution(observations: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    roots: dict[str, dict[str, Any]] = {}
    for observation in observations:
        root = str(observation.get("root_session_id") or "")
        if root:
            roots.setdefault(root, observation)
    harnesses: dict[str, int] = {}
    repos: dict[str, int] = {}
    for observation in roots.values():
        harness = str(observation.get("harness") or "(unknown)")
        repo = str(observation.get("repo") or "(unknown)")
        harnesses[harness] = harnesses.get(harness, 0) + 1
        repos[repo] = repos.get(repo, 0) + 1
    return dict(sorted(harnesses.items())), dict(sorted(repos.items()))


def _validate_global_gate(
    candidate: Mapping[str, Any], supporting: list[dict[str, Any]]
) -> None:
    if candidate.get("kind") == "observed_instance":
        return
    canonical = candidate.get("canonical")
    if (
        not isinstance(canonical, Mapping)
        or canonical.get("scope") not in {"global", "corpus", "global_corpus"}
    ):
        return
    n = int(candidate.get("n") or 0)
    distribution = candidate.get("distribution")
    if not isinstance(distribution, Mapping):
        raise MaterializationError("global routing distribution is missing")
    harnesses = dict(distribution.get("harnesses") or {})
    repos = dict(distribution.get("repos") or {})
    known_harnesses = {
        key: count for key, count in harnesses.items() if key not in {"", "(unknown)"}
    }
    known_repos = {
        key: count for key, count in repos.items() if key not in {"", "(unknown)"}
    }
    if (
        n < 15
        or len(known_harnesses) < 2
        or len(known_repos) < 2
        or max(harnesses.values(), default=0) / n > 0.70
        or max(repos.values(), default=0) / n > 0.70
    ):
        raise MaterializationError("global routing gate failed during promotion")


def _observation_window_order(
    evidence: list[Mapping[str, Any]],
) -> dict[str, int]:
    window_timestamps: dict[str, list[str]] = {}
    for item in evidence:
        window_id = str(item.get("window_id") or "")
        if window_id:
            window_timestamps.setdefault(window_id, []).append(
                str(item.get("timestamp") or "")
            )
    instants = {
        window_id: min(
            _timestamp_key(value)[0] for value in window_timestamps[window_id]
        )
        for window_id in window_timestamps
    }
    ranks = {instant: index for index, instant in enumerate(sorted(set(instants.values())))}
    return {window_id: ranks[instant] for window_id, instant in instants.items()}


def _ordered_response(
    requests: list[Mapping[str, Any]],
    responses: list[Mapping[str, Any]],
    window_order: Mapping[str, int],
) -> bool:
    return any(
        (
            str(request.get("window_id") or "")
            == str(response.get("window_id") or "")
            and (
                response.get("evidence_type") == "tool"
                or (
                    response.get("evidence_type") == EVIDENCE_MESSAGE
                    and isinstance(request.get("seq"), int)
                    and isinstance(response.get("seq"), int)
                    and int(request["seq"]) < int(response["seq"])
                )
            )
        )
        or (
            response.get("evidence_type") == EVIDENCE_MESSAGE
            and response.get("role") == "user"
            and window_order.get(str(response.get("window_id") or ""), -1)
            > window_order.get(str(request.get("window_id") or ""), -1)
        )
        for request in requests
        if request.get("evidence_type") == EVIDENCE_MESSAGE
        and request.get("role") == "user"
        for response in responses
    )


def _ordered_matching_request(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    window_order: Mapping[str, int],
) -> bool:
    first_terms = _theme_terms(first.get("quote"))
    second_terms = _theme_terms(second.get("quote"))
    shared = first_terms & second_terms
    if not shared or not (
        shared - _REPEATED_GENERIC_TERMS or first_terms == second_terms
    ):
        return False
    first_session = str(first.get("session_id") or "")
    second_session = str(second.get("session_id") or "")
    if first_session and second_session and first_session == second_session:
        return (
            isinstance(first.get("seq"), int)
            and not isinstance(first.get("seq"), bool)
            and isinstance(second.get("seq"), int)
            and not isinstance(second.get("seq"), bool)
            and int(first["seq"]) < int(second["seq"])
        )
    if first.get("window_id") == second.get("window_id"):
        return (
            isinstance(first.get("seq"), int)
            and not isinstance(first.get("seq"), bool)
            and isinstance(second.get("seq"), int)
            and not isinstance(second.get("seq"), bool)
            and int(first["seq"]) < int(second["seq"])
        )
    return window_order.get(str(second.get("window_id") or ""), -1) > window_order.get(
        str(first.get("window_id") or ""), -1
    )


def _validate_observation_proof(observation: Mapping[str, Any]) -> None:
    kind = str(observation.get("kind") or "")
    required = _REQUIRED_OBSERVATION_ARCS.get(kind)
    if required is None:
        raise MaterializationError(f"unsupported source observation kind: {kind or '(missing)'}")
    evidence = [
        item for item in observation.get("evidence", []) if isinstance(item, Mapping)
    ]
    by_ref = {str(item.get("ref") or ""): item for item in evidence}
    arcs: dict[str, list[Mapping[str, Any]]] = {}
    for raw_arc in observation.get("proof_arcs", []):
        if not isinstance(raw_arc, Mapping):
            raise MaterializationError("source observation proof arc is malformed")
        label = str(raw_arc.get("arc") or "")
        if label in arcs:
            raise MaterializationError("source observation repeats a proof arc")
        refs = raw_arc.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise MaterializationError("source observation proof arc has no evidence")
        arcs[label] = [by_ref[str(ref)] for ref in refs if str(ref) in by_ref]
        if len(arcs[label]) != len(refs):
            raise MaterializationError("source observation proof arc is not evidence-bound")
    if set(arcs) != set(required):
        raise MaterializationError("source observation proof arcs do not match its kind")
    window_order = _observation_window_order(evidence)

    if kind in {"instruction_follow", "instruction_miss"}:
        request_windows = {
            str(item.get("window_id") or "") for item in arcs["request"]
        }
        if not _ordered_response(arcs["request"], arcs["response"], window_order):
            raise MaterializationError("instruction observation has no ordered response")
        predicate = supports_successful_result if kind == "instruction_follow" else supports_bounded_gap
        result_arc = "outcome" if kind == "instruction_follow" else "gap"
        response_owners = {
            (
                str(item.get("window_id") or ""),
                str(item.get("message_id") or ""),
            )
            for item in arcs["response"]
            if item.get("evidence_type") == EVIDENCE_MESSAGE
            and item.get("role") == "assistant"
        }
        if any(
            (
                str(item.get("window_id") or ""),
                str(_fact_object(item.get("fact")).get("message_id") or ""),
            )
            not in response_owners
            for item in arcs[result_arc]
            if item.get("evidence_type") == "tool"
        ):
            raise MaterializationError(
                "instruction observation tool result is not owned by its response"
            )
        if not _ordered_response(
            arcs["request"], arcs[result_arc], window_order
        ):
            raise MaterializationError(
                "instruction observation result does not follow its owner request"
            )
        if not predicate(
            arcs[result_arc],
            request_window_ids=request_windows,
            window_order=window_order,
            request_evidence=arcs["request"],
        ):
            raise MaterializationError("instruction observation lacks deterministic result proof")
        return
    if kind == "delivery_gap":
        request_windows = {
            str(item.get("window_id") or "") for item in arcs["expectation"]
        }
        if not _ordered_response(arcs["expectation"], arcs["delivery"], window_order):
            raise MaterializationError("delivery gap has no ordered delivery evidence")
        if not supports_bounded_gap(
            arcs["delivery"],
            request_window_ids=request_windows,
            window_order=window_order,
            request_evidence=arcs["expectation"],
        ):
            raise MaterializationError("delivery gap lacks failed terminal or owner-correction proof")
        return
    if kind == "verification":
        request_windows = {
            str(item.get("window_id") or "")
            for item in arcs["verification_request"]
        }
        if not _ordered_response(
            arcs["verification_request"], arcs["verification_result"], window_order
        ):
            raise MaterializationError("verification has no ordered result evidence")
        if not supports_verification_result(
            arcs["verification_result"],
            request_window_ids=request_windows,
            window_order=window_order,
            request_evidence=arcs["verification_request"],
        ):
            raise MaterializationError("verification lacks a test result or owner confirmation")
        return
    if kind == "repeated_ask":
        first = arcs["request_1"]
        second = arcs["request_2"]
        if (
            not all(
                item.get("evidence_type") == EVIDENCE_MESSAGE
                and item.get("role") == "user"
                for item in [*first, *second]
            )
            or {(item.get("window_id"), item.get("message_id")) for item in first}
            & {(item.get("window_id"), item.get("message_id")) for item in second}
            or not any(
                _ordered_matching_request(first_item, second_item, window_order)
                for first_item in first
                for second_item in second
            )
        ):
            raise MaterializationError("repeated ask lacks two ordered owner requests")
        return
    if kind == "skill_use":
        requests = [
            item
            for item in arcs["skill_request"]
            if item.get("evidence_type") == EVIDENCE_MESSAGE
            and item.get("role") == "user"
        ]
        skills = [
            item
            for item in arcs["skill_evidence"]
            if item.get("evidence_type") == EVIDENCE_SKILL
        ]
        attributable = supports_skill_action(
            arcs["skill_action"],
            skill_evidence=skills,
            request_evidence=requests,
        )
        if (
            len(requests) != len(arcs["skill_request"])
            or len(skills) != len(arcs["skill_evidence"])
            or not attributable
        ):
            raise MaterializationError("skill use lacks exposure and attributable tool action")
        return
    actions = arcs["action"]
    artifacts = arcs["artifact"]
    if not all(
        item.get("evidence_type") == EVIDENCE_TOOL
        and is_successful_artifact_result(item)
        for item in actions
    ):
        raise MaterializationError("process fact action cannot rely on assistant self-report")
    if not all(
        item.get("evidence_type") == EVIDENCE_TOOL
        and is_successful_artifact_result(item)
        for item in artifacts
    ):
        raise MaterializationError("process fact lacks a successful artifact-producing tool result")
    action_events = {
        (str(item.get("window_id") or ""), str(item.get("tool_event_id") or ""))
        for item in actions
    }
    artifact_events = {
        (str(item.get("window_id") or ""), str(item.get("tool_event_id") or ""))
        for item in artifacts
    }
    if not action_events & artifact_events:
        raise MaterializationError("process fact action and artifact are not causally bound")


def _validate_observation_proofs(observations: list[dict[str, Any]]) -> None:
    for observation in observations:
        _validate_observation_proof(observation)


def _source_time_range(observations: list[dict[str, Any]]) -> dict[str, str | None]:
    timestamps = list(
        {
            timestamp
            for observation in observations
            for timestamp in [
                str(observation.get("observed_at_start") or ""),
                str(observation.get("observed_at_end") or ""),
                *[
                    str(item.get("timestamp") or "")
                    for item in observation.get("evidence", [])
                    if isinstance(item, Mapping)
                ],
            ]
            if timestamp
        }
    )
    if not timestamps:
        return {"first": None, "last": None}
    first, last = _timestamp_range(timestamps)
    return {"first": first, "last": last}


def _claim_evidence(
    supporting: list[dict[str, Any]],
    counter: list[dict[str, Any]],
) -> list[ClaimEvidence]:
    evidence: list[ClaimEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for role, observations in (("support", supporting), ("counterevidence", counter)):
        for observation in observations:
            observation_id = str(observation.get("observation_id") or "")
            root_id = str(observation.get("root_session_id") or "") or None
            hashes = observation.get("provenance_hashes")
            provenance_hashes = dict(hashes) if isinstance(hashes, Mapping) else {}
            arcs = [dict(item) for item in observation.get("proof_arcs", []) if isinstance(item, Mapping)]
            for raw in observation.get("evidence", []):
                if not isinstance(raw, Mapping):
                    continue
                physical_session_id = str(raw.get("session_id") or "")
                if not physical_session_id:
                    raise MaterializationError("evidence ref has no physical session_id")
                window_id = str(raw.get("window_id") or "") or None
                message_id = str(raw.get("message_id") or "") or None
                quote = str(raw.get("quote") or "").strip() or None
                ref = str(raw.get("ref") or "") or ":".join(
                    filter(
                        None,
                        (
                            window_id,
                            message_id,
                            str(raw.get("tool_event_id") or ""),
                            str(raw.get("skill_exposure_id") or ""),
                        ),
                    )
                )
                key = (role, observation_id, ref)
                if key in seen:
                    continue
                seen.add(key)
                evidence.append(
                    ClaimEvidence(
                        session_id=physical_session_id,
                        window_id=window_id,
                        message_id=message_id,
                        quote=quote,
                        meta={
                            "evidence_role": role,
                            "observation_id": observation_id,
                            "logical_root_session_id": root_id,
                            "ref": ref,
                            "evidence_type": str(raw.get("evidence_type") or ""),
                            "role": str(raw.get("role") or ""),
                            "seq": raw.get("seq"),
                            "timestamp": raw.get("timestamp"),
                            "tool_event_id": raw.get("tool_event_id"),
                            "skill_exposure_id": raw.get("skill_exposure_id"),
                            "fact": raw.get("fact"),
                            "proof_arcs": arcs,
                            "provenance_hashes": provenance_hashes,
                        },
                    )
                )
    if not evidence:
        raise MaterializationError("candidate observations contain no exact evidence")
    return evidence


def _semantic_identity(candidate: Mapping[str, Any]) -> str:
    return f"coach:{candidate['kind']}:{candidate['canonical_key']}"


def _version_payload(
    candidate: Mapping[str, Any],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    stable_candidate = dict(candidate)
    stable_candidate.pop("source_packet_ids", None)
    stable_candidate.pop("source_packet_coverage", None)
    stable_observations: list[dict[str, Any]] = []
    for observation in observations:
        stable = dict(observation)
        for key in (
            "source_packet_id",
            "source_result_id",
            "source_synthesis_packet_id",
            "source_synthesis_packet_hash",
            "source_synthesis_packet_ids",
            "source_synthesis_packets",
        ):
            stable.pop(key, None)
        hashes = stable.get("provenance_hashes")
        if isinstance(hashes, Mapping):
            stable_hashes = dict(hashes)
            stable_hashes.pop("packet_hash", None)
            stable["provenance_hashes"] = stable_hashes
        stable_observations.append(stable)
    return {
        "candidate": stable_candidate,
        "observations": sorted(
            stable_observations,
            key=lambda item: str(item.get("observation_id") or ""),
        ),
        "materializer_version": MATERIALIZER_VERSION,
    }


def _claim_from_candidate(
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    catalog: Mapping[str, Any],
    supporting: list[dict[str, Any]],
    counter: list[dict[str, Any]],
    now: str,
    replay_provenance: Mapping[str, Any],
) -> Claim:
    kind = str(candidate.get("kind") or "")
    _validate_sampling_gate(candidate)
    _validate_complete_processing(candidate)
    n = int(candidate["n"])
    if kind == "observed_instance" and n != 1:
        raise MaterializationError("observed instance must have exactly one root cluster")
    if kind == "corpus_pattern" and n < 5:
        raise MaterializationError("corpus pattern needs at least five root clusters")
    processed_roots = int(candidate.get("processed_roots") or 0)
    eligible_roots = int(candidate.get("eligible_roots") or 0)
    if kind == "corpus_pattern" and processed_roots != eligible_roots:
        raise MaterializationError("partial processing cannot promote a corpus pattern")
    _canonical_contract(candidate)
    _validate_global_gate(candidate, supporting)
    _validate_observation_proofs(supporting)
    scope_type, scope_id = _scope(candidate, supporting)
    canonical = dict(candidate["canonical"])
    identity = _semantic_identity(candidate)
    version_payload = _version_payload(candidate, [*supporting, *counter])
    version_hash = _sha256(version_payload)
    claim_id = f"coach:{kind}:{_sha256(identity + ':' + version_hash)[:24]}"
    coverage = dict(catalog.get("coverage") or {})
    provenance = {
        "provider": "coach_pipeline",
        "semantic_identity": identity,
        "version_hash": version_hash,
        "candidate_id": str(candidate["candidate_id"]),
        "packet_id": str((candidate.get("source_packet_ids") or [""])[0]),
        "source_packet_ids": list(candidate.get("source_packet_ids") or []),
        "canonical_key": str(candidate["canonical_key"]),
        "catalog_id": str(catalog["catalog_id"]),
        "catalog_hash": str(catalog["catalog_hash"]),
        "source_synthesis_manifest_hash": str(catalog.get("source_synthesis_manifest_hash") or ""),
        "review_id": str(decision.get("review_id") or ""),
        "review_reason": str(decision.get("reason") or ""),
        "review_decision": "accept",
        "observation_ids": list(candidate["supporting_observation_ids"]),
        "counterevidence_observation_ids": list(candidate["counterevidence_observation_ids"]),
        "root_cluster_count": n,
        "full_eligible_root_denominator": int(candidate["denominator"]),
        "processed_roots": processed_roots,
        "eligible_roots": eligible_roots,
        "coverage_state": (
            "complete"
            if processed_roots == eligible_roots
            and replay_provenance.get("publication_complete") is True
            else "partial"
        ),
        "publication_mode": str(replay_provenance.get("publication_mode") or ""),
        "publication_complete": replay_provenance.get("publication_complete"),
        "source_truncated_messages": replay_provenance.get(
            "source_truncated_messages"
        ),
        "population_hash": str(candidate.get("population_hash") or ""),
        "support_distribution": dict(candidate.get("distribution") or {}),
        "selection_method": str(replay_provenance.get("selection_method") or ""),
        "selection_caveat": str(replay_provenance.get("selection_caveat") or ""),
        "proof_capability_by_harness": dict(
            replay_provenance.get("proof_capability_by_harness") or {}
        ),
        "coverage": coverage,
        "catalog_provenance": dict(catalog.get("provenance") or {}),
        "materializer_version": MATERIALIZER_VERSION,
        "validator_version": MATERIALIZER_VERSION,
        "run_replay": dict(replay_provenance),
    }
    time_range = _source_time_range([*supporting, *counter])
    if time_range["last"] is None:
        raise MaterializationError("candidate observations lack a source timestamp")
    if kind == "observed_instance":
        harnesses, repos = _distribution(supporting)
        counter_roots = len(
            {str(item.get("root_session_id") or "") for item in counter}
        )
    else:
        distribution = dict(candidate["distribution"])
        harnesses = dict(distribution["harnesses"])
        repos = dict(distribution["repos"])
        counter_roots = int(candidate.get("counterevidence_roots") or 0)
    provenance["source_time_range"] = time_range
    return Claim(
        id=claim_id,
        kind=f"coach_{kind}",
        subject=str(canonical["subject"]),
        predicate=str(canonical["predicate"]),
        value={
            "product": kind,
            "title": str(candidate["title"]),
            "summary": str(candidate["summary"]),
            "canonical": canonical,
            "supporting_root_clusters": n,
            "full_eligible_root_denominator": int(candidate["denominator"]),
            "observed_rate": n / int(candidate["denominator"]),
            "coverage": coverage,
            "distribution": {"harnesses": harnesses, "repos": repos},
            "source_time_range": time_range,
            "counterevidence_root_clusters": counter_roots,
        },
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_id=scope_id,
        derivation="llm_derived",
        status="approved",
        support_status="ok",
        sample_size=n,
        denominator=int(candidate["denominator"]),
        rate=n / int(candidate["denominator"]),
        observed_at=str(time_range["last"] or ""),
        extractor_name="coach_materializer",
        extractor_version=MATERIALIZER_VERSION,
        confidence_basis=provenance,
        does_not_prove=str(candidate["does_not_prove"]),
        evidence=_claim_evidence(supporting, counter),
        created_at=now,
        updated_at=now,
    )


def _normalized_instruction(value: Any) -> str:
    return _SPACE.sub(" ", str(value or "").strip())


def _hash_matches(data: bytes, expected: str) -> bool:
    raw = expected.strip().lower()
    if ":" in raw:
        algorithm, raw = raw.split(":", 1)
    else:
        algorithm = "sha1" if len(raw) == 40 else "sha256"
    if algorithm not in {"sha1", "sha256"}:
        return False
    return hashlib.new(algorithm, data).hexdigest() == raw


def _config_gap_is_valid(candidate: Mapping[str, Any]) -> bool:
    gap = candidate.get("config_gap")
    if not isinstance(gap, Mapping) or gap.get("available") is not True:
        return False
    if gap.get("matches") not in ([], ()):
        return False
    searched = gap.get("searched")
    if not isinstance(searched, list) or not searched:
        return False
    target_ref = str(candidate.get("target_ref") or "")
    base_hash = str(candidate.get("base_content_hash") or "")
    selected = gap.get("selected_target")
    if not isinstance(selected, Mapping):
        return False
    if (
        str(selected.get("target_ref") or "") != target_ref
        or str(selected.get("fingerprint") or "") != base_hash
        or str(selected.get("target_kind") or "")
        != str(candidate.get("target_kind") or "")
    ):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("target_ref") or "") == target_ref
        and str(item.get("fingerprint") or item.get("content_hash") or "") == base_hash
        and str(item.get("target_kind") or "")
        == str(candidate.get("target_kind") or "")
        for item in searched
    )


def _target_ref(path: str, fingerprint: str) -> str:
    canonical_path = str(Path(path).expanduser().resolve())
    return "target_" + hashlib.sha256(
        (canonical_path + "\0" + fingerprint).encode("utf-8", errors="replace")
    ).hexdigest()[:24]


def _bound_target_index(
    catalog: Mapping[str, Any],
    config_target_map: Mapping[str, Any] | None,
) -> dict[str, dict[str, str]]:
    provenance = catalog.get("provenance")
    binding = (
        provenance.get("private_config_target_map")
        if isinstance(provenance, Mapping)
        else None
    )
    expected_hash = (
        str(binding.get("hash") or "") if isinstance(binding, Mapping) else ""
    )
    if config_target_map is None:
        return {}
    if not expected_hash or _sha256(config_target_map) != expected_hash:
        raise MaterializationError("private config target map hash does not match catalog")
    targets = config_target_map.get("targets")
    if not isinstance(targets, list):
        raise MaterializationError("private config target map has no targets")
    index: dict[str, dict[str, str]] = {}
    for raw in targets:
        if not isinstance(raw, Mapping):
            raise MaterializationError("private config target entry is invalid")
        target_ref = str(raw.get("target_ref") or "")
        target_path = str(raw.get("target_path") or "")
        fingerprint = str(raw.get("fingerprint") or "")
        target_kind = str(raw.get("target_kind") or "")
        target = Path(target_path).expanduser()
        if (
            not target_ref
            or not target_path
            or not target.is_absolute()
            or not fingerprint
            or target_ref != _target_ref(target_path, fingerprint)
            or target_kind
            not in {"instruction_file", "config", "skill", "harness_rule"}
            or target_ref in index
        ):
            raise MaterializationError("private config target entry is not self-consistent")
        index[target_ref] = {
            "target_ref": target_ref,
            "target_path": str(Path(target_path).expanduser().resolve()),
            "fingerprint": fingerprint,
            "target_kind": target_kind,
        }
    return index


def _validate_miss_arcs(
    candidate: Mapping[str, Any], supporting: list[dict[str, Any]]
) -> None:
    by_id = {str(item.get("observation_id") or ""): item for item in supporting}
    raw_arcs = candidate.get("miss_proof_arcs")
    if not isinstance(raw_arcs, list):
        raise MaterializationError("proposal miss proof arcs are missing")
    verified_roots: set[str] = set()
    for raw in raw_arcs:
        if not isinstance(raw, Mapping):
            continue
        observation_id = str(raw.get("observation_id") or "")
        arc_name = str(raw.get("arc") or "")
        observation = by_id.get(observation_id)
        if observation is None or arc_name not in _MISS_ARCS.get(
            str(observation.get("kind") or ""), frozenset()
        ):
            continue
        if any(
            isinstance(arc, Mapping) and str(arc.get("arc") or "") == arc_name
            for arc in observation.get("proof_arcs", [])
        ):
            root_id = str(observation.get("root_session_id") or "")
            if root_id:
                verified_roots.add(root_id)
    if len(verified_roots) < 3:
        raise MaterializationError("proposal needs three independently verified miss proof arcs")


def _validate_proof_capability(
    replay_provenance: Mapping[str, Any],
    supporting: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    raw = replay_provenance.get("proof_capability_by_harness")
    if not isinstance(raw, Mapping) or not raw:
        raise MaterializationError("proposal proof capability coverage is missing")
    capability = {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, Mapping)
    }
    supporting_roots: dict[str, set[str]] = {}
    for observation in supporting:
        harness = str(observation.get("harness") or "")
        root_id = str(observation.get("root_session_id") or "")
        if not harness or not root_id:
            raise MaterializationError("proposal support lacks routed proof capability")
        supporting_roots.setdefault(harness, set()).add(root_id)
    for harness, root_ids in supporting_roots.items():
        entry = capability.get(harness)
        if (
            entry is None
            or entry.get("adapter_capability") != "supported"
            or int(entry.get("processed_roots") or 0)
            < int(entry.get("eligible_roots") or 0)
            or int(entry.get("proof_capable_roots") or 0) < len(root_ids)
        ):
            raise MaterializationError(
                "proposal proof capability is incomplete for a supporting harness"
            )
    return capability


def _validate_pattern_authorization(
    candidate: Mapping[str, Any], pattern_claim: Claim
) -> None:
    canonical_key = str(candidate.get("canonical_key") or "")
    pattern_key = str(candidate.get("pattern_canonical_key") or "")
    claim_key = str(pattern_claim.confidence_basis.get("canonical_key") or "")
    if (
        not canonical_key
        or pattern_key != canonical_key
        or claim_key != canonical_key
        or pattern_claim.kind != "coach_corpus_pattern"
    ):
        raise MaterializationError(
            "proposal is not authorized by its exact approved corpus pattern"
        )


def _proposal_from_candidate(
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    catalog: Mapping[str, Any],
    pattern_claim: Claim,
    now: str,
    supporting: list[dict[str, Any]],
    target_index: Mapping[str, Mapping[str, str]],
    replay_provenance: Mapping[str, Any],
) -> Proposal:
    action = str(candidate.get("action") or "")
    _validate_sampling_gate(candidate)
    _validate_complete_processing(candidate)
    _validate_pattern_authorization(candidate, pattern_claim)
    if action != "add":
        raise MaterializationError("coach materializer only supports additive instruction proposals")
    _validate_proposal_destination(candidate.get("target_kind"))
    _canonical_contract(candidate)
    if int(candidate.get("n") or 0) < 10:
        raise MaterializationError("coach proposal needs at least ten root clusters")
    processed_roots = int(candidate.get("processed_roots") or 0)
    eligible_roots = int(candidate.get("eligible_roots") or 0)
    if processed_roots != eligible_roots:
        raise MaterializationError("partial processing cannot promote a coach proposal")
    proof_capability = _validate_proof_capability(replay_provenance, supporting)
    _validate_global_gate(candidate, supporting)
    _validate_observation_proofs(supporting)
    _validate_miss_arcs(candidate, supporting)
    if not _config_gap_is_valid(candidate):
        raise MaterializationError("proposal lacks deterministic config-gap evidence")
    catalog_provenance = catalog.get("provenance")
    target_binding = (
        catalog_provenance.get("private_config_target_map")
        if isinstance(catalog_provenance, Mapping)
        else None
    )
    target_map_hash = (
        str(target_binding.get("hash") or "")
        if isinstance(target_binding, Mapping)
        else ""
    )
    if (
        not target_map_hash
        or str(candidate.get("config_target_map_hash") or "") != target_map_hash
    ):
        raise MaterializationError("proposal is not bound to the catalog config target map")
    target_ref = str(candidate.get("target_ref") or "")
    target = target_index.get(target_ref)
    if not isinstance(target, Mapping):
        raise MaterializationError("bound private config target map is required")
    base_hash = str(candidate.get("base_content_hash") or "")
    if (
        str(target.get("fingerprint") or "") != base_hash
        or str(target.get("target_kind") or "") != str(candidate.get("target_kind") or "")
    ):
        raise MaterializationError("proposal target metadata differs from private target map")
    instruction = _validate_atomic_instruction(candidate.get("instruction_text"))
    target_path = str(target.get("target_path") or "")
    path = Path(target_path)
    _validate_proposal_destination(candidate.get("target_kind"), target_path)
    try:
        old_bytes = path.read_bytes() if path.is_file() else b""
    except OSError as exc:
        raise MaterializationError(f"proposal target is unreadable: {exc}") from exc
    if target_ref != _target_ref(target_path, base_hash):
        raise MaterializationError("proposal target path is not bound to its approved target_ref")
    if not _hash_matches(old_bytes, base_hash):
        raise MaterializationError("proposal target changed after config-gap scan")
    old = old_bytes.decode("utf-8", errors="replace")
    if (
        instruction.casefold() in old.casefold()
        or _instruction_semantically_present(instruction, old)
    ):
        raise MaterializationError("proposal instruction is already present")
    title = _normalized_instruction(candidate.get("title"))
    if not title:
        raise MaterializationError("proposal title is empty")
    section = f"\n## {title}\n\n- {instruction}\n"
    new = (old + ("" if not old or old.endswith("\n") else "\n") + section).lstrip("\n")
    scope_type, scope_id = _scope(candidate, supporting)
    normalized_intent = _NON_WORD.sub("_", instruction.casefold()).strip("_")
    semantic_identity = "|".join(
        (scope_type, scope_id or "", str(path), normalized_intent)
    )
    stable_candidate = dict(candidate)
    stable_candidate.pop("source_packet_ids", None)
    stable_candidate.pop("source_packet_coverage", None)
    provenance_payload = {
        "candidate": stable_candidate,
        "pattern_claim_id": pattern_claim.id,
        "materializer_version": MATERIALIZER_VERSION,
    }
    version_hash = _sha256(provenance_payload)
    proposal_id = f"coach:proposal:{_sha256(semantic_identity + ':' + version_hash)[:24]}"
    provenance = {
        "provider": "coach_pipeline",
        "semantic_identity": semantic_identity,
        "normalized_intent": normalized_intent,
        "version_hash": version_hash,
        "candidate_id": str(candidate["candidate_id"]),
        "packet_id": str((candidate.get("source_packet_ids") or [""])[0]),
        "source_packet_ids": list(candidate.get("source_packet_ids") or []),
        "canonical_key": str(candidate["canonical_key"]),
        "pattern_canonical_key": str(candidate["pattern_canonical_key"]),
        "pattern_claim_id": pattern_claim.id,
        "catalog_id": str(catalog["catalog_id"]),
        "catalog_hash": str(catalog["catalog_hash"]),
        "source_synthesis_manifest_hash": str(catalog.get("source_synthesis_manifest_hash") or ""),
        "review_id": str(decision.get("review_id") or ""),
        "review_reason": str(decision.get("reason") or ""),
        "review_decision": "accept",
        "config_gap": dict(candidate["config_gap"]),
        "target_ref": target_ref,
        "config_target_map_hash": target_map_hash,
        "root_cluster_count": int(candidate["n"]),
        "full_eligible_root_denominator": int(candidate["denominator"]),
        "processed_roots": processed_roots,
        "eligible_roots": eligible_roots,
        "coverage_state": "complete",
        "publication_mode": str(replay_provenance.get("publication_mode") or ""),
        "publication_complete": replay_provenance.get("publication_complete"),
        "source_truncated_messages": replay_provenance.get(
            "source_truncated_messages"
        ),
        "population_hash": str(candidate.get("population_hash") or ""),
        "support_distribution": dict(candidate.get("distribution") or {}),
        "selection_method": str(replay_provenance.get("selection_method") or ""),
        "selection_caveat": str(replay_provenance.get("selection_caveat") or ""),
        "proof_capability_by_harness": proof_capability,
        "luna_producers": list(replay_provenance.get("luna_producers") or []),
        "luna_result_ids": [
            str(item.get("result_id") or "")
            for item in replay_provenance.get("luna_results", [])
            if isinstance(item, Mapping)
        ],
        "terra_synthesis_producer": dict(
            replay_provenance.get("terra_synthesis_producer") or {}
        ),
        "terra_synthesis_result_ids": [
            str(item.get("result_id") or "")
            for item in replay_provenance.get("terra_synthesis_results", [])
            if isinstance(item, Mapping)
        ],
        "terra_review_producer": dict(
            replay_provenance.get("terra_review_producer") or {}
        ),
        "terra_review_id": str(replay_provenance.get("terra_review_id") or ""),
        "coverage": dict(catalog.get("coverage") or {}),
        "materializer_version": MATERIALIZER_VERSION,
        "validator_version": MATERIALIZER_VERSION,
        "run_replay": dict(replay_provenance),
    }
    return Proposal(
        id=proposal_id,
        title=title,
        action=action,  # type: ignore[arg-type]
        status="pending",
        target_path=str(path),
        target_kind=str(candidate["target_kind"]),
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_id=scope_id,
        base_content_hash=base_hash,
        unified_diff=unified_diff(path=str(path), old=old, new=new),
        proposed_content=new,
        rationale=(
            f"{candidate['summary']} Config gap: "
            f"{candidate['config_gap'].get('summary') or 'no matching instruction found.'}"
        ),
        derivation_summary=(
            f"Second-review-approved coach proposal from "
            f"{candidate['n']}/{candidate['denominator']} eligible root clusters."
        ),
        does_not_prove=str(candidate["does_not_prove"]),
        sample_size=int(candidate["n"]),
        claim_ids=[pattern_claim.id],
        claims=[pattern_claim],
        created_at=now,
        updated_at=now,
        provenance=provenance,
        model=str(
            (replay_provenance.get("terra_synthesis_producer") or {}).get("model")
            or ""
        )
        or None,
        run_id=str(catalog["catalog_id"]),
        prompt_hash=str(catalog.get("source_synthesis_manifest_hash") or ""),
        evidence_pack_hash=str(catalog["catalog_hash"]),
    )


def _existing_claim_families(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT id, status, updated_at, confidence_basis_json FROM claims "
        "WHERE extractor_name = 'coach_materializer'"
    ).fetchall()
    out: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        identity = str(_json_object(row["confidence_basis_json"]).get("semantic_identity") or "")
        if identity:
            out.setdefault(identity, []).append(row)
    return out


def _existing_proposal_families(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(proposals)")}
    if "provenance_json" not in columns:
        return {}
    rows = conn.execute(
        "SELECT id, status, updated_at, provenance_json FROM proposals"
    ).fetchall()
    out: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        provenance = _json_object(row["provenance_json"])
        if provenance.get("provider") != "coach_pipeline":
            continue
        identity = str(provenance.get("semantic_identity") or "")
        if identity:
            out.setdefault(identity, []).append(row)
    return out


def _latest_active(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    active = [row for row in rows if str(row["status"]) in _ACTIVE_CLAIM_STATUSES]
    return max(active, key=lambda row: (str(row["updated_at"] or ""), str(row["id"]))) if active else None


def _plan_materialization_from_objects(
    conn: sqlite3.Connection,
    catalog: Mapping[str, Any] | Path | str,
    second_review: Mapping[str, Any] | Path | str,
    *,
    now: str | None = None,
    config_target_map: Mapping[str, Any] | Path | str | None = None,
    verified_run: VerifiedCoachRun | None = None,
) -> MaterializationPlan:
    candidate_catalog = _load_object(catalog, "candidate catalog")
    review_raw = _load_object(second_review, "second review")
    _catalog_integrity(candidate_catalog)
    corpus_snapshot_hash = _verify_corpus_snapshot(conn, candidate_catalog)
    _verify_catalog_evidence(conn, candidate_catalog)
    private_target_map = (
        _load_object(config_target_map, "private config target map")
        if config_target_map is not None
        else None
    )
    target_index = _bound_target_index(candidate_catalog, private_target_map)
    validated_review, failures = validate_second_review_result(review_raw, candidate_catalog)
    if failures or validated_review is None:
        reasons = ", ".join(failure.reason for failure in failures)
        raise MaterializationError(f"second review failed validation: {reasons}")
    observed_at = now or _now()
    plan = MaterializationPlan(
        catalog_id=str(candidate_catalog["catalog_id"]),
        catalog_hash=str(candidate_catalog["catalog_hash"]),
        review_id=str(validated_review["review_id"]),
        corpus_snapshot_hash=corpus_snapshot_hash,
        run_dir=str(verified_run.run_dir) if verified_run else "",
        run_bundle_hash=verified_run.bundle_hash if verified_run else "",
    )
    decisions = {
        str(item["candidate_id"]): {**dict(item), "review_id": plan.review_id}
        for item in validated_review["decisions"]
    }
    candidates = {
        str(item.get("candidate_id") or ""): _normalize_candidate_scope(dict(item))
        for item in candidate_catalog.get("candidates", [])
        if isinstance(item, Mapping)
    }
    observation_index = candidate_catalog.get("observation_index")
    if not isinstance(observation_index, Mapping):
        raise MaterializationError("candidate catalog has no immutable observation index")
    catalog_coverage = candidate_catalog.get("coverage")
    if not isinstance(catalog_coverage, Mapping) or not catalog_coverage:
        raise MaterializationError("candidate catalog has no packet coverage")

    accepted_patterns: dict[str, Claim] = {}
    accepted_products: list[tuple[dict[str, Any], dict[str, Any], Claim]] = []
    for candidate_id in sorted(candidates):
        candidate = candidates[candidate_id]
        decision = decisions[candidate_id]
        if decision["decision"] != "accept" or candidate.get("kind") not in _PRODUCT_KINDS:
            continue
        try:
            supporting, counter = _candidate_observations(
                candidate, observation_index, catalog_coverage
            )
            claim = _claim_from_candidate(
                candidate,
                decision,
                candidate_catalog,
                supporting,
                counter,
                observed_at,
                verified_run.replay_provenance if verified_run else {},
            )
        except (KeyError, TypeError, ValueError, MaterializationError) as exc:
            plan.skipped.append(MaterializationSkip(candidate_id, str(exc)))
            continue
        accepted_products.append((candidate, decision, claim))

    existing_claims = _existing_claim_families(conn)
    for candidate, _decision, claim in accepted_products:
        identity = _semantic_identity(candidate)
        family = existing_claims.get(identity, [])
        version_hash = str(claim.confidence_basis.get("version_hash") or "")
        exact_versions = [
            row
            for row in family
            if str(
                _json_object(row["confidence_basis_json"]).get("version_hash") or ""
            )
            == version_hash
        ]
        if len(exact_versions) > 1:
            raise MaterializationError(
                "multiple stored coach claims have the reviewed version identity"
            )
        exact = exact_versions[0] if exact_versions else None
        if exact is not None:
            if not _bind_existing_claim_lineage(conn, claim, exact):
                raise MaterializationError(
                    "existing coach claim payload differs from reviewed materialization"
                )
            exact_status = str(exact["status"])
            if exact_status == "candidate":
                plan.claims.append(claim)
            else:
                plan.unchanged_claim_ids.append(claim.id)
            if (
                candidate["kind"] == "corpus_pattern"
                and exact_status in {"candidate", "approved", "published"}
            ):
                accepted_patterns[str(candidate["canonical_key"])] = claim
            continue
        active = _latest_active(family)
        predecessor_id = str(active["id"]) if active is not None else None
        _bind_claim_lineage(claim, predecessor_id)
        if active is not None:
            plan.supersede_claim_ids.extend(
                str(row["id"])
                for row in family
                if str(row["status"]) in _ACTIVE_CLAIM_STATUSES
            )
        plan.claims.append(claim)
        if candidate["kind"] == "corpus_pattern":
            accepted_patterns[str(candidate["canonical_key"])] = claim

    existing_proposals = _existing_proposal_families(conn)
    for candidate_id in sorted(candidates):
        candidate = candidates[candidate_id]
        decision = decisions[candidate_id]
        if decision["decision"] != "accept" or candidate.get("kind") != "coach_proposal":
            continue
        pattern = accepted_patterns.get(str(candidate.get("pattern_canonical_key") or ""))
        if pattern is None:
            plan.skipped.append(
                MaterializationSkip(candidate_id, "proposal pattern was not approved in this review")
            )
            continue
        try:
            supporting, _counter = _candidate_observations(
                candidate, observation_index, catalog_coverage
            )
            proposal = _proposal_from_candidate(
                candidate,
                decision,
                candidate_catalog,
                pattern,
                observed_at,
                supporting,
                target_index,
                verified_run.replay_provenance if verified_run else {},
            )
        except (KeyError, TypeError, ValueError, MaterializationError) as exc:
            plan.skipped.append(MaterializationSkip(candidate_id, str(exc)))
            continue
        identity = str(proposal.provenance["semantic_identity"])
        family = existing_proposals.get(identity, [])
        exact = next((row for row in family if str(row["id"]) == proposal.id), None)
        if exact is not None:
            if not _stored_proposal_matches(conn, proposal):
                raise MaterializationError(
                    "existing coach proposal payload differs from reviewed materialization"
                )
            plan.unchanged_proposal_ids.append(proposal.id)
            continue
        plan.supersede_proposal_ids.extend(
            str(row["id"]) for row in family if str(row["status"]) == "pending"
        )
        plan.proposals.append(proposal)
        plan.target_preconditions.append(
            {
                "target_path": proposal.target_path,
                "base_content_hash": str(proposal.base_content_hash or ""),
            }
        )
    return plan


def plan_materialization(
    conn: sqlite3.Connection,
    run_dir: Path | str,
    *,
    now: str | None = None,
) -> MaterializationPlan:
    verified = verify_coach_run(conn, run_dir)
    return _plan_materialization_from_objects(
        conn,
        verified.catalog,
        verified.second_review,
        now=now,
        config_target_map=verified.config_target_map,
        verified_run=verified,
    )


def apply_materialization_plan(
    conn: sqlite3.Connection,
    plan: MaterializationPlan,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    report = {**plan.to_dict(), "dry_run": dry_run, "claims_written": 0, "proposals_written": 0}
    if dry_run:
        return report
    if not plan.corpus_snapshot_hash:
        raise MaterializationError("materialization plan has no corpus snapshot precondition")
    if not plan.run_dir or not plan.run_bundle_hash:
        raise MaterializationError("materialization plan was not produced from a verified run bundle")
    if conn.in_transaction:
        raise MaterializationError("materialization apply requires a clean database transaction")
    verified = verify_coach_run(conn, plan.run_dir)
    if verified.bundle_hash != plan.run_bundle_hash:
        raise MaterializationError("coach run bundle changed after materialization planning")
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fresh_plan = _plan_materialization_from_objects(
            conn,
            verified.catalog,
            verified.second_review,
            config_target_map=verified.config_target_map,
            verified_run=verified,
        )
        if _plan_integrity_payload(fresh_plan) != _plan_integrity_payload(plan):
            raise MaterializationError("materialization plan is stale")
        current_snapshot = build_corpus_snapshot(conn)
        if str(current_snapshot.get("snapshot_hash") or "") != plan.corpus_snapshot_hash:
            raise MaterializationError("corpus changed after materialization planning")
        for precondition in fresh_plan.target_preconditions:
            path = Path(str(precondition.get("target_path") or ""))
            expected = str(precondition.get("base_content_hash") or "")
            try:
                data = path.read_bytes() if path.is_file() else b""
            except OSError as exc:
                raise MaterializationError(
                    f"proposal target changed after materialization planning: {exc}"
                ) from exc
            if not path.is_absolute() or not expected or not _hash_matches(data, expected):
                raise MaterializationError("proposal target changed after materialization planning")
        for claim_id in sorted(set(fresh_plan.supersede_claim_ids)):
            conn.execute(
                "UPDATE claims SET status = 'superseded', updated_at = ? "
                "WHERE id = ? AND status IN ('candidate','approved','published')",
                (now, claim_id),
            )
        for proposal_id in sorted(set(fresh_plan.supersede_proposal_ids)):
            changed = conn.execute(
                "UPDATE proposals SET status = 'superseded', updated_at = ?, "
                "decided_at = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
                (
                    now,
                    now,
                    "system-superseded: newer reviewed coach evidence",
                    proposal_id,
                ),
            ).rowcount
            if changed:
                conn.execute(
                    "INSERT INTO proposal_events "
                    "(id, proposal_id, event_type, detail_json, created_at) "
                    "VALUES (?, ?, 'superseded', ?, ?)",
                    (
                        f"{proposal_id}:superseded:{uuid.uuid4().hex[:12]}",
                        proposal_id,
                        json.dumps(
                            {"actor": "system", "note": "newer reviewed coach evidence"},
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
        report = {
            **fresh_plan.to_dict(),
            "dry_run": False,
            "claims_written": upsert_claims(conn, fresh_plan.claims),
            "proposals_written": upsert_proposals(conn, fresh_plan.proposals),
        }
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report


def materialize_reviewed_run(
    conn: sqlite3.Connection,
    run_dir: Path | str,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    plan = plan_materialization(conn, run_dir)
    return apply_materialization_plan(conn, plan, dry_run=dry_run)


def plan_legacy_quarantine(conn: sqlite3.Connection) -> LegacyQuarantinePlan:
    reasons: dict[str, str] = {}
    claim_ids: set[str] = set()
    proposal_ids: set[str] = set()
    proposal_backed_claim_ids: set[str] = set()
    for row in conn.execute(
        "SELECT id, status, extractor_name, confidence_basis_json FROM claims"
    ).fetchall():
        if str(row["status"]) not in _ACTIVE_CLAIM_STATUSES:
            continue
        provenance = _json_object(row["confidence_basis_json"])
        if (
            str(row["extractor_name"]) == "session_fact_packet"
            and str(provenance.get("run_id") or "") == "insights-session-demo"
        ):
            claim_id = str(row["id"])
            claim_ids.add(claim_id)
            reasons[claim_id] = "legacy insights-session-demo gallery"

    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(proposals)")}
    if "provenance_json" in columns:
        rows = conn.execute(
            "SELECT id, status, model, provenance_json FROM proposals WHERE status = 'pending'"
        ).fetchall()
        for row in rows:
            provenance = _json_object(row["provenance_json"])
            models = {
                str(value or "").lower()
                for value in (row["model"], provenance.get("model"))
                if str(value or "").strip()
            }
            validator = str(provenance.get("validator_version") or "")
            redaction = provenance.get("redaction")
            redaction_version = (
                str(redaction.get("redaction_version") or "")
                if isinstance(redaction, Mapping)
                else ""
            )
            if not any("grok" in model for model in models):
                continue
            if (
                validator == PROPOSAL_PACKET_VALIDATOR_VERSION
                and redaction_version == REDACTION_VERSION
            ):
                continue
            proposal_id = str(row["id"])
            proposal_ids.add(proposal_id)
            reasons[proposal_id] = "pre-current-validator Grok proposal"
            linked = conn.execute(
                "SELECT pc.claim_id FROM proposal_claims pc "
                "JOIN claims c ON c.id = pc.claim_id "
                "WHERE pc.proposal_id = ? "
                "AND c.status IN ('candidate','approved','published')",
                (proposal_id,),
            ).fetchall()
            for claim_row in linked:
                claim_id = str(claim_row["claim_id"])
                proposal_backed_claim_ids.add(claim_id)
    for claim_id in claim_ids | proposal_backed_claim_ids:
        active_links = {
            str(row["proposal_id"])
            for row in conn.execute(
                "SELECT pc.proposal_id FROM proposal_claims pc "
                "JOIN proposals p ON p.id = pc.proposal_id "
                "WHERE pc.claim_id = ? "
                "AND p.status IN ('pending','accepted','deferred')",
                (claim_id,),
            ).fetchall()
        }
        if active_links - proposal_ids:
            claim_ids.discard(claim_id)
            reasons.pop(claim_id, None)
        elif claim_id in proposal_backed_claim_ids and active_links:
            claim_ids.add(claim_id)
            reasons[claim_id] = "claim backing pre-current-validator Grok proposal"
    return LegacyQuarantinePlan(
        claim_ids=tuple(sorted(claim_ids)),
        proposal_ids=tuple(sorted(proposal_ids)),
        reasons=reasons,
    )


def quarantine_legacy_records(
    conn: sqlite3.Connection,
    *,
    plan: LegacyQuarantinePlan | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    current = plan_legacy_quarantine(conn)
    quarantine = plan or current
    if quarantine.to_dict() != current.to_dict():
        raise MaterializationError("legacy quarantine plan is stale or not provenance-bound")
    report = {**quarantine.to_dict(), "dry_run": dry_run, "claims_quarantined": 0, "proposals_quarantined": 0}
    if dry_run:
        return report
    if conn.in_transaction:
        raise MaterializationError("legacy quarantine apply requires a clean database transaction")
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked_current = plan_legacy_quarantine(conn)
        if quarantine.to_dict() != locked_current.to_dict():
            raise MaterializationError("legacy quarantine plan is stale or not provenance-bound")
        for claim_id in quarantine.claim_ids:
            report["claims_quarantined"] += int(
                conn.execute(
                    "UPDATE claims SET status = 'superseded', updated_at = ? "
                    "WHERE id = ? AND status IN ('candidate','approved','published')",
                    (now, claim_id),
                ).rowcount
                or 0
            )
        for proposal_id in quarantine.proposal_ids:
            changed = int(
                conn.execute(
                    "UPDATE proposals SET status = 'superseded', updated_at = ?, "
                    "decided_at = ?, decision_note = ? WHERE id = ? AND status = 'pending'",
                    (
                        now,
                        now,
                        "system-superseded: quarantined legacy proposal provenance",
                        proposal_id,
                    ),
                ).rowcount
                or 0
            )
            report["proposals_quarantined"] += changed
            if changed:
                conn.execute(
                    "INSERT INTO proposal_events "
                    "(id, proposal_id, event_type, detail_json, created_at) "
                    "VALUES (?, ?, 'superseded', ?, ?)",
                    (
                        f"{proposal_id}:superseded:{uuid.uuid4().hex[:12]}",
                        proposal_id,
                        json.dumps(
                            {
                                "actor": "system",
                                "note": quarantine.reasons.get(proposal_id),
                            },
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return report
