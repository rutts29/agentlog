"""Atomic claim extraction from the evidence ledger.

Deterministic claims are preferred. LLM-derived claims always carry an
explicit label basis and degrade when adjudications are sparse.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agentlog.analysis.claims.models import (
    EXTRACTOR_VERSION,
    MIN_SESSIONS_FINDING,
    MIN_SESSIONS_FLOOR,
    MAX_EVIDENCE_PER_CLAIM,
    Claim,
    ClaimEvidence,
    SupportStatus,
    clip_quote,
    observational_rate_phrase,
)
from agentlog.analysis.claims.scope import project_label
from agentlog.analysis.skills import skill_aliases
from agentlog.source_reader import CachedSourceTranscriptReader
from agentlog.session_identity import (
    build_identity_context,
    is_suppressed_activity_session,
    logical_projection,
    logical_session_root_ids,
)

THEME_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "dont_act_yet_brake",
        re.compile(
            r"\b("
            r"don'?t act( on it)? yet|"
            r"do not act( on it)? yet|"
            r"just info|"
            r"hold on|"
            r"wait(?:\s+\w+){0,3}\s+don'?t|"
            r"plan first|"
            r"read first|"
            r"investigat\w+ first|"
            r"don'?t (code|start|commit) yet"
            r")\b",
            re.I,
        ),
        (
            "When the user says to wait, investigate, or not act yet, "
            "do not edit files until they give an explicit go-ahead."
        ),
    ),
    (
        "verify_before_done",
        re.compile(
            r"\b("
            r"run (the )?(tests?|typecheck|ci)|"
            r"verify (it |this |before )|"
            r"before (you )?(finish|claim|done)|"
            r"make sure (the )?(tests?|ci)|"
            r"regression test"
            r")\b",
            re.I,
        ),
        (
            "Before reporting work complete, run the relevant verification "
            "(tests, typecheck, or the command the user named) and report "
            "the actual result."
        ),
    ),
    (
        "scope_narrow",
        re.compile(
            r"\b("
            r"only (edit|touch|change)|"
            r"micro[- ]?patch|"
            r"do not expand|"
            r"don'?t (touch|change) other|"
            r"edit only|"
            r"stay in(side)? (the )?scope|"
            r"no drive[- ]by"
            r")\b",
            re.I,
        ),
        (
            "Stay inside the files and scope the user named. Do not expand "
            "into adjacent refactors unless they ask."
        ),
    ),
    (
        "spawn_workers",
        re.compile(
            r"\b("
            r"spawn (a |the )?(worker|subagent|agents?)|"
            r"pawning it off|"
            r"use (a )?worker|"
            r"hand it to (a )?worker|"
            r"don'?t (do|implement) it yourself"
            r")\b",
            re.I,
        ),
        (
            "When the user asks for multi-agent execution, prefer spawning "
            "workers over doing the implementation in the coordinator turn."
        ),
    ),
]

# Cursor built-in / plugin-managed skills are not removal candidates.
_PROTECTED_SKILL_SOURCES = frozenset({"claude-plugins"})
_PROTECTED_SKILL_NAMES = frozenset(
    {
        "create-hook",
        "create-rule",
        "create-skill",
        "create-subagent",
        "migrate-to-skills",
        "update-cursor-settings",
        "update-cli-config",
        "shell",
        "loop",
        "canvas",
        "onboard",
        "rename-chat",
        "split-to-prs",
        "statusline",
        "automate",
        "autopilot",
        "review-bugbot",
        "review-security",
        "imagegen",
        "openai-docs",
        "skill-installer",
        "skill-creator",
        "plugin-creator",
        "playwright",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _claim_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _support_status(n_sessions: int) -> SupportStatus:
    if n_sessions < MIN_SESSIONS_FLOOR:
        return "abstain"
    if n_sessions < MIN_SESSIONS_FINDING:
        return "insufficient"
    return "ok"


def _label_basis(conn: sqlite3.Connection) -> dict[str, Any]:
    ux_n = 0
    adj_n = 0
    if _table_exists(conn, "ux_observations"):
        ux_n = int(
            conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()["c"]
        )
    if _table_exists(conn, "adjudications"):
        adj_n = int(
            conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()["c"]
        )
    validated = adj_n >= 20
    return {
        "ux_observations": ux_n,
        "adjudications": adj_n,
        "labels_validated": validated,
        "note": (
            "LLM labels used; inter-labeler agreement historically ~45% on "
            "multi-label turn kinds. Adjudication pass incomplete."
            if not validated
            else "Adjudication sample present; still observational, not causal."
        ),
    }


def _json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _root_session_count(conn: sqlite3.Connection) -> int:
    return len(_eligible_logical_root_ids(conn))


def _eligible_logical_root_ids(conn: sqlite3.Connection) -> set[str]:
    roots = logical_session_root_ids(conn)
    return {
        roots[str(row["id"])]
        for row in conn.execute(
            "SELECT id, thread_source FROM sessions WHERE COALESCE(external_id, '') NOT LIKE 'skills:%'"
        )
        if not is_suppressed_activity_session(row)
    }


def _extract_skill_claims(conn: sqlite3.Connection, now: str) -> list[Claim]:
    if not _table_exists(conn, "skills") or not _table_exists(conn, "skill_exposures"):
        return []
    claims: list[Claim] = []
    logical_roots = logical_session_root_ids(conn)
    eligible_session_ids = {
        str(row["id"])
        for row in conn.execute(
            "SELECT id, thread_source FROM sessions WHERE COALESCE(external_id, '') NOT LIKE 'skills:%'"
        )
        if not is_suppressed_activity_session(row)
    }
    eligible_roots = _eligible_logical_root_ids(conn)
    total_sessions = _root_session_count(conn)
    skills = conn.execute(
        """
        SELECT id, name, source, source_path, description
        FROM skills ORDER BY name, source
        """
    ).fetchall()
    exposures = conn.execute(
        """
        SELECT se.skill_name, se.session_id, s.started_at, s.repo, s.cwd,
               s.thread_source
        FROM skill_exposures se
        JOIN sessions s ON s.id = se.session_id
        """
    ).fetchall()
    exposures = [
        row for row in exposures if not is_suppressed_activity_session(row)
    ]
    by_name: dict[str, list[Any]] = defaultdict(list)
    for r in exposures:
        by_name[str(r["skill_name"])].append(r)

    for skill in skills:
        aliases = skill_aliases(
            str(skill["name"]),
            str(skill["source"]),
            Path(str(skill["source_path"])),
        )
        matched_rows: list[Any] = []
        for exp_name, rows in by_name.items():
            bare = exp_name.split(":", 1)[-1] if ":" in exp_name else exp_name
            if exp_name in aliases or bare in aliases:
                matched_rows.extend(rows)
        session_ids = sorted({str(r["session_id"]) for r in matched_rows})
        exposed_roots = {
            logical_roots.get(session_id, session_id)
            for session_id in session_ids
            if session_id in eligible_session_ids
        }
        exposed_roots.intersection_update(eligible_roots)
        n_sessions = len(exposed_roots)
        exposure_count = len(matched_rows)
        name = str(skill["name"])
        source = str(skill["source"])

        if exposure_count == 0:
            if source in _PROTECTED_SKILL_SOURCES or name in _PROTECTED_SKILL_NAMES:
                continue
            # Only propose unused for user-owned inventories.
            if source not in {"agents", "codex", "cursor"}:
                continue
            source_path = str(skill["source_path"] or "")
            # Skip harness internals / bundled system skills.
            if "/.system/" in source_path or "/node_modules/" in source_path:
                continue
            claims.append(
                Claim(
                    id=_claim_id("skill_unused", str(skill["id"]), EXTRACTOR_VERSION),
                    kind="skill_unused",
                    subject=name,
                    predicate="exposure_count",
                    value={
                        "exposure_count": 0,
                        "sessions_with_exposure": 0,
                        "sessions_total": total_sessions,
                        "source": source,
                        "source_path": skill["source_path"],
                        "phrasing": (
                            f"indexed skill {name} ({source}) had 0 exposures "
                            f"across {total_sessions} root sessions"
                        ),
                        "abstain_reason": "exposure coverage insufficient",
                    },
                    scope_type="skill",
                    scope_id=str(skill["id"]),
                    derivation="deterministic",
                    # Inventory only: zero skill_exposures rows is not proof of
                    # non-use — Cursor/Codex often invoke skills without the
                    # exposure events this join requires.
                    support_status="abstain",
                    sample_size=total_sessions,
                    denominator=total_sessions,
                    rate=0.0,
                    observed_at=now,
                    extractor_name="skill_unused",
                    confidence_basis={
                        "method": "skill_exposures join against indexed skills",
                        "deterministic": True,
                        "abstain_reason": "exposure coverage insufficient",
                        "archive_proposals": "disabled",
                        "proposal_gate": "EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS",
                    },
                    does_not_prove=(
                        "Zero exposures does not prove the skill is unused, "
                        "harmful, or deletable; Cursor/Codex may invoke skills "
                        "without writing skill_exposures rows this detector "
                        "joins on. Archive/DEPRECATED proposals are disabled "
                        "until invocation telemetry covers those harnesses."
                    ),
                    evidence=[
                        ClaimEvidence(
                            meta={
                                "skill_id": skill["id"],
                                "source_path": skill["source_path"],
                            }
                        )
                    ],
                    created_at=now,
                    updated_at=now,
                )
            )
            continue

        support = _support_status(n_sessions)
        rate = (n_sessions / total_sessions) if total_sessions else None
        evidence: list[ClaimEvidence] = []
        for r in matched_rows[:MAX_EVIDENCE_PER_CLAIM]:
            evidence.append(
                ClaimEvidence(
                    session_id=str(r["session_id"]),
                    meta={
                        "skill_name": r["skill_name"],
                        "started_at": r["started_at"],
                        "project": project_label(r["repo"], r["cwd"]),
                    },
                )
            )
        claims.append(
            Claim(
                id=_claim_id("skill_exposure", str(skill["id"]), EXTRACTOR_VERSION),
                kind="skill_exposure",
                subject=name,
                predicate="session_exposure_rate",
                value={
                    "exposure_count": exposure_count,
                    "sessions_with_exposure": n_sessions,
                    "sessions_total": total_sessions,
                    "source": source,
                    "source_path": skill["source_path"],
                    "phrasing": observational_rate_phrase(
                        name, n_sessions, total_sessions or n_sessions
                    ),
                },
                scope_type="skill",
                scope_id=str(skill["id"]),
                derivation="deterministic",
                support_status=support,
                sample_size=n_sessions,
                denominator=total_sessions,
                rate=rate,
                observed_at=now,
                extractor_name="skill_exposure",
                confidence_basis={
                    "method": "COUNT(skill_exposures) with session denominator",
                    "deterministic": True,
                },
                does_not_prove=(
                    "Exposure frequency is not skill effectiveness. No causal "
                    "claim that this skill helped or hurt outcomes."
                ),
                evidence=evidence,
                created_at=now,
                updated_at=now,
            )
        )
    return claims


def _extract_harness_model_claims(conn: sqlite3.Connection, now: str) -> list[Claim]:
    session_rows = conn.execute(
        """
        SELECT id, harness, external_id, repo, cwd, model, model_canonical,
               thread_source
        FROM sessions
        WHERE COALESCE(external_id, '') NOT LIKE 'skills:%'
        """
    ).fetchall()
    session_rows = [
        row for row in session_rows if not is_suppressed_activity_session(row)
    ]
    rows_by_id = {str(row["id"]): row for row in session_rows}
    logical_roots = logical_session_root_ids(conn)
    identity = build_identity_context(conn)
    rows: list[dict[str, Any]] = []
    for root_id in sorted(set(logical_roots.values())):
        root = rows_by_id.get(root_id)
        if root is None:
            continue
        projection = logical_projection(
            conn, root_id, str(root["harness"]), context=identity
        )
        metric_id = str(
            identity.canonical_root_backing_by_source.get(root_id)
            or projection["transcript_session_id"]
            or root_id
        )
        metric = rows_by_id.get(metric_id, root)
        rows.append(
            {
                "repo": root["repo"] or metric["repo"],
                "cwd": root["cwd"] or metric["cwd"],
                "harness": projection["logical_harness"],
                "model": metric["model_canonical"] or metric["model"] or "(unknown)",
                "sessions": 1,
            }
        )
    claims: list[Claim] = []
    # Aggregate by project label for cleaner subjects.
    by_project: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        label = project_label(r["repo"], r["cwd"])
        by_project[label].append(r)

    for label, items in by_project.items():
        total = sum(int(r["sessions"]) for r in items)
        if total < MIN_SESSIONS_FLOOR:
            continue
        support = _support_status(total)
        # Keep top models for the project.
        model_counts: Counter[str] = Counter()
        harness_counts: Counter[str] = Counter()
        for r in items:
            model_counts[str(r["model"])] += int(r["sessions"])
            harness_counts[str(r["harness"])] += int(r["sessions"])
        top_models = model_counts.most_common(5)
        top_harnesses = harness_counts.most_common(5)
        evidence = [
            ClaimEvidence(
                meta={
                    "harness": r["harness"],
                    "model": r["model"],
                    "sessions": int(r["sessions"]),
                    "repo": r["repo"],
                    "cwd": r["cwd"],
                }
            )
            for r in items[:MAX_EVIDENCE_PER_CLAIM]
        ]
        claims.append(
            Claim(
                id=_claim_id("harness_model_usage", label, EXTRACTOR_VERSION),
                kind="harness_model_usage",
                subject=label,
                predicate="usage_mix",
                value={
                    "sessions_total": total,
                    "models": [
                        {"model": m, "sessions": c, "share": c / total}
                        for m, c in top_models
                    ],
                    "harnesses": [
                        {"harness": h, "sessions": c, "share": c / total}
                        for h, c in top_harnesses
                    ],
                    "phrasing": (
                        f"project {label} had {total} root sessions; top model "
                        f"{top_models[0][0]} in {top_models[0][1]}/{total} "
                        f"({top_models[0][1]/total:.4f})"
                    ),
                },
                scope_type="repo",
                scope_id=label,
                derivation="deterministic",
                support_status=support,
                sample_size=total,
                denominator=total,
                rate=top_models[0][1] / total if top_models else None,
                observed_at=now,
                extractor_name="harness_model_usage",
                confidence_basis={
                    "method": "GROUP BY harness, model_canonical on sessions",
                    "deterministic": True,
                },
                does_not_prove=(
                    "Usage mix reflects selection history, not which model is "
                    "best for this project."
                ),
                evidence=evidence,
                created_at=now,
                updated_at=now,
            )
        )
    return claims


def _extract_tool_failure_claims(conn: sqlite3.Connection, now: str) -> list[Claim]:
    if not _table_exists(conn, "tool_events"):
        return []
    rows = conn.execute(
        """
        SELECT tool_name, COUNT(*) AS failures,
               COUNT(DISTINCT session_id) AS sessions
        FROM tool_events
        WHERE success = 0
        GROUP BY tool_name
        HAVING failures >= 3 AND sessions >= ?
        ORDER BY failures DESC
        LIMIT 20
        """,
        (MIN_SESSIONS_FLOOR,),
    ).fetchall()
    # Most current failure names are opaque call ids — skip those.
    claims: list[Claim] = []
    for r in rows:
        name = str(r["tool_name"])
        if name.startswith("call-") or name.startswith("toolu_"):
            continue
        if len(name) > 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name):
            continue
        n_sessions = int(r["sessions"])
        failures = int(r["failures"])
        support = _support_status(n_sessions)
        claims.append(
            Claim(
                id=_claim_id("tool_failure", name, EXTRACTOR_VERSION),
                kind="tool_failure_pattern",
                subject=name,
                predicate="recorded_failure_count",
                value={
                    "failures": failures,
                    "sessions_with_failure": n_sessions,
                    "phrasing": (
                        f"tool_events recorded success=0 for {name} "
                        f"{failures} times across {n_sessions} sessions"
                    ),
                    "note": (
                        "success is NULL for most tool_events; this counts only "
                        "explicit success=0 rows."
                    ),
                },
                scope_type="global",
                scope_id="global",
                derivation="deterministic",
                support_status=support,
                sample_size=n_sessions,
                denominator=n_sessions,
                rate=None,
                observed_at=now,
                extractor_name="tool_failure_pattern",
                confidence_basis={
                    "method": "tool_events.success = 0",
                    "deterministic": True,
                    "coverage": "sparse",
                },
                does_not_prove=(
                    "Explicit failures are a lower bound. NULL success is "
                    "unknown, not success. No claim about root cause."
                ),
                evidence=[
                    ClaimEvidence(
                        meta={"tool_name": name, "failures": failures}
                    )
                ],
                created_at=now,
                updated_at=now,
            )
        )
    return claims


def _theme_blob(text: str, spans_json: str | None) -> str:
    quotes: list[str] = []
    for sp in _json_list(spans_json):
        if isinstance(sp, dict) and sp.get("quote"):
            quotes.append(str(sp["quote"]))
    return f"{text or ''}\n" + "\n".join(quotes)


def _extract_theme_claims(
    conn: sqlite3.Connection, now: str, source_reader: CachedSourceTranscriptReader
) -> list[Claim]:
    if not _table_exists(conn, "ux_observations"):
        return []
    label_basis = _label_basis(conn)
    rows = conn.execute(
        """
        SELECT
            u.window_id,
            u.spans_json,
            u.turn_kinds_json,
            u.user_stance,
            w.session_id,
            w.request_message_id,
            m.text,
            s.repo,
            s.cwd,
            s.started_at,
            s.thread_source
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        JOIN messages m ON m.id = w.request_message_id
        WHERE COALESCE(m.authored_by_agent, 0) = 0
          AND (
            u.turn_kinds_json LIKE '%correction%'
            OR u.turn_kinds_json LIKE '%dont_act_yet%'
            OR u.turn_kinds_json LIKE '%redirect_or_brake%'
            OR u.user_stance IN ('correcting', 'redirecting')
          )
        """
    ).fetchall()
    rows = [row for row in rows if not is_suppressed_activity_session(row)]

    source_text: dict[str, str] = {}
    source_ids = {
        str(row["session_id"])
        for row in rows
        if conn.execute(
            "SELECT transcript_storage FROM sessions WHERE id = ?", (row["session_id"],)
        ).fetchone()["transcript_storage"] == "source_backed"
    }
    for session_id in source_ids:
        source = source_reader(conn, session_id)
        if not source.ready:
            raise RuntimeError(
                f"canonical source unavailable for {session_id}: "
                f"{source.warning or source.status}"
            )
        source_text.update({str(message["id"]): str(message["text"]) for message in source.messages})
    theme_windows: dict[str, list[Any]] = defaultdict(list)
    theme_sessions: dict[str, set[str]] = defaultdict(set)
    theme_projects: dict[str, Counter[str]] = defaultdict(Counter)

    _WORKERISH = frozenset(
        {"worker_brief", "inter_agent_handoff", "coordinator_nudge"}
    )
    _OWNERISH = frozenset(
        {
            "human_task",
            "human_followup",
            "correction",
            "dont_act_yet",
            "redirect_or_brake",
            "clarifying_question",
        }
    )

    for r in rows:
        kinds = set(_json_list(r["turn_kinds_json"]))
        # Prefer owner-facing turns; skip pure worker/coordinator briefs.
        if kinds and kinds.issubset(_WORKERISH):
            continue
        if kinds and not (kinds & _OWNERISH):
            continue
        text = source_text.get(str(r["request_message_id"]), str(r["text"] or ""))
        # Harness-generated reviewer/worker envelopes are not owner instructions.
        if text.lstrip().startswith("You are reviewing") or text.lstrip().startswith(
            "The coordinator sent"
        ):
            continue
        blob = _theme_blob(text, r["spans_json"])
        for theme, pattern, _suggestion in THEME_PATTERNS:
            if pattern.search(blob):
                theme_windows[theme].append(r)
                theme_sessions[theme].add(str(r["session_id"]))
                theme_projects[theme][project_label(r["repo"], r["cwd"])] += 1

    labeled_rows = conn.execute(
        """
        SELECT DISTINCT w.session_id, s.thread_source
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        """
    ).fetchall()
    labeled_sessions = sum(
        1 for row in labeled_rows if not is_suppressed_activity_session(row)
    )

    claims: list[Claim] = []
    for theme, pattern, suggestion in THEME_PATTERNS:
        sessions = theme_sessions.get(theme, set())
        n = len(sessions)
        windows = theme_windows.get(theme, [])
        if not windows:
            continue
        support = _support_status(n)
        top_projects = theme_projects[theme].most_common(5)
        evidence: list[ClaimEvidence] = []
        for r in windows[:MAX_EVIDENCE_PER_CLAIM]:
            spans = _json_list(r["spans_json"])
            quote = None
            for sp in spans:
                if isinstance(sp, dict) and sp.get("role") == "user" and sp.get("quote"):
                    quote = clip_quote(str(sp["quote"]))
                    break
            if quote is None:
                quote = clip_quote(str(r["text"] or ""))
            evidence.append(
                ClaimEvidence(
                    session_id=str(r["session_id"]),
                    window_id=str(r["window_id"]),
                    message_id=str(r["request_message_id"]),
                    quote=quote,
                    meta={
                        "project": project_label(r["repo"], r["cwd"]),
                        "started_at": r["started_at"],
                        "user_stance": r["user_stance"],
                        "turn_kinds": _json_list(r["turn_kinds_json"]),
                    },
                )
            )
        denom = labeled_sessions or n
        rate = n / denom if denom else None
        claims.append(
            Claim(
                id=_claim_id("recurring_instruction", theme, EXTRACTOR_VERSION),
                kind="recurring_instruction",
                subject=theme,
                predicate="observed_in_labeled_windows",
                value={
                    "theme": theme,
                    "windows": len(windows),
                    "sessions": n,
                    "labeled_sessions_denominator": labeled_sessions,
                    "top_projects": [
                        {"project": p, "windows": c} for p, c in top_projects
                    ],
                    "suggested_instruction": suggestion,
                    "phrasing": (
                        f"among sessions with UX labels, theme {theme} matched "
                        f"{n}/{denom} sessions "
                        f"({(rate or 0):.4f}); windows={len(windows)}"
                    ),
                },
                scope_type="global",
                scope_id="global",
                derivation="llm_derived",
                support_status=support,
                sample_size=n,
                denominator=denom,
                rate=rate,
                observed_at=now,
                extractor_name="recurring_instruction",
                confidence_basis={
                    "method": (
                        "Keyword match over user text + LLM evidence spans on "
                        "windows labeled correction / dont_act_yet / redirect"
                    ),
                    "deterministic": False,
                    "label_basis": label_basis,
                },
                does_not_prove=(
                    "Matching a theme in labeled windows does not prove the "
                    "instruction is missing from config, that the agent "
                    "violated it, or that adding it would reduce corrections. "
                    "Labels are LLM-derived and mostly unadjudicated."
                ),
                evidence=evidence,
                created_at=now,
                updated_at=now,
            )
        )

    # Correction volume claim (descriptive, LLM-derived).
    corr_sessions = {
        str(r["session_id"])
        for r in rows
        if "correction" in json.dumps(_json_list(r["turn_kinds_json"]))
        or r["user_stance"] == "correcting"
    }
    if corr_sessions:
        n = len(corr_sessions)
        support = _support_status(n)
        denom = labeled_sessions or n
        claims.append(
            Claim(
                id=_claim_id("correction_volume", "global", EXTRACTOR_VERSION),
                kind="correction_theme",
                subject="correction",
                predicate="session_rate_among_labeled",
                value={
                    "sessions_with_correction_label": n,
                    "labeled_sessions": labeled_sessions,
                    "phrasing": observational_rate_phrase(
                        "a correction/correcting label", n, denom
                    ),
                },
                scope_type="global",
                scope_id="global",
                derivation="llm_derived",
                support_status=support,
                sample_size=n,
                denominator=denom,
                rate=(n / denom) if denom else None,
                observed_at=now,
                extractor_name="correction_theme",
                confidence_basis={
                    "method": "ux_observations turn_kinds/user_stance",
                    "deterministic": False,
                    "label_basis": label_basis,
                },
                does_not_prove=(
                    "Correction labels are not a quality score and are not "
                    "validated by adjudication at scale."
                ),
                evidence=[
                    ClaimEvidence(session_id=sid)
                    for sid in sorted(corr_sessions)[:MAX_EVIDENCE_PER_CLAIM]
                ],
                created_at=now,
                updated_at=now,
            )
        )
    return claims


def _safe_supersedes_id(claim_id: str, candidate: str | None) -> str | None:
    """Never allow a claim to supersede itself (stable ids make this easy)."""
    if candidate is None or candidate == claim_id:
        return None
    return candidate


def link_supersessions(claims: list[Claim], prior: Iterable[Claim]) -> list[Claim]:
    """Mark prior active claims superseded when subject/predicate collide.

    Claim ids are deterministic hashes of kind/subject/version. When a re-derive
    keeps the same id, that is an in-place update — not a supersession edge.
    Setting supersedes_id = old.id in that case produced self-loops.
    """
    prior_by_key: dict[tuple[str, str, str | None], Claim] = {}
    for c in prior:
        if c.status in {"rejected", "superseded"}:
            continue
        prior_by_key[(c.kind, c.subject, c.scope_id)] = c

    for claim in claims:
        key = (claim.kind, claim.subject, claim.scope_id)
        old = prior_by_key.get(key)
        if old is None:
            claim.supersedes_id = _safe_supersedes_id(claim.id, claim.supersedes_id)
            continue
        if old.id == claim.id:
            # Same stable identity: update in place; keep a prior *other* edge.
            claim.supersedes_id = _safe_supersedes_id(claim.id, old.supersedes_id)
            continue
        if old.value == claim.value and old.sample_size == claim.sample_size:
            claim.id = old.id
            claim.supersedes_id = _safe_supersedes_id(claim.id, old.supersedes_id)
            continue
        claim.supersedes_id = _safe_supersedes_id(claim.id, old.id)
    return claims


def derive_claims(
    conn: sqlite3.Connection,
    *,
    now: str | None = None,
    include_llm_derived: bool = True,
) -> list[Claim]:
    """Derive candidate claims from the current evidence ledger."""
    ts = now or _utc_now()
    claims: list[Claim] = []
    claims.extend(_extract_skill_claims(conn, ts))
    claims.extend(_extract_harness_model_claims(conn, ts))
    claims.extend(_extract_tool_failure_claims(conn, ts))
    source_reader = CachedSourceTranscriptReader()
    if include_llm_derived:
        claims.extend(_extract_theme_claims(conn, ts, source_reader))

    if not source_reader.verify_current():
        raise RuntimeError("canonical source changed during claim extraction")

    prior: list[Claim] = []
    if _table_exists(conn, "claims"):
        from agentlog.analysis.claims.store import list_claims as _list

        prior = _list(conn, status=None, include_evidence=False)
    return link_supersessions(claims, prior)
