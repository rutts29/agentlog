"""Redirect/brake lead metric.

Contract: `docs/research/eval-architecture.md` §4.1 (root task cluster is the
analytical unit), §4.5 (numerator subtypes, human-substantive denominator) and
§4.7 (precision gates). Every number here has to name its denominator, so the
metric abstains rather than extrapolating from partial extraction coverage.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from agentlog.analysis.extractors.storage import (
    UX_RUN_KIND,
    published_ux_run_id,
    run_is_publishable,
)
from agentlog.analysis.performance.gates import (
    AggregateCell,
    abstain_cell,
    evaluate_continuous_rate,
    unavailable_cell,
)
from agentlog.api.clusters import resolve_session_roots
from agentlog.api.model_rollup import strict_message_model_sql
from agentlog.api.ranges import TimeRange, session_time_clause

METRIC = "redirects_brakes_per_10_exchange_windows"

# eval-architecture.md §4.5: a window counts once toward the numerator when it
# carries either redirect subtype, or the premature-action process flag.
REDIRECT_TURN_KINDS: frozenset[str] = frozenset({"redirect_or_brake", "dont_act_yet"})
REDIRECT_PROCESS_FLAG = "premature_action_called_out"

# Eligible denominator: deterministically triaged human-supervisor prose only.
# Worker briefs, inter-agent handoffs, auto-reviews, harness stubs, skill dumps
# and image-only turns are explicitly excluded by §4.5. `cursor_wrapped` is a
# mixed human/harness-synthetic population that triage cannot yet separate, so
# it is excluded here too and reported as an uncovered population.
ELIGIBLE_ROUTE = "ux"
ELIGIBLE_REQUEST_KINDS: tuple[str, ...] = ("substantive",)

# Coverage gate mirrors the availability floor in gates.evaluate_continuous_rate.
MIN_COVERAGE = 0.70


def _published_run_or_reason(conn: sqlite3.Connection) -> tuple[str | None, str]:
    """Return the published UX run id, or (None, reason) when aggregation must stop."""
    run_id = published_ux_run_id(conn)
    if run_id is not None:
        return run_id, ""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name = 'published_derivation_runs'"
    ).fetchone():
        return None, "no_published_run"
    row = conn.execute(
        "SELECT run_id FROM published_derivation_runs WHERE kind = ?",
        (UX_RUN_KIND,),
    ).fetchone()
    if row is None:
        return None, "no_published_run"
    _ok, reason = run_is_publishable(conn, str(row["run_id"]))
    return None, reason or "no_published_run"


def _unavailable_message(reason: str) -> str:
    if reason in {"gate_not_validated", "gate_failed"}:
        return (
            "Redirect/brake rate (descriptive steering frequency, not a quality "
            "score) is withheld because the published extraction run has not "
            "passed a real adjudication gate. Synthetic fixture audits and "
            "restore-from-disk labels do not authorize this lead metric."
        )
    return (
        "Redirect/brake rate (descriptive steering frequency, not a quality "
        "score) requires a published semantic extraction run. No completed, "
        "gate-passing run is published, so no rate, sparkline, or delta is "
        "shown. Audit predictions are never aggregated."
    )


def _eligible_clause() -> str:
    kinds = ", ".join(f"'{k}'" for k in ELIGIBLE_REQUEST_KINDS)
    return (
        f"d.route = '{ELIGIBLE_ROUTE}' AND d.request_kind IN ({kinds})"
    )


def _kind_hit_sql() -> str:
    kinds = ", ".join(f"'{k}'" for k in sorted(REDIRECT_TURN_KINDS))
    return (
        "EXISTS (SELECT 1 FROM json_each(u.turn_kinds_json) tk "
        f"WHERE tk.value IN ({kinds}))"
    )


def _has_link_status(conn: sqlite3.Connection) -> bool:
    return "link_status" in {
        str(r[1]) for r in conn.execute("PRAGMA table_info(ux_observations)")
    }


def eligible_windows(
    conn: sqlite3.Connection, tr: TimeRange, *, model: str | None = None
) -> list[sqlite3.Row]:
    where, params = session_time_clause(tr)
    model_clause = ""
    if model is not None:
        params = {**params, "model": model}
        model_clause = (
            "AND "
            f"{strict_message_model_sql(message_alias='response')} = :model"
        )
    return conn.execute(
        f"""
        SELECT w.id AS window_id, w.session_id AS session_id
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        JOIN messages response ON response.id = w.response_message_id
        JOIN window_det_classifications d ON d.window_id = w.id
        WHERE {where} AND {_eligible_clause()} {model_clause}
        """,
        params,
    ).fetchall()


def observed_windows(
    conn: sqlite3.Connection,
    tr: TimeRange,
    run_id: str,
    *,
    model: str | None = None,
) -> list[sqlite3.Row]:
    where, params = session_time_clause(tr)
    params = {**params, "run_id": run_id}
    model_clause = ""
    if model is not None:
        params = {**params, "model": model}
        model_clause = (
            "AND "
            f"{strict_message_model_sql(message_alias='response')} = :model"
        )
    link_clause = "AND u.link_status = 'linked'" if _has_link_status(conn) else ""
    return conn.execute(
        f"""
        SELECT
            u.window_id AS window_id,
            w.session_id AS session_id,
            ({_kind_hit_sql()}) AS kind_hit,
            COALESCE(
                json_extract(u.flags_json, '$.{REDIRECT_PROCESS_FLAG}'), 0
            ) AS flag_hit
        FROM ux_observations u
        JOIN exchange_windows w ON w.id = u.window_id
        JOIN sessions s ON s.id = w.session_id
        JOIN messages response ON response.id = w.response_message_id
        JOIN window_det_classifications d ON d.window_id = u.window_id
        WHERE u.run_id = :run_id
          AND {where}
          AND {_eligible_clause()}
          {link_clause}
          {model_clause}
        """,
        params,
    ).fetchall()


def _coverage_payload(observed: int, eligible: int, hits: int = 0) -> dict[str, Any]:
    return {
        "redirect_windows": hits,
        "observed_eligible_windows": observed,
        "eligible_windows": eligible,
        "ratio": (observed / eligible) if eligible else 0.0,
        "gate": MIN_COVERAGE,
        "denominator": (
            "deterministically triaged human-supervisor substantive exchange "
            "windows in range"
        ),
    }


def redirect_cell(
    conn: sqlite3.Connection,
    tr: TimeRange,
    *,
    model: str | None = None,
    extra_flags: list[str] | None = None,
) -> AggregateCell:
    """Redirect/brake rate per 10 eligible windows, clustered by root task."""
    scope = "this model" if model is not None else "this range"
    run_id, publish_block = _published_run_or_reason(conn)
    if run_id is None:
        return unavailable_cell(
            metric=METRIC,
            kind="continuous",
            message=_unavailable_message(publish_block),
            flags=["source_capability"],
            session_ids=_recent_root_ids(conn, tr),
        )

    eligible = eligible_windows(conn, tr, model=model)
    eligible_ids = {str(r["window_id"]) for r in eligible}
    if not eligible_ids:
        return unavailable_cell(
            metric=METRIC,
            kind="continuous",
            message=(
                "No human-supervisor substantive exchange windows in "
                f"{scope}, so the redirect/brake denominator is empty."
            ),
            flags=["source_capability"],
        )

    observed = observed_windows(conn, tr, run_id, model=model)
    roots = resolve_session_roots(conn)
    by_root: dict[str, list[bool]] = defaultdict(list)
    seen: set[str] = set()
    hits = 0
    for row in observed:
        window_id = str(row["window_id"])
        if window_id in seen:
            continue
        seen.add(window_id)
        session_id = str(row["session_id"])
        hit = bool(row["kind_hit"]) or bool(row["flag_hit"])
        hits += int(hit)
        by_root[roots.get(session_id, session_id)].append(hit)

    coverage = _coverage_payload(len(seen), len(eligible_ids), hits)
    ratio = float(coverage["ratio"])
    flags = list(extra_flags or [])

    if not seen:
        cell = unavailable_cell(
            metric=METRIC,
            kind="continuous",
            message=(
                "The published extraction run observed none of the "
                f"{len(eligible_ids)} eligible human-substantive windows in "
                f"{scope}. No rate is shown."
            ),
            flags=[*flags, "source_capability"],
        )
        cell.coverage = coverage
        return cell

    session_ids = sorted(by_root)
    if ratio < MIN_COVERAGE:
        cell = abstain_cell(
            metric=METRIC,
            kind="continuous",
            reason="coverage_below_gate",
            message=(
                "Insufficient coverage to aggregate: the published extraction run "
                f"labels {len(seen)} of {len(eligible_ids)} eligible "
                f"human-substantive windows ({ratio:.0%}), below the "
                f"{MIN_COVERAGE:.0%} gate. Sessions are listed; no rate is shown "
                "because the unlabeled remainder cannot be assumed to behave like "
                "the labeled part."
            ),
            n_clusters=len(by_root),
            n_events=hits,
            availability=ratio,
            flags=[*flags, "outcome_missingness"],
            session_ids=session_ids,
        )
        cell.coverage = coverage
        return cell

    rates = [10.0 * sum(v) / len(v) for _root, v in sorted(by_root.items())]
    cell = evaluate_continuous_rate(
        metric=METRIC,
        per_cluster_values=rates,
        session_ids=session_ids,
        availability=ratio,
        extra_flags=flags,
    )
    cell.coverage = coverage
    return cell


def _recent_root_ids(conn: sqlite3.Connection, tr: TimeRange) -> list[str]:
    where, params = session_time_clause(tr)
    rows = conn.execute(
        f"""
        SELECT s.id AS id FROM sessions s
        WHERE {where}
        ORDER BY COALESCE(s.started_at, '') DESC
        LIMIT 200
        """,
        params,
    ).fetchall()
    if not rows:
        return []
    roots = resolve_session_roots(conn)
    out: list[str] = []
    for row in rows:
        root = roots.get(str(row["id"]), str(row["id"]))
        if root not in out:
            out.append(root)
        if len(out) >= 40:
            break
    return out
