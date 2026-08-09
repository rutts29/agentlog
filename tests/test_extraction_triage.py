from __future__ import annotations

import unittest

from agentlog.analysis.extractors.models import WindowContext
from agentlog.analysis.extractors.taxonomy import Route
from agentlog.analysis.extractors.triage import triage_window, triage_windows


def _ctx(
    text: str,
    *,
    window_id: str = "w1",
    harness: str = "cursor",
    plumbing: bool = False,
) -> WindowContext:
    return WindowContext(
        window_id=window_id,
        session_id="s1",
        harness=harness,
        request_text=text,
        is_tool_plumbing=plumbing,
    )


class TriageRuleTests(unittest.TestCase):
    def test_short_high_signal_human_survives(self) -> None:
        for text in (
            "no, revert that",
            "yes do it",
            "stop",
            "not like that",
            "ok",
            "no",
        ):
            with self.subTest(text=text):
                result = triage_window(_ctx(text))
                self.assertEqual(result.route, Route.UX, msg=text)
                self.assertNotIn("empty_string", result.matched_rules)

    def test_tool_plumbing_does_not_enter_ux(self) -> None:
        result = triage_window(_ctx("", plumbing=True))
        self.assertEqual(result.route, Route.DROP)
        self.assertIn("tool_plumbing", result.matched_rules)

    def test_tool_plumbing_with_empty_text_not_length_filtered(self) -> None:
        """Plumbing is structural; length floor must not be the reason."""
        result = triage_window(_ctx("", plumbing=True))
        self.assertIn("tool_plumbing", result.matched_rules)
        self.assertNotIn("empty_string", result.matched_rules)

    def test_auto_review_routes_separately(self) -> None:
        text = (
            "The following is the Codex agent history added since your last "
            "approval assessment. Please review."
        )
        result = triage_window(_ctx(text, harness="codex"))
        self.assertEqual(result.route, Route.AUTO_REVIEW)
        self.assertIn("auto_review", result.matched_rules)

    def test_continue_stub_dropped(self) -> None:
        result = triage_window(_ctx("Continue from where you left off."))
        self.assertEqual(result.route, Route.DROP)
        self.assertIn("continue_stub", result.matched_rules)

    def test_task_notification_dropped(self) -> None:
        result = triage_window(
            _ctx("<task-notification><summary>Agent finished</summary></task-notification>")
        )
        self.assertEqual(result.route, Route.DROP)
        self.assertIn("task_notification", result.matched_rules)

    def test_worker_brief_routed(self) -> None:
        text = (
            "You are Phase3 Sandbox Core Worker.\n"
            "Owned files: src/foo.py\n"
            "Finish with STATUS when done."
        )
        result = triage_window(_ctx(text, harness="codex"))
        self.assertEqual(result.route, Route.WORKER_TASK)

    def test_skill_body_routed(self) -> None:
        text = "# Update Config Skill\n\n" + ("x" * 25_000)
        result = triage_window(_ctx(text, harness="claude"))
        self.assertEqual(result.route, Route.SKILL_COMPLIANCE)
        self.assertIn("skill_body_as_user", result.matched_rules)

    def test_genuine_empty_dropped_structurally(self) -> None:
        result = triage_window(_ctx("   ", plumbing=False))
        self.assertEqual(result.route, Route.DROP)
        self.assertIn("empty_string", result.matched_rules)

    def test_per_rule_hits_auditable(self) -> None:
        contexts = [
            _ctx("no, revert that", window_id="a"),
            _ctx("", window_id="b", plumbing=True),
            _ctx("Continue from where you left off.", window_id="c"),
            _ctx(
                "The following is the Codex agent history added since your last "
                "approval assessment.",
                window_id="d",
                harness="codex",
            ),
        ]
        report = triage_windows(contexts)
        self.assertEqual(report.total, 4)
        self.assertEqual(report.route_counts["ux"], 1)
        self.assertGreaterEqual(report.rule_hits["tool_plumbing"], 1)
        self.assertGreaterEqual(report.rule_hits["continue_stub"], 1)
        self.assertGreaterEqual(report.rule_hits["auto_review"], 1)
        self.assertIn("tool_plumbing", report.to_dict()["rule_hits"])


if __name__ == "__main__":
    unittest.main()
