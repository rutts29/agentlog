from __future__ import annotations

from pathlib import Path
from typing import Any

from agentlog.config import CODEX_SESSIONS_DIR
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    extract_text,
    iter_jsonl_bytes,
    parse_ts,
)
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
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
        event_messages: list[NormalizedMessage] = []
        response_messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        external_id = _external_id_from_path(path)
        parent_id: str | None = None
        cwd: str | None = None
        repo = branch = commit = None
        model: str | None = None
        effort: str | None = None
        started_at = None
        ended_at = None
        event_seq = 0
        response_seq = 0
        tool_seq = 0
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
                cwd = payload.get("cwd") or cwd
                r, b, c = _git_fields(payload.get("git"))
                repo = r or repo
                branch = b or branch
                commit = c or commit
                if payload.get("model_provider") and not model:
                    model = str(payload.get("model_provider"))
                continue

            if kind == "turn_context":
                if payload.get("model"):
                    model = str(payload["model"])
                if payload.get("effort") is not None:
                    effort = str(payload["effort"])
                cwd = payload.get("cwd") or cwd
                r, b, c = _git_fields(payload.get("git"))
                repo = r or repo
                branch = b or branch
                commit = c or commit
                continue

            payload_type = payload.get("type")

            if kind == "event_msg" and payload_type in (
                "user_message",
                "agent_message",
            ):
                role = "user" if payload_type == "user_message" else "assistant"
                text = extract_text(payload.get("message") or payload.get("text"))
                event_seq += 1
                event_messages.append(
                    NormalizedMessage(
                        seq=event_seq,
                        role=role,
                        timestamp=ts,
                        model=model if role == "assistant" else None,
                        effort=effort if role == "assistant" else None,
                        text=text,
                        content_hash=content_hash_text(text),
                    )
                )
                continue

            if kind == "response_item" and payload_type == "message":
                role = str(payload.get("role") or "assistant")
                if role in ("developer", "system"):
                    continue
                text = extract_text(payload.get("content"))
                if not text.strip():
                    continue
                if (
                    response_messages
                    and response_messages[-1].role == role
                    and response_messages[-1].text == text
                ):
                    continue
                response_seq += 1
                response_messages.append(
                    NormalizedMessage(
                        seq=response_seq,
                        role=role,
                        timestamp=ts,
                        model=model if role == "assistant" else None,
                        effort=effort if role == "assistant" else None,
                        text=text,
                        content_hash=content_hash_text(text),
                    )
                )
                continue

            if payload_type in (
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
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=None,
                        tool_name=name,
                        action="call",
                        success=None,
                        duration_ms=None,
                    )
                )
                continue

            if payload_type in (
                "function_call_output",
                "custom_tool_call_output",
                "tool_search_output",
            ):
                name = str(payload.get("name") or payload.get("tool_name") or "tool")
                success = payload.get("success")
                if success is None and "exit_code" in payload:
                    success = payload.get("exit_code") == 0
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=None,
                        tool_name=name,
                        action="result",
                        success=bool(success) if success is not None else None,
                        duration_ms=_as_ms(payload.get("duration_ms") or payload.get("duration")),
                    )
                )
                continue

            if payload_type in (
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
                        message_seq=None,
                        tool_name=str(payload.get("command") or payload.get("name") or name),
                        action="end",
                        success=(exit_code == 0) if exit_code is not None else None,
                        duration_ms=_as_ms(payload.get("duration_ms") or payload.get("duration")),
                    )
                )
                continue

        messages = event_messages or response_messages

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
            effort=effort,
        )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
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
