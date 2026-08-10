from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentlog.analysis.extractors.models import (
    EvidenceSpan,
    ExtractorMeta,
    ProcessFlags,
    UxObservation,
    WindowContext,
)
from agentlog.analysis.extractors.prompt import PROMPT_PATH, load_ux_prompt, ux_prompt_hash
from agentlog.analysis.extractors.storage import (
    PURPOSE_FULL_CORPUS,
    finish_ux_run,
    start_ux_run,
    write_ux_observations,
)
from agentlog.analysis.extractors.taxonomy import (
    EXTRACTOR_NAME_UX,
    EXTRACTOR_VERSION,
    PROCESS_FLAGS,
    AgentStance,
    PriorOutcome,
    Route,
    TurnKind,
    UserStance,
)
from agentlog.analysis.extractors.window_context import load_window_contexts, truncate_for_ux
from agentlog.safety.redaction import REDACTION_VERSION
from agentlog.safety.write_guard import assert_writable

# Packet sizing: after §6 truncation, median ~1k chars and p90 ~4.5k; p99 outliers
# hit field caps (~10k). Default to a few windows per packet with a hard char budget
# so one oversized window does not blow a subagent context.
DEFAULT_WINDOWS_PER_PACKET = 4
DEFAULT_MAX_CHARS_PER_PACKET = 28_000
SINGLETON_CHAR_THRESHOLD = 12_000

PACKET_STATUS_PENDING = "pending"
PACKET_STATUS_COMPLETED = "completed"
PACKET_STATUS_REJECTED = "rejected"

DETERMINISTIC_TURN_KINDS = frozenset(
    {
        TurnKind.HARNESS_SYNTHETIC.value,
        TurnKind.AUTO_REVIEW.value,
        TurnKind.EMPTY_OR_UNPARSEABLE.value,
        TurnKind.TOOL_PLUMBING.value,
    }
)
ALLOWED_TURN_KINDS = frozenset(k.value for k in TurnKind) - DETERMINISTIC_TURN_KINDS
ALLOWED_USER_STANCE = frozenset(k.value for k in UserStance)
ALLOWED_AGENT_STANCE = frozenset(k.value for k in AgentStance)
ALLOWED_PRIOR_OUTCOME = frozenset(k.value for k in PriorOutcome)
REQUIRED_WINDOW_FIELDS = (
    "window_id",
    "turn_kind",
    "user_stance",
    "agent_stance",
    "prior_outcome",
    "flags",
    "spans",
    "confidence",
    "abstain_reasons",
    "novel_observations",
)


class PacketExtractionProvider:
    """File-based subagent handoff. Does not call a model in-process."""

    name = "packet"

    def complete_json(self, *, system: str, user: str, model: str) -> dict[str, Any]:
        raise RuntimeError(
            "PacketExtractionProvider does not invoke models in-process; "
            "use emit_packet_run / ingest_packet_results"
        )


@dataclass
class ValidationFailure:
    reason: str
    window_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "window_id": self.window_id}


