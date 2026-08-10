"""Static harness registry and declared capability matrix.

Live coverage percentages are computed at request time from the DB
(see ``agentlog.api.harnesses``); this module stores only declared
capability levels so stale rates never land in source control.
"""

from __future__ import annotations

from typing import Any, Literal

CapabilityLevel = Literal["supported", "partial", "absent", "unknown"]

CAPABILITY_KEYS: tuple[str, ...] = (
    "per_message_model",
    "per_message_tokens",
    "effort",
    "branch",
    "commit_sha",
    "ended_at",
    "tool_events",
    "skill_exposures",
    "subagent_links",
)

HarnessRecord = dict[str, Any]

# Keys match sessions.harness for active ingest adapters.
HARNESSES: dict[str, HarnessRecord] = {
    "claude": {
        "id": "claude",
        "display_name": "Claude Code",
        "vendor": "Anthropic",
        "ingest_status": "active",
        "transcript_locations": [
            "~/.claude/projects/**/*.jsonl",
        ],
        "capabilities": {
            "per_message_model": "supported",
            "per_message_tokens": "supported",
            "effort": "partial",
            "branch": "partial",
            "commit_sha": "absent",
            "ended_at": "supported",
            "tool_events": "supported",
            "skill_exposures": "supported",
            "subagent_links": "supported",
        },
        "notes": {
            "per_message_tokens": "Usage reported per assistant message.",
            "branch": "Populated when project path / git context is available.",
            "commit_sha": "Not present in Claude Code session JSONL today.",
        },
    },
    "codex": {
        "id": "codex",
        "display_name": "Codex",
        "vendor": "OpenAI",
        "ingest_status": "active",
        "transcript_locations": [
            "~/.codex/sessions/**/rollout-*.jsonl",
            "~/.codex/archived_sessions/",
        ],
        "capabilities": {
            "per_message_model": "supported",
            "per_message_tokens": "partial",
            "effort": "partial",
            "branch": "partial",
            "commit_sha": "partial",
            "ended_at": "supported",
            "tool_events": "supported",
            "skill_exposures": "absent",
            "subagent_links": "supported",
        },
        "notes": {
            "per_message_tokens": (
                "Turn and session_cumulative snapshots; not additive "
                "per-message rows like Claude."
            ),
            "skill_exposures": "No skill-exposure events observed in Codex rollouts.",
            "subagent_links": "parent_session_id from spawn / parent_thread_id.",
        },
    },
    "cursor": {
        "id": "cursor",
        "display_name": "Cursor",
        "vendor": "Cursor",
        "ingest_status": "active",
        "transcript_locations": [
            "~/.cursor/projects/*/agent-transcripts/**/*.jsonl",
            "~/Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        ],
        "capabilities": {
            "per_message_model": "partial",
            "per_message_tokens": "absent",
            "effort": "partial",
            "branch": "partial",
            "commit_sha": "absent",
            "ended_at": "supported",
            "tool_events": "supported",
            "skill_exposures": "absent",
            "subagent_links": "supported",
        },
        "notes": {
            "per_message_model": (
                "Recovered from state.vscdb when present; often null on "
                "transcript-only rows."
            ),
            "branch": "Partial coverage from project / composer metadata.",
            "per_message_tokens": "Not ingested from Cursor sources yet.",
        },
    },
    "warp": {
        "id": "warp",
        "display_name": "Warp",
        "vendor": "Warp",
        "ingest_status": "active",
        "transcript_locations": [
            "~/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite",
        ],
        "capabilities": {
            "per_message_model": "absent",
            "per_message_tokens": "absent",
            "effort": "absent",
            "branch": "absent",
            "commit_sha": "absent",
            "ended_at": "supported",
            "tool_events": "partial",
            "skill_exposures": "absent",
            "subagent_links": "absent",
        },
        "notes": {
            "_": (
                "Local warp.sqlite stores user Query turns and ActionResult "
                "tool events per conversation_id. ai_blocks (assistant text) "
                "is empty on observed installs — replies are not available."
            ),
            "tool_events": (
                "ActionResult kinds such as RequestCommandOutput / "
                "SuggestCreatePlan; not full tool call/result pairs."
            ),
            "per_message_model": "model_id is typically 'auto' with no per-turn model.",
        },
    },
    "hermes": {
        "id": "hermes",
        "display_name": "Hermes",
        "vendor": "Nous Research",
        "ingest_status": "active",
        "transcript_locations": [
            "~/.hermes/state.db",
            "~/.hermes/kanban.db",
            "~/.hermes/kanban/boards/*/kanban.db",
        ],
        "capabilities": {
            "per_message_model": "partial",
            "per_message_tokens": "partial",
            "effort": "partial",
            "branch": "supported",
            "commit_sha": "absent",
            "ended_at": "supported",
            "tool_events": "supported",
            "skill_exposures": "absent",
            "subagent_links": "supported",
        },
        "notes": {
            "_": (
                "Parses Hermes SessionDB (state.db) and Kanban boards. "
                "No ~/.hermes install was present when the adapter landed; "
                "coverage is validated via synthetic fixtures."
            ),
            "per_message_model": "Session model applied to assistant rows when set.",
            "per_message_tokens": (
                "Optional messages.token_count plus session_model_usage rollups."
            ),
            "subagent_links": "parent_session_id from sessions / kanban session_id.",
        },
    },
    "t3code": {
        "id": "t3code",
        "display_name": "T3 Code",
        "vendor": "Ping Labs",
        "ingest_status": "active",
        "transcript_locations": [
            "~/.t3/userdata/state.sqlite",
            "~/.t3code/userdata/state.sqlite",
            "~/.config/t3*/userdata/state.sqlite",
        ],
        "capabilities": {
            "per_message_model": "supported",
            "per_message_tokens": "absent",
            "effort": "supported",
            "branch": "supported",
            "commit_sha": "absent",
            "ended_at": "supported",
            "tool_events": "supported",
            "skill_exposures": "absent",
            "subagent_links": "partial",
        },
        "notes": {
            "_": (
                "t3 code is an orchestrator over provider CLIs "
                "(cursor / codex / claudeAgent / grok / opencode). The "
                "provider instance is recorded as agent_profile and the "
                "upstream vendor as provider; model holds only the slug. "
                "Installed via a nightly-auto-updating Homebrew cask, so "
                "discovery globs several candidate roots and a missing "
                "directory is a no-data-yet state, not an error."
            ),
            "per_message_model": (
                "thread.turn-start-requested carries modelSelection per "
                "message; thread.meta-updated changes apply from their "
                "event timestamp onward."
            ),
            "per_message_tokens": (
                "state.sqlite records no token counts; usage lives only in "
                "the separate ~/.t3/userdata/usage-scan-cache.json."
            ),
            "effort": (
                "modelSelection.options.effort. t3 also offers ultracode / "
                "ultrathink, which normalize to 'unknown' with the raw "
                "value kept in effort_source."
            ),
            "tool_events": (
                "projection_thread_activities rows with tone tool / "
                "approval / error, linked to a message via turn_id."
            ),
            "subagent_links": (
                "Plan threads link to their implementation thread; there "
                "is no general subagent spawn model."
            ),
        },
    },
}


def list_harnesses(*, ingest_status: str | None = None) -> list[HarnessRecord]:
    items = list(HARNESSES.values())
    if ingest_status is not None:
        items = [h for h in items if h["ingest_status"] == ingest_status]
    return items


def get_harness(harness_id: str) -> HarnessRecord | None:
    return HARNESSES.get(harness_id)


def supports(harness_id: str, capability: str) -> CapabilityLevel:
    """Return declared capability level, or unknown if harness/key missing."""
    record = HARNESSES.get(harness_id)
    if record is None:
        return "unknown"
    caps = record.get("capabilities") or {}
    level = caps.get(capability)
    if level in ("supported", "partial", "absent", "unknown"):
        return level  # type: ignore[return-value]
    return "unknown"
