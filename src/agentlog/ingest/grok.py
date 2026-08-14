from __future__ import annotations

import json
import re
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agentlog.config import GROK_SESSIONS_DIR
from agentlog.ingest.base import (
    SourceSnapshot,
    TranscriptAdapter,
    content_hash_text,
    extract_text,
    file_stat,
    flag_parent_authored_prompt,
    hash_bytes,
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
from agentlog.normalize.synthetic import classify_synthetic_user_text
from agentlog.normalize.tool_ops import classify_operation
from agentlog.session_identity import (
    GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE,
    GROK_BOOTSTRAP_ONLY_THREAD_SOURCE,
)


_USER_QUERY_RE = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>", re.IGNORECASE | re.DOTALL
)
_WORKSPACE_SESSION_RE = re.compile(r"^[^/]+/[^/]+/chat_history\.jsonl$")
_GROK_BOOTSTRAP_PROMPT = re.compile(
    r"^you are grok 4\.[56] released by xai\.", re.IGNORECASE
)
_GROK_SKILL_REMINDER = "the following skills are available for use:"
_GROK_AUTONOMOUS_PROMPT_PREFIX = (
    "You are Grok 4.6 released by xAI. You are an autonomous agent that "
    "completes software engineering tasks. There is no human operator in "
    "this session."
)


def _workspace(path: Path) -> tuple[str | None, str | None]:
    """Decode Grok's percent-encoded workspace directory without probing it."""
    try:
        relative = path.expanduser().resolve().relative_to(
            GROK_SESSIONS_DIR.expanduser().resolve()
        )
    except ValueError:
        return None, None
    if len(relative.parts) != 3 or relative.name != "chat_history.jsonl":
        return None, None
    decoded = unquote(relative.parts[0])
    if not decoded.startswith("/"):
        return None, None
    workspace = Path(decoded)
    return decoded, workspace.name or decoded


def _session_id(path: Path) -> str:
    return path.parent.name


def external_id_from_path(path: Path) -> str:
    return _session_id(path)


def _text(value: Any) -> str:
    text = extract_text(value).strip()
    match = _USER_QUERY_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def _summary_path(path: Path) -> Path:
    return path.parent / "summary.json"


def _load_summary(path: Path, warnings: list[str]) -> dict[str, Any]:
    summary_path = _summary_path(path)
    try:
        if not summary_path.is_file() or summary_path.is_symlink():
            return {}
        value = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warnings.append(f"{summary_path}: unreadable Grok metadata: {exc}")
        return {}
    return value if isinstance(value, dict) else {}


