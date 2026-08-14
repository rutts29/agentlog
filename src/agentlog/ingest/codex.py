from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlog.config import CODEX_SESSIONS_DIR
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    extract_text,
    flag_parent_authored_prompt,
    iter_jsonl_bytes,
    parse_ts,
)
from agentlog.ingest.grok_launch import completed_grok_launch
from agentlog.normalize.effort import normalize_effort
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    TokenUsage,
    ToolEvent,
)
from agentlog.normalize.synthetic import (
    flag_synthetic_user_messages,
    is_codex_internal_context_goal,
    normalize_synthetic_user_text,
    synthetic_skill_exposures,
)
from agentlog.normalize.tool_ops import classify_operation
from agentlog.session_identity import INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE


_INTERNAL_APPROVAL_GUARDIAN_PREFIXES = (
    "the following is the codex agent history whose request action you are assessing",
    "the following is the codex agent history added since your last approval assessment",
)


def _is_internal_approval_guardian_prompt(text: str) -> bool:
    return text.lstrip().casefold().startswith(
        _INTERNAL_APPROVAL_GUARDIAN_PREFIXES
    )


def _external_id_from_path(path: Path) -> str:
    name = path.stem
    # rollout-2026-08-01T20-53-36-019fbdec-7065-7470-bb1e-dfa6c0d38237
    if "rollout-" in name:
        parts = name.split("-")
        # UUID is last 5 hyphenated groups at end (8-4-4-4-12) but with extra prefixes
        # Prefer payload session id when available; path fallback:
        for i, part in enumerate(parts):
            if len(part) == 8 and i + 4 < len(parts):
                candidate = "-".join(parts[i : i + 5])
                if len(candidate) >= 36:
                    return candidate
    return name


def external_id_from_path(path: Path) -> str:
    """Public path → external_id helper (used by live presence)."""
    return _external_id_from_path(path)


