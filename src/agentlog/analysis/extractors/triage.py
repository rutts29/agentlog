from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.patterns import (
    classify_request_text,
    unwrap_cursor_user_text,
)
from agentlog.analysis.extractors.taxonomy import MIN_NONEMPTY_CHARS, Route, TurnKind


@dataclass
class TriageResult:
    window_id: str
    harness: str
    request_kind: str
    turn_kinds: list[str]
    route: Route
    matched_rules: list[str] = field(default_factory=list)
    human_text: str = ""


@dataclass
class TriageReport:
    total: int = 0
    by_harness_total: dict[str, int] = field(default_factory=dict)
    by_harness_route: dict[str, dict[str, int]] = field(default_factory=dict)
    rule_hits: Counter[str] = field(default_factory=Counter)
    route_counts: Counter[str] = field(default_factory=Counter)
    request_kind_counts: Counter[str] = field(default_factory=Counter)
    results: list[TriageResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "by_harness_total": dict(sorted(self.by_harness_total.items())),
            "by_harness_route": {
                h: dict(sorted(routes.items()))
                for h, routes in sorted(self.by_harness_route.items())
            },
            "rule_hits": dict(self.rule_hits.most_common()),
            "route_counts": dict(self.route_counts.most_common()),
            "request_kind_counts": dict(self.request_kind_counts.most_common()),
            "ux_eligible": int(self.route_counts.get(Route.UX.value, 0)),
        }


# Structural rules only. Order is evaluation order for logging; route uses priority.
TRIAGE_RULES: tuple[str, ...] = (
    "tool_plumbing",
    "auto_review",
    "task_notification",
    "continue_stub",
    "realtime_delegation",
    "cursor_subagent_followup",
    "skill_body_as_user",
    "worker_brief",
    "empty_string",
)


def _match_rules(ctx: WindowContext, request_kind: str, raw_text: str) -> list[str]:
    matched: list[str] = []
    stripped = (raw_text or "").strip()
    human = unwrap_cursor_user_text(stripped)

    if ctx.is_tool_plumbing or request_kind == "tool_plumbing":
        matched.append("tool_plumbing")
    if request_kind == "auto_review":
        matched.append("auto_review")
    if request_kind == "task_notification":
        matched.append("task_notification")
        if "perform any necessary follow-up actions" in stripped.lower():
            matched.append("cursor_subagent_followup")
    if request_kind == "continue_stub":
        matched.append("continue_stub")
    if request_kind == "realtime_delegation":
        matched.append("realtime_delegation")
    if request_kind == "skill_body":
        matched.append("skill_body_as_user")
    if request_kind == "worker_brief":
        matched.append("worker_brief")
    # Genuine empty human string only. MIN_NONEMPTY_CHARS=1 so "no", "ok",
    # "stop", "yes do it", "no, revert that" all survive.
    if len(human) < MIN_NONEMPTY_CHARS and "tool_plumbing" not in matched:
        matched.append("empty_string")
    return matched


def _route_from_rules(matched: list[str], request_kind: str) -> Route:
    rules = set(matched)
    if "tool_plumbing" in rules:
        return Route.DROP
    if "auto_review" in rules:
        return Route.AUTO_REVIEW
    if rules & {
        "task_notification",
        "continue_stub",
        "realtime_delegation",
        "cursor_subagent_followup",
    }:
        return Route.DROP
    if "skill_body_as_user" in rules:
        return Route.SKILL_COMPLIANCE
    if "worker_brief" in rules:
        return Route.WORKER_TASK
    if "empty_string" in rules:
        return Route.DROP
    if request_kind == "image_only":
        # Segregated modality: not UX attitude labels.
        return Route.DROP
    return Route.UX


def triage_window(ctx: WindowContext) -> TriageResult:
    raw = ctx.request_text or ""
    if ctx.is_tool_plumbing:
        hit_kind = "tool_plumbing"
        turn_kinds = [TurnKind.TOOL_PLUMBING.value]
    else:
        hit = classify_request_text(raw)
        hit_kind = hit.kind
        turn_kinds = list(hit.turn_kinds)

    matched = _match_rules(ctx, hit_kind, raw)
    route = _route_from_rules(matched, hit_kind)
    human = unwrap_cursor_user_text(raw.strip())
    return TriageResult(
        window_id=ctx.window_id,
        harness=ctx.harness,
        request_kind=hit_kind,
        turn_kinds=turn_kinds,
        route=route,
        matched_rules=matched,
        human_text=human,
    )


def triage_windows(contexts: list[WindowContext]) -> TriageReport:
    report = TriageReport()
    for ctx in contexts:
        result = triage_window(ctx)
        report.results.append(result)
        report.total += 1
        report.by_harness_total[result.harness] = (
            report.by_harness_total.get(result.harness, 0) + 1
        )
        route_key = result.route.value
        report.route_counts[route_key] += 1
        report.request_kind_counts[result.request_kind] += 1
        harness_routes = report.by_harness_route.setdefault(result.harness, {})
        harness_routes[route_key] = harness_routes.get(route_key, 0) + 1
        for rule in result.matched_rules:
            report.rule_hits[rule] += 1
        # Also count non-matches implicitly via total - sum(hits) is not
        # meaningful since multi-match; per-rule hits are the audit unit.
    return report