def _field(obj: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in obj and obj[name] not in (None, ""):
            return obj[name]
    return None


def _summary_count(summary: dict[str, Any], name: str) -> int | None:
    value = summary.get(name)
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_bootstrap_only_session(
    summary: dict[str, Any], records: list[dict[str, Any]] | None
) -> bool:
    """Recognize Grok's two-record CLI setup artifact and nothing broader."""
    if (
        _field(summary, "agent_name", "agent") != "grok-build-plan"
        or _summary_count(summary, "num_messages") not in {0, 1}
        or _summary_count(summary, "num_chat_messages") != 2
        or not isinstance(records, list)
        or len(records) != 2
    ):
        return False
    first, second = records
    if str(first.get("type") or "").casefold() != "system":
        return False
    if (
        str(second.get("type") or "").casefold() != "user"
        or str(second.get("synthetic_reason") or "").casefold()
        != "system_reminder"
    ):
        return False
    first_text = extract_text(first.get("content")).lstrip()
    second_text = extract_text(second.get("content")).casefold()
    return (
        _GROK_BOOTSTRAP_PROMPT.match(first_text) is not None
        and _GROK_SKILL_REMINDER in second_text
    )


def is_bootstrap_only_artifact(
    path: Path,
    *,
    expected_revision: tuple[int, int] | None = None,
    expected_content_hash: str | None = None,
    verify_current_dependencies: bool = True,
) -> bool:
    """Verify a stable two-record Grok setup artifact without retaining text."""
    try:
        summary_path = _summary_path(path)
        if (
            path.is_symlink()
            or summary_path.is_symlink()
            or not path.is_file()
            or not summary_path.is_file()
        ):
            return False
        main_before = file_stat(path)
        summary_before = file_stat(summary_path)
        main = path.read_bytes()
        summary_bytes = summary_path.read_bytes()
        if main_before != file_stat(path) or summary_before != file_stat(summary_path):
            return False
        summary = json.loads(summary_bytes.decode("utf-8"))
        records: list[dict[str, Any]] = []
        for _start, _end, obj, error in iter_jsonl_bytes(main, source=str(path)):
            if error is not None or not isinstance(obj, dict):
                return False
            records.append(obj)
        if not isinstance(summary, dict) or not is_bootstrap_only_session(summary, records):
            return False
        dependency_pairs = [(path, summary_path)]
        resolved_summary = summary_path.resolve(strict=False)
        if str(resolved_summary) != str(summary_path):
            dependency_pairs.append((path, resolved_summary))
        snapshots = []
        for main_path, metadata_path in dependency_pairs:
            states = (
                str(main_path).encode() + b"\0" + main + b"\0"
                + str(metadata_path).encode() + b"\0" + summary_bytes + b"\0"
            )
            digest = hash_bytes(b"agentlog-grok-composite-v2\0" + states)
            snapshots.append((
                (len(main), int(digest, 16) & ((1 << 63) - 1)),
                digest,
            ))
        if main_before != file_stat(path) or summary_before != file_stat(summary_path):
            return False
        if expected_revision is not None or expected_content_hash is not None:
            if expected_revision is None or expected_content_hash is None:
                return False
            if (expected_revision, expected_content_hash) not in snapshots:
                return False
        if verify_current_dependencies:
            dependencies = GrokAdapter()._dependency_paths(path)
            if len(dependencies) != 2 or {item.name for item in dependencies} != {
                "chat_history.jsonl",
                "summary.json",
            }:
                return False
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return False
    return True


def autonomous_user_query_index(
    summary: dict[str, Any], records: list[dict[str, Any]] | None
) -> int | None:
    """Return the first real user-query record in an exact autonomous run."""
    if (
        _field(summary, "agent_name", "agent") != "grok-build-plan"
        or not isinstance(records, list)
        or not records
    ):
        return None
    first = records[0]
    if str(first.get("type") or "") != "system":
        return None
    system_text = extract_text(first.get("content"))
    if not system_text.startswith(_GROK_AUTONOMOUS_PROMPT_PREFIX):
        return None
    for index, record in enumerate(records):
        if str(record.get("type") or "") != "user":
            continue
        if str(record.get("synthetic_reason") or "").strip():
            continue
        raw_text = extract_text(record.get("content"))
        match = _USER_QUERY_RE.search(raw_text)
        if match and match.group(1).strip():
            return index
    return None


def is_autonomous_agent_session(
    summary: dict[str, Any],
    records: list[dict[str, Any]] | None,
    *,
    has_parent: bool = False,
) -> bool:
    """Recognize an unlinked autonomous run without inferring from metadata."""
    return not has_parent and autonomous_user_query_index(summary, records) is not None


def _git_fields(obj: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    git = obj.get("git")
    if not isinstance(git, dict):
        git = {}
    repo = _field(git, "repository_url", "repo", "remote") or _field(
        obj, "repository_url", "repo", "remote"
    )
    branch = _field(git, "branch", "git_branch") or _field(
        obj, "branch", "git_branch", "gitBranch"
    )
    commit = _field(git, "commit_hash", "commit", "sha") or _field(
        obj, "commit_hash", "commit", "sha"
    )
    return (
        str(repo) if repo else None,
        str(branch) if branch else None,
        str(commit) if commit else None,
    )


def _tool_id(payload: dict[str, Any]) -> str | None:
    value = _field(payload, "call_id", "callId", "tool_call_id", "toolCallId", "id")
    return str(value).strip() if value is not None and str(value).strip() else None


def _tool_name(payload: dict[str, Any]) -> str:
    value = _field(payload, "name", "tool_name", "toolName", "function")
    if isinstance(value, dict):
        value = value.get("name")
    if value == "use_tool":
        arguments = payload.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if isinstance(arguments, dict):
            nested = _field(arguments, "tool_name", "toolName", "name")
            if nested:
                return str(nested)
    return str(value or "tool")


def _tool_detail(payload: dict[str, Any]) -> str | None:
    for key in ("arguments", "input", "params"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            for nested in ("command", "cmd", "path", "file_path"):
                if isinstance(value.get(nested), str) and value[nested].strip():
                    return value[nested]
    return None


def _success(payload: dict[str, Any]) -> bool | None:
    value = _field(payload, "success", "ok", "is_error", "isError")
    if value is None:
        return None
    if isinstance(value, bool):
        return not value if "is_error" in payload or "isError" in payload else value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "ok", "success", "1"}:
            return True
        if normalized in {"false", "no", "error", "failure", "0"}:
            return False
    return None


def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _usage(record: dict[str, Any], *, seq: int, message_seq: int | None, model: str | None, ts) -> TokenUsage | None:
    raw = _field(record, "usage", "token_usage", "tokenUsage")
    if not isinstance(raw, dict):
        return None
    fields = {
        "input_tokens": _int(_field(raw, "input_tokens", "inputTokens", "prompt_tokens")),
        "output_tokens": _int(_field(raw, "output_tokens", "outputTokens", "completion_tokens")),
        "cached_input_tokens": _int(_field(raw, "cached_input_tokens", "cachedInputTokens")),
        "reasoning_output_tokens": _int(_field(raw, "reasoning_output_tokens", "reasoningTokens")),
        "total_tokens": _int(_field(raw, "total_tokens", "totalTokens")),
    }
    if all(value is None for value in fields.values()):
        return None
    extras = {
        key: value
        for key, value in raw.items()
        if key not in {
            "input_tokens", "inputTokens", "prompt_tokens", "output_tokens",
            "outputTokens", "completion_tokens", "cached_input_tokens",
            "cachedInputTokens", "reasoning_output_tokens", "reasoningTokens",
            "total_tokens", "totalTokens",
        }
        and value not in (None, "", [], {})
    }
    return TokenUsage(
        seq=seq,
        message_seq=message_seq,
        granularity="message",
        usage_source="grok_message_usage",
        model=model,
        timestamp=ts,
        extras=extras,
        **fields,
    )


def _child_prompt(child_meta: dict[str, Any]) -> str:
    value = _field(child_meta, "prompt", "description")
    return _text(value) if value is not None else ""


def _matches_child_prompt(record: dict[str, Any], prompt: str) -> bool:
    return (
        bool(prompt)
        and record.get("type") == "user"
        and _text(record.get("content")) == prompt
    )


def _terminal_output_is_duplicate(records: list[dict[str, Any]], output: str) -> bool:
    for record in reversed(records):
        if record.get("type") == "assistant":
            return _text(record.get("content")) == output
    return False


def _merge_child_records(
    records: list[dict[str, Any]],
    child_meta: dict[str, Any],
    output_value: Any,
    warnings: list[str],
) -> list[dict[str, Any]]:
    merged = [dict(record) for record in records]
    prompt = _child_prompt(child_meta)
    prompt_found = False
    for record in merged:
        if _matches_child_prompt(record, prompt):
            record["_agentlog_parent_authored_prompt"] = True
            prompt_found = True
            break
    if prompt and not prompt_found:
        merged.insert(
            0,
            {
                "type": "user",
                "content": prompt,
                "created_at": _field(child_meta, "started_at", "createdAt"),
                "_agentlog_parent_authored_prompt": True,
            },
        )

    if not isinstance(output_value, dict) or not isinstance(output_value.get("output"), str):
        warnings.append("Grok subagent output is unreadable")
        return merged
    output = output_value["output"].strip()
    if not output or _terminal_output_is_duplicate(merged, output):
        return merged
    merged.append(
        {
            "type": "assistant",
            "content": output,
            "model_id": _field(child_meta, "effective_model_id", "model_id", "model"),
            "created_at": _field(child_meta, "completed_at", "completedAt"),
        }
    )
    return merged


class GrokAdapter(TranscriptAdapter):
    harness = Harness.GROK
    supports_byte_append = False
    uses_composite_source = True

    def _session_root(self, path: Path) -> Path:
        root = GROK_SESSIONS_DIR.expanduser().resolve()
        try:
            relative = path.expanduser().resolve(strict=False).relative_to(root)
        except ValueError:
            return path.parent
        if len(relative.parts) >= 2:
            return root / relative.parts[0] / relative.parts[1]
        return path.parent

    def canonical_artifact_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        if candidate.name == "chat_history.jsonl":
            return candidate
        root = GROK_SESSIONS_DIR.expanduser().resolve()
        try:
            relative = candidate.resolve(strict=False).relative_to(root)
        except ValueError:
            return candidate
        if len(relative.parts) >= 4 and relative.parts[2] == "subagents":
            child_id = relative.parts[3]
            return root / relative.parts[0] / child_id / "chat_history.jsonl"
        if len(relative.parts) >= 2:
            return root / relative.parts[0] / relative.parts[1] / "chat_history.jsonl"
        return candidate

    def accepts_watch_path(self, path: Path, source_root: Path) -> bool:
        try:
            path.expanduser().resolve(strict=False).relative_to(
                source_root.expanduser().resolve()
            )
        except ValueError:
            return False
        return path.name in {
            "chat_history.jsonl",
            "summary.json",
            "updates.jsonl",
            "meta.json",
            "output.json",
        } or "compaction_requests" in path.parts or "compaction_checkpoints" in path.parts

    def _dependency_paths(self, path: Path) -> list[Path]:
        session_root = self._session_root(path)
        if path.name == "chat_history.jsonl":
            out = [path, session_root / "summary.json"]
            for folder in ("compaction_requests", "compaction_checkpoints"):
                out.extend(sorted(session_root.glob(f"{folder}/*.json")))
            child_id = _session_id(path)
            parent_meta = sorted(
                session_root.parent.glob(f"*/subagents/{child_id}/meta.json")
            )
            if len(parent_meta) > 1:
                raise OSError(f"ambiguous Grok parent metadata for {child_id}")
            if len(parent_meta) == 1:
                meta = parent_meta[0]
                parent_root = meta.parents[2]
                out.extend([
                    meta,
                    meta.with_name("output.json"),
                    *sorted(parent_root.glob("workflows/*/state.json")),
                ])
            return out
        return [path]

    def _child_metadata(
        self, path: Path, dependency_bytes: dict[str, bytes] | None = None
    ) -> dict[str, Any]:
        if dependency_bytes is None:
            return {}
        child_id = _session_id(path)
        metadata: dict[str, Any] = {}
        for candidate_s, raw in dependency_bytes.items():
            candidate = Path(candidate_s)
            if candidate.name != "meta.json" or candidate.parent.name != child_id:
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("child_session_id", child_id) == child_id:
                metadata = dict(value)
                break
        for workflow_s, raw in dependency_bytes.items():
            workflow_file = Path(workflow_s)
            if workflow_file.name != "state.json":
                continue
            try:
                workflow = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            state = workflow.get("state") if isinstance(workflow, dict) else None
            agents = state.get("agents") if isinstance(state, dict) else None
            if not isinstance(agents, list):
                continue
            for position, agent in enumerate(agents):
                if isinstance(agent, dict) and str(agent.get("agent_id")) == child_id:
                    metadata["workflow_id"] = str(state.get("run_id") or workflow_file.parent.name)
                    metadata["workflow_name"] = str(state.get("name") or "")
                    metadata["workflow_position"] = position
                    return metadata
        return metadata

    def _records(
        self,
        path: Path,
        data: bytes,
        warnings: list[str],
        dependency_bytes: dict[str, bytes] | None = None,
    ) -> tuple[list[dict[str, Any]] | None, int]:
        parsed: list[dict[str, Any]] = []
        safe = 0
        for _start, end, obj, err in iter_jsonl_bytes(data, source=str(path)):
            safe = max(safe, end)
            if err:
                warnings.append(err)
                if "incomplete trailing line" in err:
                    continue
            if obj is not None:
                parsed.append(obj)
        if dependency_bytes is None:
            return parsed, safe
        requests = []
        for candidate_s, raw in dependency_bytes.items():
            candidate = Path(candidate_s)
            if candidate.parent.name != "compaction_requests":
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                warnings.append(f"{candidate}: unreadable compaction request: {exc}")
                continue
            if isinstance(value, dict) and isinstance(value.get("chat_history"), list):
                requests.append((str(value.get("created_at") or ""), candidate, value))
        if not requests:
            return parsed, safe
        requests.sort(key=lambda item: item[0])
        _created, request_path, request = requests[-1]
        checkpoints = []
        for candidate_s, raw in dependency_bytes.items():
            candidate = Path(candidate_s)
            if candidate.parent.name != "compaction_checkpoints":
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                warnings.append(f"{candidate}: unreadable compaction checkpoint: {exc}")
                continue
            if isinstance(value, dict) and isinstance(value.get("compacted_history"), list):
                checkpoints.append((str(value.get("created_at") or ""), candidate, value))
        matching = []
        for _cp_created, candidate, checkpoint in checkpoints:
            compacted = checkpoint["compacted_history"]
            if len(parsed) >= len(compacted) and parsed[:len(compacted)] == compacted:
                matching.append((len(compacted), candidate, checkpoint))
        if not matching:
            warnings.append(f"{path}: compaction request/checkpoint boundary could not be verified")
            return None, 0
        prefix_len, checkpoint_path, checkpoint = max(matching, key=lambda item: item[0])
        compacted = checkpoint["compacted_history"]
        if str(checkpoint.get("request_id") or "") not in {request_path.stem, ""}:
            warnings.append(f"{path}: compaction checkpoint request identity is ambiguous")
            return None, 0
        canonical = list(request["chat_history"])
        if len(compacted) >= 3:
            canonical.extend(compacted[2:3])
        canonical.extend(parsed[prefix_len:])
        warnings.append(f"{path}: reconstructed compaction boundary from {request_path.name} and {checkpoint_path.name}")
        return canonical, len(data)

    def discover(self) -> list[Path]:
        root = GROK_SESSIONS_DIR.expanduser()
        if not root.is_dir():
            return []
        resolved_root = root.resolve()
        out: list[Path] = []
        for candidate in root.glob("*/*/chat_history.jsonl"):
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(resolved_root)
            except (OSError, ValueError):
                continue
            if candidate.is_file() and not candidate.is_symlink():
                out.append(candidate)
        return sorted(out)

    def parse_path(
        self, path: Path, data: bytes, *, start_offset: int
    ) -> list[ParseResult]:
        del start_offset
        snapshot = self.capture_source(path)
        return self.parse_source_snapshot(path, snapshot)

    def capture_source(self, path: Path) -> SourceSnapshot:
        dependencies: dict[str, bytes] = {}
        states: list[bytes] = []
        for dependency in self._dependency_paths(path):
            try:
                before = file_stat(dependency)
                raw = dependency.read_bytes()
                after = file_stat(dependency)
            except OSError:
                if dependency == path:
                    raise
                raw = b""
                before = after = (0, 0)
            if before != after:
                raise OSError(f"Grok source changed while capturing: {dependency}")
            key = str(dependency)
            dependencies[key] = raw
            states.append(key.encode() + b"\0" + raw + b"\0")
        data = dependencies[str(path)]
        envelope = json.dumps(
            {"main": b64encode(data).decode("ascii"), "dependencies": {
                key: b64encode(raw).decode("ascii")
                for key, raw in dependencies.items()
            }},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hash_bytes(b"agentlog-grok-composite-v2\0" + b"".join(states))
        return SourceSnapshot(
            data=envelope,
            revision=(len(data), int(digest, 16) & ((1 << 63) - 1)),
            content_hash=digest,
        )

    def parse_source_snapshot(
        self, path: Path, snapshot: SourceSnapshot
    ) -> list[ParseResult]:
        try:
            envelope = json.loads(snapshot.data.decode("utf-8"))
            dependencies = {
                str(key): b64decode(value)
                for key, value in envelope["dependencies"].items()
            }
            data = dependencies[str(path)]
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            return []
        warnings: list[str] = []
        summary_raw = dependencies.get(str(_summary_path(path)))
        if summary_raw is None:
            summary_raw = next(
                (
                    raw
                    for candidate, raw in dependencies.items()
                    if Path(candidate).name == "summary.json"
                    and Path(candidate).parent.name == _session_id(path)
                ),
                None,
            )
        try:
            summary = json.loads(summary_raw.decode("utf-8")) if summary_raw else {}
        except (UnicodeError, json.JSONDecodeError):
            summary = {}
        child_meta = self._child_metadata(path, dependencies)
        records, safe = self._records(path, data, warnings, dependencies)
        if child_meta and records is not None:
            output_raw = next(
                (
                    raw
                    for candidate, raw in dependencies.items()
                    if Path(candidate).name == "output.json"
                    and Path(candidate).parent.name == _session_id(path)
                ),
                b"",
            )
            try:
                output_value = json.loads(output_raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                output_value = None
            records = _merge_child_records(records, child_meta, output_value, warnings)
        return [
            self.parse_chunk(
                path,
                data,
                start_offset=0,
                _records_override=(records, safe),
                _summary_override=summary,
                _child_meta_override=child_meta,
                _warnings_override=warnings,
                _source_dependencies_override=list(dependencies),
            )
        ]

    def parse_chunk(
        self,
        path: Path,
        data: bytes,
        *,
        start_offset: int,
        _records_override: tuple[list[dict[str, Any]] | None, int] | None = None,
        _summary_override: dict[str, Any] | None = None,
        _child_meta_override: dict[str, Any] | None = None,
        _warnings_override: list[str] | None = None,
        _source_dependencies_override: list[str] | None = None,
    ) -> ParseResult:
        warnings: list[str] = list(_warnings_override or [])
        messages: list[NormalizedMessage] = []
        tools: list[ToolEvent] = []
        usages: list[TokenUsage] = []
        call_names: dict[str, str] = {}
        call_message_seq: dict[str, int | None] = {}
        external_id = _session_id(path)
        cwd, repo = _workspace(path)
        branch: str | None = None
        commit: str | None = None
        parent: str | None = None
        model: str | None = None
        effort: str | None = None
        effort_source: str | None = None
        started_at = None
        ended_at = None
        msg_seq = 0
        tool_seq = 0
        last_assistant_seq: int | None = None
        parent_prompt_marked = False
        safe_consumed = 0
        summary = (
            _summary_override
            if _summary_override is not None
            else _load_summary(path, warnings)
        )
        info = summary.get("info") if isinstance(summary.get("info"), dict) else {}
        summary_cwd = _field(info, "cwd") or _field(summary, "cwd")
        summary_repo = _field(info, "git_root_dir") or _field(summary, "git_root_dir")
        summary_model = _field(summary, "current_model_id", "model_id")
        summary_effort = _field(summary, "reasoning_effort", "effort")
        summary_agent = _field(summary, "agent_name", "agent")
        child_meta = (
            _child_meta_override
            if _child_meta_override is not None
            else self._child_metadata(path)
        )
        parent = str(
            _field(child_meta, "parent_session_id", "parentSessionId", "parent_id")
            or ""
        ) or parent
        summary_parent = _field(
            summary, "parent_session_id", "parentSessionId", "parent_id"
        )
        if summary_parent and str(summary_parent) != external_id and not parent:
            parent = str(summary_parent)
        if parent and not parent.startswith(f"{Harness.GROK.value}:"):
            parent = f"{Harness.GROK.value}:{parent}"
        child_model = _field(child_meta, "effective_model_id", "model_id", "model")
        child_effort = _field(child_meta, "reasoning_effort", "effort")
        child_type = _field(child_meta, "subagent_type", "agent_type")
        if child_model:
            model = str(child_model)
        if child_effort is not None:
            effort, effort_source = normalize_effort(str(child_effort))
        child_cwd = _field(child_meta, "cwd", "workspace")
        cwd = str(child_cwd or summary_cwd) if child_cwd or summary_cwd else cwd
        repo = str(summary_repo) if summary_repo else repo
        model = str(summary_model) if summary_model and model is None else model
        if summary_effort is not None:
            effort, effort_source = normalize_effort(str(summary_effort))
        branch = str(_field(summary, "head_branch", "branch") or "") or branch
        commit = str(_field(summary, "head_commit", "commit", "sha") or "") or commit
        started_at = parse_ts(_field(summary, "created_at", "createdAt"))
        ended_at = parse_ts(_field(summary, "updated_at", "updatedAt", "last_active_at"))

        if _records_override is None:
            records, canonical_safe = self._records(path, data, warnings)
        else:
            records, canonical_safe = _records_override
        checkpoint_blocked = records is None
        if records is None:
            records = []
        record_parent = next(
            (
                str(_field(record, "parent_session_id", "parentSessionId", "parent_id"))
                for record in records
                if _field(record, "parent_session_id", "parentSessionId", "parent_id")
                and str(_field(record, "parent_session_id", "parentSessionId", "parent_id"))
                != external_id
            ),
            None,
        )
        if record_parent and not parent:
            parent = record_parent
            if not parent.startswith(f"{Harness.GROK.value}:"):
                parent = f"{Harness.GROK.value}:{parent}"
        autonomous_query_index = autonomous_user_query_index(summary, records)
        autonomous = is_autonomous_agent_session(
            summary,
            records,
            has_parent=bool(child_meta or parent),
        )
        record_iter = (
            ((0, len(data), obj, None) for obj in records)
            if not checkpoint_blocked
            else iter_jsonl_bytes(data, source=str(path))
        )
        for record_index, (_start, end, obj, err) in enumerate(record_iter):
            safe_consumed = max(safe_consumed, end)
            if err:
                warnings.append(err)
                continue
            assert obj is not None
            ts = parse_ts(_field(obj, "timestamp", "created_at", "createdAt"))
            if started_at is None and ts:
                started_at = ts
            if ts:
                ended_at = ts
            cwd = _field(obj, "cwd", "workspace", "workspace_path", "project_path") or cwd
            r, b, c = _git_fields(obj)
            repo, branch, commit = r or repo, b or branch, c or commit
            parent_raw = _field(obj, "parent_session_id", "parentSessionId", "parent_id")
            if parent_raw and str(parent_raw) != external_id:
                parent = str(parent_raw)
                if not parent.startswith(f"{Harness.GROK.value}:"):
                    parent = f"{Harness.GROK.value}:{parent}"
            raw_effort = _field(obj, "reasoning_effort", "reasoningEffort", "effort")
            if raw_effort is not None:
                effort, effort_source = normalize_effort(str(raw_effort))
            raw_model = _field(obj, "model_id", "model", "modelId")
            if raw_model:
                model = str(raw_model)

            record_type = str(obj.get("type") or "")
            synthetic_reason = str(obj.get("synthetic_reason") or "").strip().lower()
            if record_type == "system":
                text = extract_text(obj.get("content")).strip()
                if text:
                    msg_seq += 1
                    messages.append(
                        NormalizedMessage(
                            seq=msg_seq,
                            role="system",
                            timestamp=ts,
                            text=text,
                            content_hash=content_hash_text(text),
                            is_tool_plumbing=True,
                            authored_by_agent=True,
                        )
                    )
                continue
            if record_type == "user":
                raw_text = extract_text(obj.get("content")).strip()
                parent_authored_prompt = bool(
                    obj.get("_agentlog_parent_authored_prompt")
                )
                synthetic = synthetic_reason in {
                    "compaction_meta",
                    "system_reminder",
                    "notification_drain",
                    "task_completed",
                }
                synthetic = synthetic or raw_text.lstrip().startswith(
                    "Your task is to produce a faithful, concise summary"
                )
                if raw_text.lstrip().startswith("<user_info>"):
                    synthetic = True
                image_only = raw_text.lstrip().startswith("<image_files>")
                if image_only:
                    text = "[Image attachment]"
                else:
                    text = raw_text if synthetic else _text(obj.get("content"))
                flags = classify_synthetic_user_text(raw_text)
                if not synthetic and "<user_query>" in raw_text.casefold() and not _USER_QUERY_RE.search(raw_text):
                    warnings.append(f"{path}: malformed user_query wrapper omitted")
                    continue
                if not text:
                    continue
                msg_seq += 1
                parent_prompt_marked = parent_prompt_marked or parent_authored_prompt
                messages.append(
                    NormalizedMessage(
                        seq=msg_seq,
                        role="user",
                        timestamp=ts,
                        text=text,
                        content_hash=content_hash_text(text),
                        is_tool_plumbing=synthetic or flags.is_tool_plumbing,
                        authored_by_agent=(
                            synthetic
                            or flags.authored_by_agent
                            or parent_authored_prompt
                            or (
                                autonomous
                                and record_index == autonomous_query_index
                            )
                        ),
                    )
                )
                continue
            if record_type == "assistant":
                text = _text(obj.get("content"))
                if text:
                    msg_seq += 1
                    last_assistant_seq = msg_seq
                    messages.append(
                        NormalizedMessage(
                            seq=msg_seq,
                            role="assistant",
                            timestamp=ts,
                            model=str(raw_model) if raw_model else model,
                            provider="xai",
                            effort=effort,
                            effort_source=effort_source,
                            text=text,
                            content_hash=content_hash_text(text),
                        )
                    )
                usage = _usage(
                    obj,
                    seq=len(usages) + 1,
                    message_seq=last_assistant_seq,
                    model=str(raw_model) if raw_model else model,
                    ts=ts,
                )
                if usage is not None:
                    usages.append(usage)
                for raw_call in obj.get("tool_calls") or []:
                    if not isinstance(raw_call, dict):
                        continue
                    call_id = _tool_id(raw_call) or f"grok-call-{tool_seq + 1}"
                    name = _tool_name(raw_call)
                    call_names[call_id] = name
                    call_message_seq[call_id] = last_assistant_seq
                    tool_seq += 1
                    tools.append(
                        ToolEvent(
                            seq=tool_seq,
                            message_seq=last_assistant_seq,
                            tool_name=name,
                            action="call",
                            operation_kind=classify_operation(name, _tool_detail(raw_call)),
                        )
                    )
                continue
            if record_type == "tool_result":
                call_id = _tool_id(obj)
                payload = obj
                if call_id is None:
                    call_id = f"grok-result-{tool_seq + 1}"
                result_name = _tool_name(obj) if any(k in obj for k in ("name", "tool_name", "toolName")) else call_names.get(call_id, "tool")
                tool_seq += 1
                tools.append(
                    ToolEvent(
                        seq=tool_seq,
                        message_seq=call_message_seq.get(call_id, last_assistant_seq),
                        tool_name=result_name,
                        action="result",
                        success=_success(payload),
                        operation_kind=classify_operation(result_name),
                    )
                )

        if child_meta and not parent_prompt_marked:
            flag_parent_authored_prompt(messages)

        bootstrap_only = is_bootstrap_only_session(summary, records)
        if bootstrap_only:
            messages = []
            tools = []
            usages = []

        session = NormalizedSession(
            harness=Harness.GROK,
            external_id=external_id,
            parent_session_id=parent,
            thread_source=(
                GROK_BOOTSTRAP_ONLY_THREAD_SOURCE
                if bootstrap_only
                else GROK_AUTONOMOUS_AGENT_UNLINKED_THREAD_SOURCE
                if autonomous
                else "workflow_subagent" if child_meta else None
            ),
            started_at=started_at,
            ended_at=ended_at,
            repo=repo,
            cwd=cwd,
            branch=branch,
            commit_sha=commit,
            model=model,
            provider="xai" if model else None,
            agent_profile=(str(summary_agent) if summary_agent else "grok-build") if model else None,
            effort=effort,
            effort_source=effort_source,
        )
        return ParseResult(
            session=session,
            messages=messages,
            tool_events=tools,
            token_usages=usages,
            warnings=warnings,
            bytes_consumed=(start_offset + canonical_safe if not checkpoint_blocked else start_offset + safe_consumed),
            extras={
                "checkpoint_blocked": checkpoint_blocked,
                "checkpoint_blocked_reason": "unverified Grok compaction boundary"
                if checkpoint_blocked
                else None,
                "workflow_id": _field(child_meta, "workflow_id", "workflowId"),
                "workflow_group": (
                    {
                        "id": _field(child_meta, "workflow_id", "workflowId"),
                        "label": _field(child_meta, "workflow_name", "workflowName"),
                        "position": _field(child_meta, "workflow_position", "position"),
                    }
                    if _field(child_meta, "workflow_id", "workflowId")
                    else None
                ),
                "subagent_type": child_type,
                **(
                    {"activity_suppressed": "grok_bootstrap_only"}
                    if bootstrap_only
                    else {}
                ),
                **(
                    {"autonomous_agent_unlinked": True}
                    if autonomous
                    else {}
                ),
                "source_dependencies": _source_dependencies_override or [str(path)],
            },
        )
