from __future__ import annotations

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
        effort: str | None = None
        effort_source: str | None = None
        started_at = None
        ended_at = None
        tool_seq = 0
        token_seq = 0
        last_response_assistant_seq: int | None = None
        last_event_assistant_seq: int | None = None
        call_names: dict[str, str] = {}
        # Avoid double-counting when both event_msg and response_item carry the same call
        seen_call_ids: set[str] = set()
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
                call_id = str(
                    payload.get("call_id")
                    or payload.get("id")
                    or f"{payload_type}:{tool_seq}"
                )
                if call_id in seen_call_ids:
                    continue
                seen_call_ids.add(call_id)
                name = str(
                    payload.get("name")
                    or payload.get("tool_name")
                    or payload_type
                )
                call_names[call_id] = name
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
                    )
                )
                continue

            if kind == "response_item" and payload_type in (
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            ):
                call_id = str(payload.get("call_id") or payload.get("id") or "")
                name = str(
                    payload.get("name")
                    or payload.get("tool_name")
                    or call_names.get(call_id)
                    or "tool"
                )
                success = payload.get("success")
                if success is None and "exit_code" in payload:
                    success = payload.get("exit_code") == 0
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
                        success=bool(success) if success is not None else None,
                        duration_ms=_as_ms(
                            payload.get("duration_ms") or payload.get("duration")
                        ),
                    )
                )
                continue

            if kind == "response_item" and payload_type in (
                "exec_command_end",
                "patch_apply_end",
                "web_search_end",
                "mcp_tool_call_end",
            ):
                name = payload_type.replace("_end", "")
                tool_seq += 1
                exit_code = payload.get("exit_code")
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=(
                            last_response_assistant_seq
                            if response_messages
                            else last_event_assistant_seq
                        ),
                        tool_name=str(
                            payload.get("command")
                            or payload.get("name")
                            or name
                        ),
                        action="end",
                        success=(exit_code == 0) if exit_code is not None else None,
                        duration_ms=_as_ms(
                            payload.get("duration_ms") or payload.get("duration")
                        ),
                    )
                )
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
            token_usages=token_usages,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
        )
