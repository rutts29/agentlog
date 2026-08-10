from __future__ import annotations

import re
from pathlib import Path

from agentlog.config import CLAUDE_PROJECTS_DIR
from agentlog.ingest.base import (
    TranscriptAdapter,
    content_hash_text,
    content_is_tool_plumbing,
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
    SkillExposure,
    TokenUsage,
    ToolEvent,
)
from agentlog.normalize.synthetic import (
    flag_synthetic_user_messages,
    synthetic_skill_exposures,
)
from agentlog.normalize.tool_ops import classify_operation

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)


def _tool_detail(block: dict) -> str | None:
    raw_input = block.get("input")
    if not isinstance(raw_input, dict):
        return None
    for key in ("command", "cmd"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_from_claude_message(
    usage: object,
    *,
    seq: int,
    message_seq: int,
    model: str | None,
    timestamp,
) -> TokenUsage | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(usage.get("output_tokens"))
    cache_creation = _int_or_none(usage.get("cache_creation_input_tokens"))
    cache_read = _int_or_none(usage.get("cache_read_input_tokens"))
    if all(
        v is None
        for v in (input_tokens, output_tokens, cache_creation, cache_read)
    ):
        return None
    extras: dict = {}
    for key in (
        "service_tier",
        "inference_geo",
        "speed",
        "server_tool_use",
        "cache_creation",
        "iterations",
    ):
        if key in usage and usage[key] not in (None, "", [], {}):
            extras[key] = usage[key]
    return TokenUsage(
        seq=seq,
        message_seq=message_seq,
        granularity="message",
        usage_source="claude_message_usage",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        timestamp=timestamp,
        extras=extras,
    )


def _project_slug(path: Path) -> str | None:
    try:
        rel = path.relative_to(CLAUDE_PROJECTS_DIR)
    except ValueError:
        return None
    parts = rel.parts
    return parts[0] if parts else None


def _parent_session_id(path: Path) -> str | None:
    # .../<session-uuid>/subagents/agent-....jsonl
    # .../<session-uuid>/subagents/workflows/.../agent-....jsonl
    parts = path.parts
    for i, part in enumerate(parts):
        if part == "subagents" and i > 0 and UUID_RE.match(parts[i - 1]):
            return parts[i - 1]
    return None


def _external_id(path: Path) -> str:
    stem = path.stem
    if stem == "skill-injections":
        slug = _project_slug(path) or "unknown"
        return f"skills:{slug}"
    return stem


def external_id_from_path(path: Path) -> str:
    """Public path → external_id helper (used by live presence)."""
    return _external_id(path)


def _is_structural_user_context(record: dict[str, object], *, role: str) -> bool:
    if role != "user":
        return False
    return bool(
        record.get("isMeta")
        or record.get("sourceToolUseID")
        or record.get("isCompactSummary")
    )


class ClaudeAdapter(TranscriptAdapter):
    harness = Harness.CLAUDE

    def discover(self) -> list[Path]:
        root = CLAUDE_PROJECTS_DIR
        if not root.is_dir():
            return []
        out: list[Path] = []
        for p in root.rglob("*.jsonl"):
            if not p.is_file():
                continue
            name = p.name
            if name == "journal.jsonl":
                continue
            out.append(p)
        return sorted(out)

    def parse_chunk(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        if path.name == "skill-injections.jsonl":
            return self._parse_skills(path, data, start_offset=start_offset)
        return self._parse_session(path, data, start_offset=start_offset)

    def _parse_skills(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        warnings: list[str] = []
        exposures: list[SkillExposure] = []
        started_at = None
        ended_at = None
        bytes_consumed = 0
        for _s, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
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
            for name in obj.get("matchedSkills") or []:
                exposures.append(
                    SkillExposure(
                        skill_name=str(name),
                        exposure_type="matched",
                    )
                )
            for name in obj.get("injectedSkills") or []:
                exposures.append(
                    SkillExposure(
                        skill_name=str(name),
                        exposure_type="injected",
                    )
                )
        slug = _project_slug(path)
        session = NormalizedSession(
            harness=Harness.CLAUDE,
            external_id=_external_id(path),
            started_at=started_at,
            ended_at=ended_at,
            cwd=str(CLAUDE_PROJECTS_DIR / slug) if slug else None,
            repo=slug,
        )
        return ParseResult(
            session=session,
            skill_exposures=exposures,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
        )

    def _parse_session(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> ParseResult:
        warnings: list[str] = []
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        tool_operations: dict[str, str] = {}
        tool_names: dict[str, str] = {}
        skills: list[SkillExposure] = []
        token_usages: list[TokenUsage] = []
        external_id = _external_id(path)
        parent = _parent_session_id(path)
        is_subagent = path.name.startswith("agent-") or parent is not None
        cwd: str | None = None
        branch: str | None = None
        model: str | None = None
        agent_profile: str | None = None
        effort: str | None = None
        effort_source: str | None = None
        started_at = None
        ended_at = None
        msg_seq = 0
        tool_seq = 0
        bytes_consumed = 0

        for _s, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
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
            cwd = obj.get("cwd") or cwd
            branch = obj.get("gitBranch") or branch
            # Claude JSONL exposes gitBranch only — no commit/sha field in corpus.
            if obj.get("effort") is not None:
                effort, effort_source = normalize_effort(str(obj["effort"]))
            if is_subagent:
                if obj.get("agentId"):
                    external_id = f"agent-{obj['agentId']}"
                if obj.get("sessionId"):
                    parent = str(obj["sessionId"])
            elif obj.get("sessionId"):
                external_id = str(obj["sessionId"])
            agent_type = obj.get("agentType") or obj.get("agent_type")
            if agent_type not in (None, "") and agent_profile is None:
                agent_profile = str(agent_type)

            etype = obj.get("type")
            if etype in ("user", "assistant"):
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                role = str(msg.get("role") or etype)
                msg_model = msg.get("model")
                if msg_model:
                    raw_model = str(msg_model)
                    if raw_model != "<synthetic>":
                        model = raw_model
                    elif model is None:
                        model = raw_model
                content = msg.get("content")
                text = extract_text(content)
                structural_context = _is_structural_user_context(
                    obj, role=role
                ) or _is_structural_user_context(msg, role=role)
                plumbing = content_is_tool_plumbing(content) or structural_context
                msg_seq += 1
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role=role,
                        timestamp=ts,
                        model=str(msg_model) if msg_model else None,
                        effort=effort if role == "assistant" else None,
                        effort_source=(
                            effort_source if role == "assistant" else None
                        ),
                        text=text,
                        content_hash=content_hash_text(text),
                        is_tool_plumbing=plumbing,
                        authored_by_agent=structural_context,
                    )
                )
                usage_row = _usage_from_claude_message(
                    msg.get("usage"),
                    seq=len(token_usages) + 1,
                    message_seq=msg_seq,
                    model=str(msg_model) if msg_model else None,
                    timestamp=ts,
                )
                if usage_row is not None:
                    token_usages.append(usage_row)
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            tool_seq += 1
                            name = str(block.get("name") or "tool")
                            tool_id = str(block.get("id") or block.get("tool_use_id") or "")
                            operation_kind = str(
                                classify_operation(name, _tool_detail(block))
                            )
                            if tool_id:
                                tool_operations[tool_id] = operation_kind
                                tool_names[tool_id] = name
                            tools.append(
                                ToolEvent(
                                    seq=tool_seq,
                                    message_seq=msg_seq,
                                    tool_name=name,
                                    action="call",
                                    operation_kind=operation_kind,
                                )
                            )
                            if name == "Skill":
                                inp = block.get("input") or {}
                                skill_name = None
                                if isinstance(inp, dict):
                                    skill_name = inp.get("skill") or inp.get("name")
                                if skill_name:
                                    skills.append(
                                        SkillExposure(
                                            message_seq=msg_seq,
                                            skill_name=str(skill_name),
                                            exposure_type="tool_use",
                                        )
                                    )
                        elif btype == "tool_result":
                            tool_seq += 1
                            tool_id = str(block.get("tool_use_id") or block.get("id") or "")
                            result_name = block.get("name")
                            name = str(
                                result_name
                                or tool_names.get(tool_id)
                                or tool_id
                                or "tool"
                            )
                            tools.append(
                                ToolEvent(
                                    seq=tool_seq,
                                    message_seq=msg_seq,
                                    tool_name=name,
                                    action="result",
                                    success=(
                                        not bool(block.get("is_error"))
                                        if "is_error" in block
                                        else None
                                    ),
                                    operation_kind=tool_operations.get(
                                        tool_id, str(classify_operation(name))
                                    ),
                                )
                            )
                continue

            if etype == "system":
                content = obj.get("content")
                text = extract_text(content) if content else str(obj.get("subtype") or "")
                if not text:
                    continue
                msg_seq += 1
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role="system",
                        timestamp=ts,
                        text=text,
                        content_hash=content_hash_text(text),
                    )
                )
                continue

            # tool_result sometimes as user content blocks already handled;
            # attachment / progress events skipped unless they carry text
            if etype == "attachment":
                continue

        if is_subagent and start_offset == 0:
            flag_parent_authored_prompt(messages)
        flag_synthetic_user_messages(messages)
        skills.extend(synthetic_skill_exposures(messages))

        session = NormalizedSession(
            harness=Harness.CLAUDE,
            external_id=external_id,
            parent_session_id=parent,
            started_at=started_at,
            ended_at=ended_at,
            cwd=cwd,
            branch=branch,
            model=model,
            agent_profile=agent_profile,
            effort=effort,
            effort_source=effort_source,
            repo=_project_slug(path),
        )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            skill_exposures=skills,
            token_usages=token_usages,
            warnings=warnings,
            bytes_consumed=start_offset + bytes_consumed,
        )
