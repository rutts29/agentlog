from __future__ import annotations

import json
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
        originator: str | None = None
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
        session_id_locked = False
        bytes_consumed = 0

        for _start, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
            bytes_consumed = end
            if err:
                warnings.append(err)
                continue
            assert obj is not None
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
                if parent_raw and str(parent_raw) != external_id:
                    parent_id = str(parent_raw)
                elif (
                    root_id
                    and str(root_id) != external_id
                    and parent_id is None
                ):
                    parent_id = str(root_id)
                agent_role = payload.get("agent_role")
                raw_originator = payload.get("originator")
                if isinstance(raw_originator, str) and raw_originator.strip():
                    originator = raw_originator.strip()
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

        session = NormalizedSession(
            harness=Harness.CODEX,
            external_id=external_id,
            parent_session_id=parent_id,
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
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            skill_exposures=skills,
            token_usages=token_usages,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
            extras={"originator": originator} if originator else {},
        )