def _git_fields(git: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(git, dict):
        return None, None, None
    branch = git.get("branch") or git.get("git_branch")
    commit = git.get("commit_hash") or git.get("commit") or git.get("sha")
    repo = git.get("repository_url") or git.get("repo") or git.get("remote")
    return (
        str(repo) if repo else None,
        str(branch) if branch else None,
        str(commit) if commit else None,
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _codex_usage_block(
    block: object,
    *,
    seq: int,
    granularity: str,
    usage_source: str,
    model: str | None,
    timestamp,
    extras: dict[str, Any] | None = None,
) -> TokenUsage | None:
    if not isinstance(block, dict):
        return None
    fields = {
        "input_tokens": _int_or_none(block.get("input_tokens")),
        "output_tokens": _int_or_none(block.get("output_tokens")),
        "cached_input_tokens": _int_or_none(block.get("cached_input_tokens")),
        "cache_write_input_tokens": _int_or_none(
            block.get("cache_write_input_tokens")
        ),
        "reasoning_output_tokens": _int_or_none(
            block.get("reasoning_output_tokens")
        ),
        "total_tokens": _int_or_none(block.get("total_tokens")),
    }
    if all(v is None for v in fields.values()):
        return None
    return TokenUsage(
        seq=seq,
        message_seq=None,
        granularity=granularity,
        usage_source=usage_source,
        model=model,
        timestamp=timestamp,
        extras=extras or {},
        **fields,
    )


def _as_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n < 0:
        return None
    # Heuristic: seconds if small float
    if isinstance(value, float) and n < 1e6:
        return int(n * 1000)
    return int(n)


def _tool_call_id(payload: dict[str, Any]) -> str | None:
    for key in ("call_id", "callId", "tool_call_id", "toolCallId", "id"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _explicit_success(payload: dict[str, Any]) -> bool | None:
    explicit = _success_field(payload)
    exit_success = _exit_code_success(payload)
    if explicit is not None and exit_success is not None and explicit != exit_success:
        return None
    return explicit if explicit is not None else exit_success


def _success_field(payload: dict[str, Any]) -> bool | None:
    if "success" not in payload or payload.get("success") is None:
        return None
    value = payload.get("success")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "ok", "success", "1"}:
            return True
        if normalized in {"false", "no", "error", "failure", "0"}:
            return False
    return None


def _exit_code_success(payload: dict[str, Any]) -> bool | None:
    if "exit_code" not in payload or payload.get("exit_code") is None:
        return None
    value = payload.get("exit_code")
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return None


def _outcome_conflicts(payload: dict[str, Any]) -> bool:
    explicit = _success_field(payload)
    exit_success = _exit_code_success(payload)
    return (
        explicit is not None
        and exit_success is not None
        and explicit != exit_success
    )


def _merge_success(
    existing: bool | None,
    incoming: bool | None,
    *,
    call_id: str | None,
    conflicted_call_ids: set[str],
) -> bool | None:
    if call_id and call_id in conflicted_call_ids:
        return None
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    if existing != incoming:
        if call_id:
            conflicted_call_ids.add(call_id)
        return None
    return existing


def _mcp_result_success(payload: dict[str, Any]) -> bool | None:
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    if "Ok" in result and "Err" in result:
        return None
    if "Ok" in result:
        return True
    if "Err" in result:
        return False
    return None


def _success_conflicts(payload: dict[str, Any]) -> bool:
    if _outcome_conflicts(payload):
        return True
    result = payload.get("result")
    mcp_conflict = isinstance(result, dict) and "Ok" in result and "Err" in result
    if mcp_conflict:
        return True
    explicit = _explicit_success(payload)
    mcp_success = _mcp_result_success(payload)
    return explicit is not None and mcp_success is not None and explicit != mcp_success


def _resolved_success(payload: dict[str, Any]) -> bool | None:
    if _success_conflicts(payload):
        return None
    explicit = _explicit_success(payload)
    mcp_success = _mcp_result_success(payload)
    return explicit if explicit is not None else mcp_success


def _output_texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _output_texts(item)]
    if isinstance(value, dict):
        texts: list[str] = []
        for key in ("text", "output", "content"):
            if key in value:
                texts.extend(_output_texts(value[key]))
        return texts
    return []


def _output_exit_success(payload: dict[str, Any]) -> bool | None:
    codes: set[int] = set()
    for text in _output_texts(payload.get("output")):
        stripped = text.strip()
        if not stripped.startswith("{"):
            continue
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        value = decoded.get("exit_code") if isinstance(decoded, dict) else None
        try:
            codes.add(int(value))
        except (TypeError, ValueError):
            continue
    if len(codes) != 1:
        return None
    return next(iter(codes)) == 0


def _completion_success(payload: dict[str, Any]) -> bool | None:
    explicit = _resolved_success(payload)
    embedded = _output_exit_success(payload)
    if explicit is not None and embedded is not None and explicit != embedded:
        return None
    return explicit if explicit is not None else embedded


def _running_cell_id(payload: dict[str, Any]) -> str | None:
    for text in _output_texts(payload.get("output")):
        match = re.fullmatch(r"Script running with cell ID ([A-Za-z0-9_-]+)\s*.*", text, re.DOTALL)
        if match:
            return match.group(1)
    return None


def _wait_cell_id(payload: dict[str, Any]) -> str | None:
    arguments = payload.get("arguments")
    if not isinstance(arguments, str):
        return None
    try:
        decoded = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    value = decoded.get("cell_id") if isinstance(decoded, dict) else None
    return str(value) if isinstance(value, (str, int)) else None


def _terminal_tool_name(
    payload_type: str, payload: dict[str, Any], call_names: dict[str, str]
) -> str:
    if payload_type == "mcp_tool_call_end":
        invocation = payload.get("invocation")
        if isinstance(invocation, dict):
            tool = invocation.get("tool") or invocation.get("name")
            if isinstance(tool, str) and tool.strip():
                return tool.strip()
    call_id = _tool_call_id(payload)
    if call_id and call_id in call_names:
        return call_names[call_id]
    for key in ("tool_name", "toolName", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return payload_type.removesuffix("_end")


def _read_only_hint(payload: dict[str, Any]) -> bool | None:
    for key in ("read_only_hint", "readOnlyHint"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    invocation = payload.get("invocation")
    if isinstance(invocation, dict):
        for key in ("read_only_hint", "readOnlyHint"):
            value = invocation.get(key)
            if isinstance(value, bool):
                return value
    return None


def _operation_detail(payload: dict[str, Any]) -> str | None:
    """Extract only a transient command-like argument for classification."""
    for key in ("command", "cmd"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        for key in ("command", "cmd"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return None
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except (TypeError, ValueError):
            return None
        if isinstance(decoded, dict):
            for key in ("command", "cmd"):
                value = decoded.get(key)
                if isinstance(value, str) and value.strip():
                    return value
    return None


def _operation_for_event(
    payload: dict[str, Any],
    *,
    call_id: str | None,
    name: str,
    call_operations: dict[str, str],
) -> str:
    hint = _read_only_hint(payload)
    if hint is True:
        return "read_only"
    if call_id and call_operations.get(call_id) not in (None, "unknown"):
        return call_operations[call_id]
    return str(
        classify_operation(name, _operation_detail(payload), read_only_hint=hint)
    )


@dataclass(frozen=True)
class _ForkBoundary:
    record_index: int | None
    turn_id: str | None
    status: str | None
    inherited_record_count: int = 0
    inherited_message_count: int = 0
    local_prefix: tuple[dict[str, Any], ...] = ()


def _response_item_id(obj: dict[str, Any]) -> str | None:
    if obj.get("type") != "response_item":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    value = payload.get("id")
    return str(value) if value else None


def _worker_agent_path(records: list[dict[str, Any]]) -> str | None:
    if not records:
        return None
    payload = records[0].get("payload")
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    path = spawn.get("agent_path") if isinstance(spawn, dict) else None
    return path.strip() if isinstance(path, str) and path.strip() else None


def _is_agent_coordination_envelope(text: str) -> bool:
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    return first_line in {"Message Type: NEW_TASK", "Message Type: MESSAGE"}


def _incoming_agent_message_indices(records: list[dict[str, Any]]) -> set[int]:
    worker_path = _worker_agent_path(records)
    if worker_path is None:
        return set()
    incoming: set[int] = set()
    for index, obj in enumerate(records):
        payload = obj.get("payload")
        if (
            obj.get("type") != "inter_agent_communication_metadata"
            or not isinstance(payload, dict)
            or payload.get("trigger_turn") is not True
        ):
            continue
        for later_index, later in enumerate(
            records[index + 1 : index + 5], start=index + 1
        ):
            later_payload = later.get("payload")
            if (
                later.get("type") == "response_item"
                and isinstance(later_payload, dict)
                and later_payload.get("type") == "agent_message"
                and later_payload.get("recipient") == worker_path
                and _is_agent_coordination_envelope(
                    extract_text(later_payload.get("content"))
                )
            ):
                incoming.add(later_index)
                break
    return incoming


def _parent_response_ids(parent_id: str) -> set[str] | None:
    matches = list(CODEX_SESSIONS_DIR.rglob(f"*{parent_id}.jsonl"))
    if len(matches) != 1:
        return None
    ids: set[str] = set()
    try:
        data = matches[0].read_bytes()
    except OSError:
        return None
    for _start, _end, obj, err in iter_jsonl_bytes(data, source=str(matches[0])):
        if err or obj is None:
            continue
        response_id = _response_item_id(obj)
        if response_id:
            ids.add(response_id)
    return ids


def _fork_boundary(records: list[dict[str, Any]]) -> _ForkBoundary:
    if not records:
        return _ForkBoundary(None, None, None)
    meta = records[0]
    payload = meta.get("payload")
    if meta.get("type") != "session_meta" or not isinstance(payload, dict):
        return _ForkBoundary(None, None, None)
    parent_id = payload.get("forked_from_id")
    if payload.get("thread_source") != "subagent" or not parent_id:
        return _ForkBoundary(None, None, None)

    meta_ts = parse_ts(payload.get("timestamp") or meta.get("timestamp"))
    first_after_meta = records[1] if len(records) > 1 else None
    first_payload = (
        first_after_meta.get("payload")
        if isinstance(first_after_meta, dict)
        else None
    )
    if (
        isinstance(first_payload, dict)
        and first_after_meta.get("type") == "event_msg"
        and first_payload.get("type") == "task_started"
        and first_payload.get("turn_id")
    ):
        raw_started_at = first_payload.get("started_at")
        starts_at_spawn = meta_ts is None or raw_started_at is None
        if meta_ts is not None and raw_started_at is not None:
            try:
                starts_at_spawn = abs(
                    float(raw_started_at) - meta_ts.timestamp()
                ) <= 1
            except (TypeError, ValueError):
                starts_at_spawn = False
        turn_id = str(first_payload["turn_id"])
        has_context = any(
            obj.get("type") == "turn_context"
            and isinstance(obj.get("payload"), dict)
            and str(obj["payload"].get("turn_id") or "") == turn_id
            for obj in records[2:]
        )
        if starts_at_spawn and has_context:
            return _ForkBoundary(None, None, None)

    candidates: list[tuple[int, str]] = []
    for index, obj in enumerate(records[1:], start=1):
        event = obj.get("payload")
        if (
            obj.get("type") != "event_msg"
            or not isinstance(event, dict)
            or event.get("type") != "task_started"
            or not event.get("turn_id")
        ):
            continue
        turn_id = str(event["turn_id"])
        next_task = next(
            (
                offset
                for offset, later in enumerate(records[index + 1 :], start=index + 1)
                if later.get("type") == "event_msg"
                and isinstance(later.get("payload"), dict)
                and later["payload"].get("type") == "task_started"
            ),
            len(records),
        )
        turn_records = records[index + 1 : next_task]
        has_context = any(
            later.get("type") == "turn_context"
            and isinstance(later.get("payload"), dict)
            and str(later["payload"].get("turn_id") or "") == turn_id
            for later in turn_records
        )
        has_trigger = any(
            later.get("type") == "inter_agent_communication_metadata"
            and isinstance(later.get("payload"), dict)
            and later["payload"].get("trigger_turn") is True
            for later in turn_records
        )
        raw_started_at = event.get("started_at")
        starts_after_spawn = True
        if meta_ts is not None and raw_started_at is not None:
            try:
                starts_after_spawn = float(raw_started_at) >= meta_ts.timestamp() - 1
            except (TypeError, ValueError):
                starts_after_spawn = False
        if has_context and has_trigger and starts_after_spawn:
            candidates.append((index, turn_id))

    if not candidates:
        return _ForkBoundary(None, None, "ambiguous")
    boundary_index, turn_id = candidates[0]
    inherited = records[1:boundary_index]
    response_ids = {
        response_id
        for obj in inherited
        if (response_id := _response_item_id(obj)) is not None
    }
    parent_ids = _parent_response_ids(str(parent_id))
    status = "structural_only"
    local_prefix: tuple[dict[str, Any], ...] = ()
    if parent_ids is not None:
        non_parent_ids = response_ids - parent_ids
        if non_parent_ids:
            local_prefix = tuple(
                obj
                for obj in inherited
                if _response_item_id(obj) in non_parent_ids
                and isinstance(obj.get("payload"), dict)
                and obj["payload"].get("type") == "message"
                and obj["payload"].get("role") in ("developer", "system")
            )
            if {
                response_id
                for obj in local_prefix
                if (response_id := _response_item_id(obj)) is not None
            } != non_parent_ids:
                return _ForkBoundary(None, turn_id, "ambiguous")
        status = "verified_parent"
    local_prefix_ids = {
        response_id
        for obj in local_prefix
        if (response_id := _response_item_id(obj)) is not None
    }
    inherited_messages = sum(
        1
        for obj in inherited
        if obj.get("type") == "response_item"
        and isinstance(obj.get("payload"), dict)
        and obj["payload"].get("type") == "message"
        and _response_item_id(obj) not in local_prefix_ids
    )
    return _ForkBoundary(
        boundary_index,
        turn_id,
        status,
        inherited_record_count=len(inherited) - len(local_prefix),
        inherited_message_count=inherited_messages,
        local_prefix=local_prefix,
    )


def _append_message(
    bucket: list[NormalizedMessage],
    *,
    role: str,
    text: str,
    ts,
    model: str | None,
    effort: str | None,
    effort_source: str | None,
    authored_by_agent: bool = False,
) -> int | None:
    """Append message; return its seq. Skip empty or consecutive duplicates."""
    is_tool_plumbing = False
    if role == "user":
        normalized = normalize_synthetic_user_text(text)
        text = normalized.text
        authored_by_agent = authored_by_agent or normalized.flags.authored_by_agent
        is_tool_plumbing = normalized.flags.is_tool_plumbing
    if not text.strip() and role != "system":
        return None
    if (
        bucket
        and bucket[-1].role == role
        and bucket[-1].text == text
    ):
        return bucket[-1].seq
    seq = len(bucket) + 1
    bucket.append(
        NormalizedMessage(
            seq=seq,
            role=role,
            timestamp=ts,
            model=model if role == "assistant" else None,
            effort=effort if role == "assistant" else None,
            effort_source=effort_source if role == "assistant" else None,
            text=text,
            content_hash=content_hash_text(text),
            is_tool_plumbing=is_tool_plumbing,
            authored_by_agent=authored_by_agent,
        )
    )
    return seq


class CodexAdapter(TranscriptAdapter):
    harness = Harness.CODEX

    def discover(self) -> list[Path]:
        root = CODEX_SESSIONS_DIR
        if not root.is_dir():
            return []
        return sorted(p for p in root.rglob("*.jsonl") if p.is_file())

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        warnings: list[str] = []
        parsed_records: list[dict[str, Any]] = []
        bytes_consumed = 0
        for _start, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
            bytes_consumed = end
            if err:
                warnings.append(err)
            elif obj is not None:
                parsed_records.append(obj)
        fork_boundary = (
            _fork_boundary(parsed_records)
            if start_offset == 0
            else _ForkBoundary(None, None, None)
        )
        if fork_boundary.status == "ambiguous":
            warnings.append(
                f"{path}: ambiguous full-history fork boundary; activity omitted"
            )
            parsed_records = parsed_records[:1]
        elif fork_boundary.record_index is not None:
            parsed_records = (
                parsed_records[:1]
                + list(fork_boundary.local_prefix)
                + parsed_records[fork_boundary.record_index :]
            )
        incoming_agent_messages = _incoming_agent_message_indices(parsed_records)
        # response_item stream is canonical when present (tools interleave there).
        response_messages: list[NormalizedMessage] = []
        event_messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        token_usages: list[TokenUsage] = []
        external_id = _external_id_from_path(path)
        parent_id: str | None = None
        # True when session_meta shows a spawned worker/guardian (not a history fork).
        agent_brief_session = False
        cwd: str | None = None
        repo = branch = commit = None
        model: str | None = None
        provider: str | None = None
        agent_profile: str | None = None
        guardian_other_subagent = False
        originator: str | None = None
        thread_source: str | None = None
        effort: str | None = None
        effort_source: str | None = None
        started_at = None
        ended_at = None
        tool_seq = 0
        token_seq = 0
        last_response_assistant_seq: int | None = None
        last_event_assistant_seq: int | None = None
        call_names: dict[str, str] = {}
        call_operations: dict[str, str] = {}
        # Avoid double-counting when both event_msg and response_item carry the same call
        seen_call_ids: set[str] = set()
        result_indices: dict[str, int] = {}
        terminal_indices: dict[str, int] = {}
        conflicted_call_ids: set[str] = set()
        grok_launch_calls: dict[str, tuple[object, object]] = {}
        call_successes: dict[str, bool | None] = {}
        launch_cells: dict[str, str] = {}
        wait_cells: dict[str, str] = {}
        session_id_locked = False
        for record_index, obj in enumerate(parsed_records):
            ts = parse_ts(obj.get("timestamp"))
            if started_at is None and ts:
                started_at = ts
            if ts:
                ended_at = ts
            kind = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if kind == "session_meta":
                # Some rollouts repeat session_meta with the root thread id; keep the first.
                if not session_id_locked and payload.get("id"):
                    external_id = str(payload["id"])
                    session_id_locked = True
                parent_raw = (
                    payload.get("parent_thread_id")
                    or payload.get("parent_session_id")
                )
                root_id = payload.get("session_id")
                forked_from_id = payload.get("forked_from_id")
                if parent_raw and str(parent_raw) != external_id:
                    parent_id = str(parent_raw)
                elif (
                    root_id
                    and str(root_id) != external_id
                    and parent_id is None
                ):
                    parent_id = str(root_id)
                elif (
                    forked_from_id
                    and str(forked_from_id) != external_id
                    and parent_id is None
                ):
                    parent_id = str(forked_from_id)
                agent_role = payload.get("agent_role")
                raw_originator = payload.get("originator")
                if isinstance(raw_originator, str) and raw_originator.strip():
                    originator = raw_originator.strip()
                raw_thread_source = payload.get("thread_source")
                if isinstance(raw_thread_source, str) and raw_thread_source.strip():
                    thread_source = raw_thread_source.strip()
                source = payload.get("source")
                sub = (
                    source.get("subagent")
                    if isinstance(source, dict)
                    else None
                )
                if payload.get("thread_source") == "subagent":
                    # thread_spawn with no role often forks parent history (human turns).
                    # Roleed workers/explorers and guardian "other" spawns get agent briefs.
                    if agent_role not in (None, ""):
                        agent_brief_session = True
                    elif isinstance(sub, dict) and "other" in sub:
                        agent_brief_session = True
                    elif isinstance(sub, dict):
                        spawn = sub.get("thread_spawn")
                        agent_brief_session = isinstance(spawn, dict) and bool(
                            spawn.get("agent_nickname") or spawn.get("agent_path")
                        )
                guardian_other_subagent = (
                    payload.get("thread_source") == "subagent"
                    and isinstance(sub, dict)
                    and sub.get("other") == "guardian"
                )
                cwd = payload.get("cwd") or cwd
                r, b, c = _git_fields(payload.get("git"))
                repo = r or repo
                branch = b or branch
                commit = c or commit
                if payload.get("model_provider"):
                    provider = str(payload.get("model_provider"))
                if agent_role not in (None, ""):
                    agent_profile = str(agent_role)
                elif isinstance(sub, dict) and sub.get("other") not in (None, ""):
                    agent_profile = str(sub.get("other"))
                elif isinstance(sub, dict):
                    spawn = sub.get("thread_spawn")
                    if isinstance(spawn, dict):
                        nickname = spawn.get("agent_nickname")
                        path_hint = spawn.get("agent_path")
                        if isinstance(nickname, str) and nickname.strip():
                            agent_profile = nickname.strip()
                        elif isinstance(path_hint, str) and path_hint.strip():
                            agent_profile = path_hint.strip()
                continue

            if kind == "turn_context":
                if payload.get("model"):
                    model = str(payload["model"])
                if payload.get("effort") is not None:
                    effort, effort_source = normalize_effort(str(payload["effort"]))
                cwd = payload.get("cwd") or cwd
                r, b, c = _git_fields(payload.get("git"))
                repo = r or repo
                branch = b or branch
                commit = c or commit
                continue

            payload_type = payload.get("type")

            if kind == "event_msg" and payload_type == "token_count":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                token_seq += 1
                extras: dict[str, Any] = {}
                if info.get("model_context_window") is not None:
                    extras["model_context_window"] = info.get("model_context_window")
                last_row = _codex_usage_block(
                    info.get("last_token_usage"),
                    seq=token_seq,
                    granularity="turn",
                    usage_source="codex_last_token_usage",
                    model=model,
                    timestamp=ts,
                    extras=extras,
                )
                if last_row is not None:
                    token_usages.append(last_row)
                total_row = _codex_usage_block(
                    info.get("total_token_usage"),
                    seq=token_seq,
                    granularity="session_cumulative",
                    usage_source="codex_total_token_usage",
                    model=model,
                    timestamp=ts,
                    extras=extras,
                )
                if total_row is not None:
                    token_usages.append(total_row)
                continue

            if kind == "event_msg" and payload_type in (
                "user_message",
                "agent_message",
            ):
                role = "user" if payload_type == "user_message" else "assistant"
                text = extract_text(payload.get("message") or payload.get("text"))
                seq = _append_message(
                    event_messages,
                    role=role,
                    text=text,
                    ts=ts,
                    model=model,
                    effort=effort,
                    effort_source=effort_source,
                    authored_by_agent=(
                        role == "user" and is_codex_internal_context_goal(text)
                    ),
                )
                if role == "assistant" and seq is not None:
                    last_event_assistant_seq = seq
                continue

            if kind == "response_item" and payload_type == "message":
                raw_role = str(payload.get("role") or "assistant")
                text = extract_text(payload.get("content"))
                if raw_role in ("developer", "system"):
                    # Environment / permissions preamble — keep, never as a human turn.
                    seq = _append_message(
                        response_messages,
                        role="system",
                        text=text,
                        ts=ts,
                        model=None,
                        effort=None,
                        effort_source=None,
                        authored_by_agent=True,
                    )
                    continue
                seq = _append_message(
                    response_messages,
                    role=raw_role,
                    text=text,
                    ts=ts,
                    model=model,
                    effort=effort,
                    effort_source=effort_source,
                    authored_by_agent=(
                        raw_role == "user"
                        and is_codex_internal_context_goal(text)
                    ),
                )
                if raw_role == "assistant" and seq is not None:
                    last_response_assistant_seq = seq
                continue

            if (
                kind == "response_item"
                and payload_type == "agent_message"
                and record_index in incoming_agent_messages
            ):
                text = extract_text(payload.get("content"))
                _append_message(
                    response_messages,
                    role="user",
                    text=text,
                    ts=ts,
                    model=None,
                    effort=None,
                    effort_source=None,
                    authored_by_agent=True,
                )
                continue

            if kind == "response_item" and payload_type in (
                "function_call",
                "custom_tool_call",
                "web_search_call",
                "tool_search_call",
            ):
                call_id = _tool_call_id(payload) or f"{payload_type}:{tool_seq}"
                if call_id in seen_call_ids:
                    continue
                seen_call_ids.add(call_id)
                name = str(
                    payload.get("name")
                    or payload.get("tool_name")
                    or payload_type
                )
                call_names[call_id] = name
                if name.casefold() == "wait":
                    cell_id = _wait_cell_id(payload)
                    if cell_id is not None:
                        wait_cells[call_id] = cell_id
                if name.casefold() in {"exec", "exec_command"}:
                    launch = completed_grok_launch(payload, cwd=cwd)
                    if launch is not None:
                        grok_launch_calls[call_id] = (ts, launch)
                call_operations[call_id] = str(
                    classify_operation(
                        name,
                        _operation_detail(payload),
                        read_only_hint=_read_only_hint(payload),
                    )
                )
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=(
                            last_response_assistant_seq
                            if response_messages
                            else last_event_assistant_seq
                        ),
                        tool_name=name,
                        action="call",
                        success=None,
                        duration_ms=None,
                        operation_kind=call_operations[call_id],
                    )
                )
                continue

            if kind == "response_item" and payload_type in (
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            ):
                call_id = _tool_call_id(payload)
                output_success = _completion_success(payload)
                if call_id:
                    call_successes[call_id] = _merge_success(
                        call_successes.get(call_id),
                        output_success,
                        call_id=call_id,
                        conflicted_call_ids=conflicted_call_ids,
                    )
                    cell_id = _running_cell_id(payload)
                    if cell_id is not None and call_id in grok_launch_calls:
                        launch_cells[cell_id] = call_id
                    waited_cell = wait_cells.get(call_id)
                    launch_call = launch_cells.get(waited_cell or "")
                    if launch_call is not None:
                        call_successes[launch_call] = _merge_success(
                            call_successes.get(launch_call),
                            output_success,
                            call_id=launch_call,
                            conflicted_call_ids=conflicted_call_ids,
                        )
                if call_id and call_id in terminal_indices:
                    terminal = tools[terminal_indices[call_id]]
                    if _success_conflicts(payload):
                        conflicted_call_ids.add(call_id)
                    terminal_success = _resolved_success(payload)
                    terminal.success = _merge_success(
                        terminal.success,
                        terminal_success,
                        call_id=call_id,
                        conflicted_call_ids=conflicted_call_ids,
                    )
                    result_indices[call_id] = terminal_indices[call_id]
                    continue
                name = _terminal_tool_name(payload_type, payload, call_names)
                if call_id and _success_conflicts(payload):
                    conflicted_call_ids.add(call_id)
                success = _resolved_success(payload)
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=(
                            last_response_assistant_seq
                            if response_messages
                            else last_event_assistant_seq
                        ),
                        tool_name=name,
                        action="result",
                        success=success,
                        duration_ms=_as_ms(
                            payload.get("duration_ms") or payload.get("duration")
                        ),
                        operation_kind=classify_operation(
                            name, _operation_detail(payload),
                            read_only_hint=_read_only_hint(payload),
                        )
                        if not call_id
                        else _operation_for_event(
                            payload,
                            call_id=call_id,
                            name=name,
                            call_operations=call_operations,
                        ),
                    )
                )
                if call_id:
                    result_indices[call_id] = len(tools) - 1
                continue

            if kind in ("event_msg", "response_item") and payload_type in (
                "exec_command_end",
                "patch_apply_end",
                "web_search_end",
                "mcp_tool_call_end",
            ):
                call_id = _tool_call_id(payload)
                success = _resolved_success(payload)
                if call_id:
                    call_successes[call_id] = _merge_success(
                        call_successes.get(call_id),
                        success,
                        call_id=call_id,
                        conflicted_call_ids=conflicted_call_ids,
                    )
                existing_index = (
                    result_indices.get(call_id)
                    if call_id
                    else None
                )
                if existing_index is not None:
                    existing = tools[existing_index]
                    existing.tool_name = _terminal_tool_name(
                        payload_type, payload, call_names
                    )
                    if _success_conflicts(payload) and call_id:
                        conflicted_call_ids.add(call_id)
                    existing.success = _merge_success(
                        existing.success,
                        success,
                        call_id=call_id,
                        conflicted_call_ids=conflicted_call_ids,
                    )
                    duration = _as_ms(
                        payload.get("duration_ms") or payload.get("duration")
                    )
                    if duration is not None:
                        existing.duration_ms = duration
                    existing.operation_kind = _operation_for_event(
                        payload,
                        call_id=call_id,
                        name=existing.tool_name,
                        call_operations=call_operations,
                    )
                    terminal_indices[call_id] = existing_index
                    continue
                existing_index = terminal_indices.get(call_id) if call_id else None
                if existing_index is not None:
                    existing = tools[existing_index]
                    if _success_conflicts(payload) and call_id:
                        conflicted_call_ids.add(call_id)
                    existing.success = _merge_success(
                        existing.success,
                        success,
                        call_id=call_id,
                        conflicted_call_ids=conflicted_call_ids,
                    )
                    if existing.operation_kind == "unknown":
                        existing.operation_kind = _operation_for_event(
                            payload,
                            call_id=call_id,
                            name=existing.tool_name,
                            call_operations=call_operations,
                        )
                    continue
                name = _terminal_tool_name(payload_type, payload, call_names)
                if call_id and _success_conflicts(payload):
                    conflicted_call_ids.add(call_id)
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=(
                            last_response_assistant_seq
                            if response_messages
                            else last_event_assistant_seq
                        ),
                        tool_name=name,
                        action="end",
                        success=success,
                        duration_ms=_as_ms(
                            payload.get("duration_ms") or payload.get("duration")
                        ),
                        operation_kind=_operation_for_event(
                            payload,
                            call_id=call_id,
                            name=name,
                            call_operations=call_operations,
                        ),
                    )
                )
                if call_id:
                    terminal_indices[call_id] = len(tools) - 1
                continue

        # Prefer response_item transcript: it carries developer/user preambles and
        # is the structural home for interleaved tool calls.
        messages = response_messages or event_messages
        internal_approval_guardian = guardian_other_subagent and any(
            message.role == "user"
            and _is_internal_approval_guardian_prompt(message.text)
            for message in messages
        )
        # Tools sometimes fire before the first assistant narration (reasoning →
        # function_call → assistant text). Attach those to the first assistant.
        first_assistant_seq = next(
            (m.seq for m in messages if m.role == "assistant"), None
        )
        if first_assistant_seq is not None:
            for te in tools:
                if te.message_seq is None:
                    te.message_seq = first_assistant_seq
        if agent_brief_session and start_offset == 0:
            flag_parent_authored_prompt(messages, leading_users=True)
        flag_synthetic_user_messages(messages)
        skills = synthetic_skill_exposures(messages)
        if internal_approval_guardian:
            messages = []
            tools = []
            skills = []
            token_usages = []
            thread_source = INTERNAL_APPROVAL_GUARDIAN_THREAD_SOURCE

        session = NormalizedSession(
            harness=Harness.CODEX,
            external_id=external_id,
            parent_session_id=parent_id,
            originator=originator,
            thread_source=thread_source,
            started_at=started_at,
            ended_at=ended_at,
            repo=repo,
            cwd=cwd,
            branch=branch,
            commit_sha=commit,
            model=model,
            provider=provider,
            agent_profile=agent_profile,
            effort=effort,
            effort_source=effort_source,
        )
        grok_launches = []
        for call_id, (call_timestamp, launch) in grok_launch_calls.items():
            if call_successes.get(call_id) is not True:
                continue
            if call_timestamp is None:
                continue
            grok_launches.append(
                {
                    "call_id": call_id,
                    "timestamp": call_timestamp.isoformat(),
                    "prompt_hash": launch.prompt_hash,
                    "requested_model": launch.requested_model,
                    "cwd": launch.cwd,
                }
            )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            skill_exposures=skills,
            token_usages=token_usages,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
            extras={
                **({"originator": originator} if originator else {}),
                "inherited_message_count": fork_boundary.inherited_message_count,
                "inherited_record_count": fork_boundary.inherited_record_count,
                "fork_context_status": fork_boundary.status,
                "fork_context_boundary": fork_boundary.turn_id,
                "checkpoint_blocked": fork_boundary.status == "ambiguous",
                **({"grok_launches": grok_launches} if grok_launches else {}),
                **(
                    {"activity_suppressed": "internal_approval_guardian"}
                    if internal_approval_guardian
                    else {}
                ),
                **(
                    {
                        "checkpoint_blocked_reason": (
                            "ambiguous full-history fork boundary"
                        )
                    }
                    if fork_boundary.status == "ambiguous"
                    else {}
                ),
            },
        )
