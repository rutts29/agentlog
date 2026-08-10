"""agentlog is advisory-only: no code path may write a harness config file."""

from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path

from agentlog.config import DEFAULT_DB_PATH
from agentlog.mcp_server import tools as mcp_tools
from agentlog.mcp_server.server import TOOL_NAMES, create_server
from agentlog.safety.write_guard import (
    WriteGuardViolation,
    allowed_roots,
    assert_writable,
    is_harness_config,
    write_text,
)

MUTATING_TOKENS = (
    "write",
    "update",
    "insert",
    "delete",
    "remove",
    "apply",
    "patch",
    "edit",
    "set_",
    "create",
    "mutate",
    "publish",
    "rollback",
)


class HarnessConfigRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _config_paths(self) -> list[Path]:
        return [
            self.root / ".claude" / "CLAUDE.md",
            self.root / "AGENTS.md",
            self.root / ".codex" / "AGENTS.md",
            self.root / "skills" / "my-skill" / "SKILL.md",
            self.root / ".cursor" / "rules" / "house-style.mdc",
            self.root / ".claude" / "hooks.json",
            self.root / ".claude" / "plugins" / "thing" / "plugin.json",
        ]

    def test_harness_config_paths_are_recognized(self) -> None:
        for path in self._config_paths():
            with self.subTest(path=str(path)):
                self.assertTrue(is_harness_config(path))

    def test_assert_writable_refuses_harness_config(self) -> None:
        for path in self._config_paths():
            with self.subTest(path=str(path)):
                with self.assertRaises(WriteGuardViolation):
                    assert_writable(path)

    def test_write_text_refuses_and_creates_nothing(self) -> None:
        target = self.root / ".claude" / "CLAUDE.md"
        with self.assertRaises(WriteGuardViolation):
            write_text(target, "# injected\n")
        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())

    def test_existing_config_file_is_not_overwritten(self) -> None:
        target = self.root / "AGENTS.md"
        target.write_text("# original\n", encoding="utf-8")
        with self.assertRaises(WriteGuardViolation):
            write_text(target, "# replaced\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "# original\n")

    def test_paths_outside_allowed_roots_are_refused(self) -> None:
        outside = Path.home() / "Documents" / "agentlog-should-not-write.txt"
        with self.assertRaises(WriteGuardViolation):
            assert_writable(outside)

    def test_harness_home_dirs_are_refused_even_for_plain_files(self) -> None:
        with self.assertRaises(WriteGuardViolation):
            assert_writable(Path.home() / ".claude" / "notes.txt")


class AllowedRootTests(unittest.TestCase):
    def test_agentlog_data_dir_is_writable(self) -> None:
        target = DEFAULT_DB_PATH.parent / "context" / "probe.md"
        self.assertEqual(assert_writable(target), target.resolve().parent / "probe.md")

    def test_repo_research_dir_is_writable(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        assert_writable(repo / ".research" / "probe" / "out.jsonl")

    def test_temp_dir_is_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = write_text(Path(tmp) / "nested" / "out.txt", "ok\n")
            self.assertEqual(written.read_text(encoding="utf-8"), "ok\n")

    def test_allowed_roots_exclude_home(self) -> None:
        self.assertNotIn(Path.home().resolve(), allowed_roots())


class NoApplyPathTests(unittest.TestCase):
    def test_claims_package_exposes_no_apply(self) -> None:
        import agentlog.analysis.claims as claims

        for name in ("apply_proposal", "dry_run_apply", "rollback_proposal"):
            self.assertFalse(
                hasattr(claims, name), f"{name} must not exist: it wrote config files"
            )

    def test_apply_module_is_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            __import__("agentlog.analysis.claims.apply")


class McpReadOnlyTests(unittest.TestCase):
    def test_no_tool_name_suggests_mutation(self) -> None:
        for name in TOOL_NAMES:
            lowered = name.lower()
            for token in MUTATING_TOKENS:
                self.assertNotIn(
                    token,
                    lowered,
                    f"MCP tool {name} looks mutating; the server must stay read-only",
                )

    def test_registered_tools_match_read_only_allowlist(self) -> None:
        server = create_server()
        listed = asyncio.run(server.list_tools())
        self.assertEqual({t.name for t in listed}, set(TOOL_NAMES))
        for tool in listed:
            self.assertIsNotNone(tool.annotations)
            self.assertTrue(tool.annotations.read_only_hint)

    def test_tools_module_has_no_write_helpers(self) -> None:
        exported = [
            name
            for name, obj in vars(mcp_tools).items()
            if not name.startswith("_") and inspect.isfunction(obj)
            and obj.__module__ == mcp_tools.__name__
        ]
        self.assertTrue(exported)
        for name in exported:
            for token in ("write", "insert", "update", "delete", "apply"):
                self.assertNotIn(token, name.lower())

    def test_tools_source_contains_no_sql_mutations(self) -> None:
        source = Path(mcp_tools.__file__).read_text(encoding="utf-8").upper()
        for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE"):
            self.assertNotIn(statement, source)


if __name__ == "__main__":
    unittest.main()
