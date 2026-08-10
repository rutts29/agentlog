from __future__ import annotations

import json
import unittest
from pathlib import Path

from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter
from agentlog.normalize.models import NormalizedMessage
from agentlog.normalize.synthetic import (
    classify_synthetic_user_text,
    skill_exposure_from_synthetic_message,
)


def _line(payload: dict) -> bytes:
    return (json.dumps(payload) + "\n").encode()


class SyntheticNormalizationTests(unittest.TestCase):
    def test_only_anchored_known_envelopes_are_non_owner(self) -> None:
        cases = {
            "<subagent_notification>finished</subagent_notification>": (True, False),
            "<task-notification>finished</task-notification>": (True, False),
            "<task_notification>finished</task_notification>": (True, False),
            "<teammate-message>status</teammate-message>": (True, False),
            "<system-reminder>context</system-reminder>": (False, True),
            "<local-command-stdout>output</local-command-stdout>": (False, True),
            "<skill name=\"review\"># review</skill>": (False, True),
            "<recommended_plugins>plugins</recommended_plugins>\n</environment_context>": (True, False),
            "<image>": (False, True),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                flags = classify_synthetic_user_text(text)
                self.assertEqual((flags.authored_by_agent, flags.is_tool_plumbing), expected)

    def test_user_content_wrappers_and_mixed_images_are_preserved(self) -> None:
        values = (
            "continue from where you left off",
            "<in-app-browser-context>state</in-app-browser-context>\n## My request for Codex:\nFix it",
            "<open_subagent_context>context</open_subagent_context>\n<timestamp>now</timestamp>\n<user_query>Fix it</user_query>",
            "<image>\nPlease inspect the screenshot",
            "<skill name=\"review\"># review</skill>\n<user_query>Fix it</user_query>",
            "Please use this context: <task-notification>finished</task-notification>",
        )
        for text in values:
            with self.subTest(text=text):
                self.assertEqual(classify_synthetic_user_text(text).authored_by_agent, False)
                self.assertEqual(classify_synthetic_user_text(text).is_tool_plumbing, False)

    def test_skill_exposure_requires_deterministic_name(self) -> None:
        named = NormalizedMessage(
            seq=3,
            role="user",
            text='<skill name="review"># review</skill>',
            content_hash="named",
        )
        unnamed = named.model_copy(update={"text": "<skill>instructions</skill>"})
        exposure = skill_exposure_from_synthetic_message(named)
        assert exposure is not None
        self.assertEqual((exposure.message_seq, exposure.skill_name), (3, "review"))
        self.assertIsNone(skill_exposure_from_synthetic_message(unnamed))

        element_named = named.model_copy(
            update={"text": "<skill><name>codex-claude-communication</name><body>rules</body></skill>"}
        )
        element_exposure = skill_exposure_from_synthetic_message(element_named)
        assert element_exposure is not None
        self.assertEqual(
            element_exposure.skill_name, "codex-claude-communication"
        )

    def test_codex_claude_and_cursor_raw_records_share_flags(self) -> None:
        codex = CodexAdapter().parse_chunk(
            Path("/tmp/session.jsonl"),
            _line({"type": "response_item", "payload": {"type": "message", "role": "user", "content": "<subagent_notification>done</subagent_notification>"}}),
            start_offset=0,
        )
        claude = ClaudeAdapter().parse_chunk(
            Path("/tmp/session.jsonl"),
            _line({"type": "user", "message": {"role": "user", "content": "<system-reminder>context</system-reminder>"}}),
            start_offset=0,
        )
        cursor = CursorAdapter().parse_chunk(
            Path("/tmp/agent-transcripts/root/root.jsonl"),
            _line({"role": "user", "message": {"role": "user", "content": [{"type": "text", "text": "<skill name=\"review\"># review</skill>"}]}}),
            start_offset=0,
        )
        self.assertTrue(codex.messages[0].authored_by_agent)
        self.assertTrue(claude.messages[0].is_tool_plumbing)
        self.assertTrue(cursor.messages[0].is_tool_plumbing)
        self.assertEqual(cursor.skill_exposures[0].skill_name, "review")

    def test_codex_stores_request_after_browser_and_image_wrappers(self) -> None:
        def parse(text: str):
            return CodexAdapter().parse_chunk(
                Path("/tmp/session.jsonl"),
                _line(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": text,
                        },
                    }
                ),
                start_offset=0,
            ).messages[0]

        browser = parse(
            "<in-app-browser-context>browser state</in-app-browser-context>\n"
            "## My request for Codex:\nFix the parser"
        )
        image_with_text = parse("<image>\nInspect the error screenshot")
        image_only = parse("<image>")
        self.assertEqual(browser.text, "Fix the parser")
        self.assertFalse(browser.authored_by_agent)
        self.assertEqual(image_with_text.text, "Inspect the error screenshot")
        self.assertFalse(image_with_text.is_tool_plumbing)
        self.assertEqual(image_only.text, "<image>")
        self.assertTrue(image_only.is_tool_plumbing)

    def test_codex_strips_live_image_block_shapes(self) -> None:
        def parse(text: str):
            return CodexAdapter().parse_chunk(
                Path("/tmp/session.jsonl"),
                _line(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": text,
                        },
                    }
                ),
                start_offset=0,
            ).messages[0]

        paired = parse(
            "<image>\n/private/tmp/capture.png\n</image>\n"
            "Explain the failing panel"
        )
        self_closing = parse(
            '<image src="/tmp/capture.png"/>\nCompare both states'
        )
        placeholder = parse(
            "<image>\n/tmp/capture.png\n[Image #1]\n</image>"
        )
        unclosed_path = parse(
            "<image>\n/private/tmp/capture.webp\nInspect this regression"
        )
        self.assertEqual(paired.text, "Explain the failing panel")
        self.assertEqual(self_closing.text, "Compare both states")
        self.assertEqual(unclosed_path.text, "Inspect this regression")
        self.assertEqual(placeholder.text, "<image>")
        self.assertTrue(placeholder.is_tool_plumbing)