@dataclass
class PacketIngestResult:
    packet_id: str
    status: str
    accepted: list[UxObservation] = field(default_factory=list)
    failures: list[ValidationFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "status": self.status,
            "accepted": len(self.accepted),
            "failures": [f.to_dict() for f in self.failures],
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_char_estimate(payload: dict[str, Any]) -> int:
    return (
        len(str(payload.get("user") or ""))
        + len(str(payload.get("assistant") or ""))
        + len(str(payload.get("next_user") or ""))
        + sum(len(str(x)) for x in (payload.get("tool_timeline") or []))
    )


def pack_windows(
    payloads: list[dict[str, Any]],
    *,
    windows_per_packet: int = DEFAULT_WINDOWS_PER_PACKET,
    max_chars_per_packet: int = DEFAULT_MAX_CHARS_PER_PACKET,
) -> list[list[dict[str, Any]]]:
    """Greedy pack by count and char budget; large windows become singletons."""
    if windows_per_packet < 1:
        raise ValueError("windows_per_packet must be >= 1")
    packets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for payload in payloads:
        est = _payload_char_estimate(payload)
        if est >= SINGLETON_CHAR_THRESHOLD or est > max_chars_per_packet:
            if current:
                packets.append(current)
                current, current_chars = [], 0
            packets.append([payload])
            continue
        would_exceed = (
            current
            and (
                len(current) >= windows_per_packet
                or current_chars + est > max_chars_per_packet
            )
        )
        if would_exceed:
            packets.append(current)
            current, current_chars = [], 0
        current.append(payload)
        current_chars += est
    if current:
        packets.append(current)
    return packets


def labeled_window_ids(conn: sqlite3.Connection) -> set[str]:
    """Window ids that already carry a live (non-orphaned) ux_observations row."""
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(ux_observations)")}
    if "link_status" in cols:
        sql = (
            "SELECT DISTINCT window_id FROM ux_observations "
            "WHERE window_id IS NOT NULL AND link_status = 'linked'"
        )
    else:
        sql = (
            "SELECT DISTINCT window_id FROM ux_observations WHERE window_id IS NOT NULL"
        )
    return {str(r[0]) for r in conn.execute(sql)}


def _run_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "manifest": run_dir / "manifest.json",
        "packets": run_dir / "packets",
        "results": run_dir / "results",
        "rejects": run_dir / "rejects",
        "prompt": run_dir / "ux_extraction_subagent.md",
    }


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = _run_paths(run_dir)["manifest"]
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    path = assert_writable(_run_paths(run_dir)["manifest"], purpose="packet manifest")
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def packet_run_status(run_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(run_dir)
    packets = manifest.get("packets") or {}
    counts: dict[str, int] = {}
    for meta in packets.values():
        st = str(meta.get("status") or PACKET_STATUS_PENDING)
        counts[st] = counts.get(st, 0) + 1
    return {
        "run_id": manifest.get("run_id"),
        "provider": manifest.get("provider"),
        "prompt_hash": manifest.get("prompt_hash"),
        "model": manifest.get("model"),
        "packet_count": len(packets),
        "window_count": manifest.get("window_count"),
        "status_counts": counts,
        "db_run_id": manifest.get("db_run_id"),
        "packets": packets,
    }


def emit_packet_run(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    windows_per_packet: int = DEFAULT_WINDOWS_PER_PACKET,
    max_chars_per_packet: int = DEFAULT_MAX_CHARS_PER_PACKET,
    model: str = "grok-4.5",
    ux_only: bool = True,
    window_ids: list[str] | None = None,
    skip_labeled: bool = False,
    resume: bool = True,
) -> dict[str, Any]:
    """
    Emit triaged/truncated work packets for subagent labeling.

    If run_dir already has a manifest and resume=True, return it unchanged
    (idempotent; does not rewrite completed work).
    """
    paths = _run_paths(run_dir)
    if resume and paths["manifest"].exists():
        return load_manifest(run_dir)

    contexts = load_window_contexts(conn, window_ids=window_ids)
    if skip_labeled:
        already = labeled_window_ids(conn)
        contexts = [c for c in contexts if c.window_id not in already]
    if ux_only:
        from agentlog.analysis.extractors.triage import triage_window

        contexts = [c for c in contexts if triage_window(c).route == Route.UX]
    payloads = [truncate_for_ux(c) for c in contexts]
    groups = pack_windows(
        payloads,
        windows_per_packet=windows_per_packet,
        max_chars_per_packet=max_chars_per_packet,
    )

    assert_writable(run_dir, purpose="packet run dir")
    run_dir.mkdir(parents=True, exist_ok=True)
    for key in ("packets", "results", "rejects"):
        paths[key].mkdir(parents=True, exist_ok=True)

    prompt_text = load_ux_prompt()
    phash = ux_prompt_hash(prompt_text)
    paths["prompt"].write_text(prompt_text, encoding="utf-8")

    run_id = f"packet_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    db_run_id = start_ux_run(
        conn,
        model=model,
        batch_size=1,
        window_count=len(payloads),
        gated=True,
        purpose=PURPOSE_FULL_CORPUS,
    )
    row = conn.execute(
        "SELECT meta_json FROM derivation_runs WHERE id = ?", (db_run_id,)
    ).fetchone()
    meta = json.loads(row["meta_json"] or "{}") if row else {}
    meta.update(
        {
            "provider": PacketExtractionProvider.name,
            "packet_run_id": run_id,
            "prompt_path": str(PROMPT_PATH),
        }
    )
    conn.execute(
        "UPDATE derivation_runs SET meta_json = ? WHERE id = ?",
        (json.dumps(meta), db_run_id),
    )
    conn.commit()

    packet_meta: dict[str, Any] = {}
    for i, group in enumerate(groups, start=1):
        packet_id = f"pkt_{i:04d}"
        packet_body = {
            "packet_id": packet_id,
            "run_id": run_id,
            "prompt_hash": phash,
            "prompt_file": "ux_extraction_subagent.md",
            "model_hint": model,
            "windows": group,
        }
        packet_path = paths["packets"] / f"{packet_id}.json"
        packet_path.write_text(
            json.dumps(packet_body, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        packet_meta[packet_id] = {
            "status": PACKET_STATUS_PENDING,
            "window_ids": [p["window_id"] for p in group],
            "char_estimate": sum(_payload_char_estimate(p) for p in group),
            "packet_path": str(packet_path.relative_to(run_dir)),
            "result_path": None,
            "ingested_at": None,
            "reject_reasons": [],
        }

    manifest = {
        "run_id": run_id,
        "db_run_id": db_run_id,
        "provider": PacketExtractionProvider.name,
        "created_at": _utc_now(),
        "model": model,
        "prompt_hash": phash,
        "redaction_version": REDACTION_VERSION,
        "prompt_repo_path": str(PROMPT_PATH),
        "windows_per_packet": windows_per_packet,
        "max_chars_per_packet": max_chars_per_packet,
        "window_count": len(payloads),
        "packet_count": len(groups),
        "packets": packet_meta,
    }
    save_manifest(run_dir, manifest)
    return manifest


def _source_for_role(payload: dict[str, Any], role: str) -> str:
    return str(
        {
            "user": payload.get("user") or "",
            "assistant": payload.get("assistant") or "",
            "next_user": payload.get("next_user") or "",
        }.get(role, "")
    )


def validate_window_result(
    raw: Any,
    *,
    payload: dict[str, Any],
    allowed_ids: set[str],
) -> tuple[dict[str, Any] | None, list[ValidationFailure]]:
    """Hard validation — reject rather than coerce."""
    failures: list[ValidationFailure] = []
    if not isinstance(raw, dict):
        return None, [ValidationFailure(reason="result_not_object")]

    wid = raw.get("window_id")
    if wid is None or str(wid).strip() == "":
        failures.append(ValidationFailure(reason="missing_required_field:window_id"))
        return None, failures
    wid = str(wid)
    if wid not in allowed_ids:
        failures.append(
            ValidationFailure(reason="unknown_window_id", window_id=wid)
        )
        return None, failures

    for field_name in REQUIRED_WINDOW_FIELDS:
        if field_name not in raw:
            failures.append(
                ValidationFailure(
                    reason=f"missing_required_field:{field_name}", window_id=wid
                )
            )
    if failures:
        return None, failures

    turn_kind = raw.get("turn_kind")
    if not isinstance(turn_kind, list):
        failures.append(
            ValidationFailure(reason="turn_kind_not_list", window_id=wid)
        )
    else:
        for k in turn_kind:
            ks = str(k)
            if ks in DETERMINISTIC_TURN_KINDS:
                failures.append(
                    ValidationFailure(
                        reason=f"deterministic_turn_kind_forbidden:{ks}",
                        window_id=wid,
                    )
                )
            elif ks not in ALLOWED_TURN_KINDS:
                failures.append(
                    ValidationFailure(
                        reason=f"unknown_turn_kind:{ks}", window_id=wid
                    )
                )

    for field_name, allowed in (
        ("user_stance", ALLOWED_USER_STANCE),
        ("agent_stance", ALLOWED_AGENT_STANCE),
        ("prior_outcome", ALLOWED_PRIOR_OUTCOME),
    ):
        val = raw.get(field_name)
        if val is None:
            continue
        if str(val) not in allowed:
            failures.append(
                ValidationFailure(
                    reason=f"unknown_{field_name}:{val}", window_id=wid
                )
            )

    flags = raw.get("flags")
    if not isinstance(flags, dict):
        failures.append(ValidationFailure(reason="flags_not_object", window_id=wid))
    else:
        for key in flags:
            if key not in PROCESS_FLAGS:
                failures.append(
                    ValidationFailure(
                        reason=f"unknown_flag:{key}", window_id=wid
                    )
                )

    spans = raw.get("spans")
    if not isinstance(spans, list):
        failures.append(ValidationFailure(reason="spans_not_list", window_id=wid))
    else:
        for i, span in enumerate(spans):
            if not isinstance(span, dict):
                failures.append(
                    ValidationFailure(reason=f"span_not_object:{i}", window_id=wid)
                )
                continue
            quote = span.get("quote")
            role = span.get("role")
            if quote is None or role is None:
                failures.append(
                    ValidationFailure(
                        reason=f"span_missing_quote_or_role:{i}", window_id=wid
                    )
                )
                continue
            quote_s = str(quote)
            role_s = str(role)
            if role_s not in ("user", "assistant", "next_user"):
                failures.append(
                    ValidationFailure(
                        reason=f"unknown_span_role:{role_s}", window_id=wid
                    )
                )
                continue
            source = _source_for_role(payload, role_s)
            if quote_s not in source:
                failures.append(
                    ValidationFailure(
                        reason="evidence_quote_not_in_source",
                        window_id=wid,
                    )
                )

    confidence = raw.get("confidence")
    if not isinstance(confidence, dict):
        failures.append(
            ValidationFailure(reason="confidence_not_object", window_id=wid)
        )
    else:
        for k, v in confidence.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                failures.append(
                    ValidationFailure(
                        reason=f"confidence_not_numeric:{k}", window_id=wid
                    )
                )

    for list_field in ("abstain_reasons", "novel_observations"):
        if not isinstance(raw.get(list_field), list):
            failures.append(
                ValidationFailure(
                    reason=f"{list_field}_not_list", window_id=wid
                )
            )

    if failures:
        return None, failures
    return raw, []


def _parse_result_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "windows" in data and isinstance(data["windows"], list):
        return [r for r in data["windows"] if isinstance(r, dict)]
    if "window_id" in data:
        return [data]
    return []


def raw_to_observation(
    raw: dict[str, Any],
    *,
    model: str,
    prompt_hash: str,
    packet_id: str,
    redaction_version: str | None = None,
) -> UxObservation:
    flags_raw = raw.get("flags") or {}
    flags = ProcessFlags(
        premature_action_called_out=bool(flags_raw.get("premature_action_called_out")),
        scope_expansion=bool(flags_raw.get("scope_expansion")),
        scope_narrowing=bool(flags_raw.get("scope_narrowing")),
        multi_agent_reference=bool(flags_raw.get("multi_agent_reference")),
        instruction_violation_alleged=bool(
            flags_raw.get("instruction_violation_alleged")
        ),
        verification_requested=bool(flags_raw.get("verification_requested")),
        usage_or_api_limit=bool(flags_raw.get("usage_or_api_limit")),
    )
    spans = [
        EvidenceSpan(
            role=str(s.get("role")),
            quote=str(s.get("quote")),
            supports=[str(x) for x in (s.get("supports") or [])],
        )
        for s in (raw.get("spans") or [])
        if isinstance(s, dict)
    ]
    conf = {
        str(k): float(v)
        for k, v in (raw.get("confidence") or {}).items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    us = raw.get("user_stance")
    ag = raw.get("agent_stance")
    po = raw.get("prior_outcome")
    return UxObservation(
        window_id=str(raw["window_id"]),
        extractor=ExtractorMeta(
            name=EXTRACTOR_NAME_UX,
            version=EXTRACTOR_VERSION,
            model=model,
            prompt_hash=prompt_hash,
            packet_id=packet_id,
            provider=PacketExtractionProvider.name,
            redaction_version=redaction_version or REDACTION_VERSION,
        ),
        turn_kind=[str(x) for x in (raw.get("turn_kind") or [])],
        user_stance=str(us) if us is not None else None,
        agent_stance=str(ag) if ag is not None else None,
        prior_outcome=str(po) if po is not None else None,
        flags=flags,
        spans=spans,
        confidence=conf,
        abstain_reasons=[str(x) for x in (raw.get("abstain_reasons") or [])],
        novel_observations=[str(x) for x in (raw.get("novel_observations") or [])],
        batch_size=1,
    )


def ingest_packet_result(
    run_dir: Path,
    packet_id: str,
    result_path: Path,
    *,
    conn: sqlite3.Connection | None = None,
    model: str | None = None,
    write_db: bool = True,
) -> PacketIngestResult:
    """Validate one result file and optionally write accepted rows to ux_observations."""
    manifest = load_manifest(run_dir)
    packets = manifest.get("packets") or {}
    if packet_id not in packets:
        return PacketIngestResult(
            packet_id=packet_id,
            status=PACKET_STATUS_REJECTED,
            failures=[ValidationFailure(reason="packet_id_not_in_manifest")],
        )
    meta = packets[packet_id]
    if meta.get("status") == PACKET_STATUS_COMPLETED:
        return PacketIngestResult(packet_id=packet_id, status=PACKET_STATUS_COMPLETED)

    packet_path = run_dir / meta["packet_path"]
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    payloads = {str(w["window_id"]): w for w in packet.get("windows") or []}
    allowed_ids = set(payloads)
    expected_ids = set(meta.get("window_ids") or allowed_ids)

    data = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        failures = [ValidationFailure(reason="result_root_not_object")]
        return _finalize_reject(
            run_dir, manifest, packet_id, result_path, failures
        )

    rows = _parse_result_rows(data)
    if not rows:
        failures = [ValidationFailure(reason="missing_windows_or_window_id")]
        return _finalize_reject(
            run_dir, manifest, packet_id, result_path, failures
        )

    failures: list[ValidationFailure] = []
    accepted_raw: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in rows:
        wid = str(row.get("window_id") or "")
        if wid and wid in seen:
            failures.append(
                ValidationFailure(reason="duplicate_window_id", window_id=wid)
            )
            continue
        if wid:
            seen.add(wid)
        ok, row_failures = validate_window_result(
            row, payload=payloads.get(wid, {}), allowed_ids=allowed_ids
        )
        failures.extend(row_failures)
        if ok is not None:
            accepted_raw.append(ok)

    missing = expected_ids - seen
    for wid in sorted(missing):
        failures.append(
            ValidationFailure(reason="missing_window_result", window_id=wid)
        )

    if failures:
        return _finalize_reject(
            run_dir, manifest, packet_id, result_path, failures
        )

    use_model = model or str(manifest.get("model") or "grok-4.5")
    phash = str(manifest.get("prompt_hash") or ux_prompt_hash())
    rver = str(manifest.get("redaction_version") or REDACTION_VERSION)
    observations = [
        raw_to_observation(
            r,
            model=use_model,
            prompt_hash=phash,
            packet_id=packet_id,
            redaction_version=rver,
        )
        for r in accepted_raw
    ]

    if write_db:
        if conn is None:
            raise ValueError("conn required when write_db=True")
        db_run_id = str(manifest["db_run_id"])
        write_ux_observations(conn, db_run_id, observations)

    paths = _run_paths(run_dir)
    dest = paths["results"] / f"{packet_id}.json"
    if result_path.resolve() != dest.resolve():
        dest.write_text(result_path.read_text(encoding="utf-8"), encoding="utf-8")

    meta["status"] = PACKET_STATUS_COMPLETED
    meta["result_path"] = str(dest.relative_to(run_dir))
    meta["ingested_at"] = _utc_now()
    meta["reject_reasons"] = []
    packets[packet_id] = meta
    manifest["packets"] = packets
    save_manifest(run_dir, manifest)

    if write_db and conn is not None:
        _maybe_finish_run(conn, manifest)

    return PacketIngestResult(
        packet_id=packet_id,
        status=PACKET_STATUS_COMPLETED,
        accepted=observations,
    )


def _finalize_reject(
    run_dir: Path,
    manifest: dict[str, Any],
    packet_id: str,
    result_path: Path,
    failures: list[ValidationFailure],
) -> PacketIngestResult:
    paths = _run_paths(run_dir)
    reject_path = paths["rejects"] / f"{packet_id}.json"
    reject_path.write_text(
        json.dumps(
            {
                "packet_id": packet_id,
                "result_path": str(result_path),
                "failures": [f.to_dict() for f in failures],
                "rejected_at": _utc_now(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    meta = manifest["packets"][packet_id]
    meta["status"] = PACKET_STATUS_REJECTED
    meta["result_path"] = str(result_path)
    meta["reject_reasons"] = [f.to_dict() for f in failures]
    manifest["packets"][packet_id] = meta
    save_manifest(run_dir, manifest)
    return PacketIngestResult(
        packet_id=packet_id,
        status=PACKET_STATUS_REJECTED,
        failures=failures,
    )


def _maybe_finish_run(conn: sqlite3.Connection, manifest: dict[str, Any]) -> None:
    packets = manifest.get("packets") or {}
    statuses = [str(p.get("status")) for p in packets.values()]
    if not statuses:
        return
    status_counts = {s: statuses.count(s) for s in set(statuses)}
    base_meta = {
        "provider": PacketExtractionProvider.name,
        "packet_run_id": manifest.get("run_id"),
        "window_count": manifest.get("window_count"),
        "packet_count": manifest.get("packet_count"),
        "status_counts": status_counts,
    }
    if all(s == PACKET_STATUS_COMPLETED for s in statuses):
        finish_ux_run(
            conn,
            str(manifest["db_run_id"]),
            status="completed",
            meta=base_meta,
        )
    elif all(s in (PACKET_STATUS_COMPLETED, PACKET_STATUS_REJECTED) for s in statuses):
        finish_ux_run(
            conn,
            str(manifest["db_run_id"]),
            status="completed_with_rejects",
            meta=base_meta,
        )


def ingest_packet_results(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    results_dir: Path | None = None,
    model: str | None = None,
) -> list[PacketIngestResult]:
    """
    Ingest all new result files for pending/rejected packets.

    Looks for `<results_dir>/<packet_id>.json` (default: run_dir/results_inbox
    then run_dir/results). Skips packets already completed.
    """
    manifest = load_manifest(run_dir)
    search_dirs: list[Path] = []
    if results_dir is not None:
        search_dirs.append(results_dir)
    search_dirs.append(run_dir / "results_inbox")
    search_dirs.append(run_dir / "results")

    out: list[PacketIngestResult] = []
    for packet_id, meta in sorted((manifest.get("packets") or {}).items()):
        if meta.get("status") == PACKET_STATUS_COMPLETED:
            out.append(
                PacketIngestResult(packet_id=packet_id, status=PACKET_STATUS_COMPLETED)
            )
            continue
        result_file: Path | None = None
        for d in search_dirs:
            candidate = d / f"{packet_id}.json"
            if candidate.exists():
                result_file = candidate
                break
        if result_file is None:
            continue
        # Reload manifest each time so status updates stick across loop.
        out.append(
            ingest_packet_result(
                run_dir,
                packet_id,
                result_file,
                conn=conn,
                model=model,
                write_db=True,
            )
        )
    return out


def contexts_to_packet_payloads(contexts: list[WindowContext]) -> list[dict[str, Any]]:
    return [truncate_for_ux(c) for c in contexts]
