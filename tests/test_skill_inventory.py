from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.skills import (
    FRONTMATTER_ABSENT,
    FRONTMATTER_MISSING_NAME,
    FRONTMATTER_OK,
    FRONTMATTER_UNTERMINATED,
    frontmatter_status,
    index_skills,
    index_t3_visibility,
    list_skill_profiles,
    normalize_skill_content,
    skill_inventory_report,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db

SKILL_BODY = "# demo\n\nDo the thing carefully.\n"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(base: Path, folder: str, name: str, body: str = SKILL_BODY) -> Path:
    return _write(
        base / folder / "SKILL.md",
        f"---\nname: {name}\ndescription: demo skill\n---\n{body}",
    )


class FrontmatterStatusTests(unittest.TestCase):
    def test_statuses(self) -> None:
        ok, err = frontmatter_status("---\nname: a\n---\nbody\n")
        self.assertEqual(ok, FRONTMATTER_OK)
        self.assertIsNone(err)

        absent, err = frontmatter_status("# just a heading\n")
        self.assertEqual(absent, FRONTMATTER_ABSENT)
        self.assertTrue(err)

        unterminated, err = frontmatter_status("---\nname: a\ndescription: b\n")
        self.assertEqual(unterminated, FRONTMATTER_UNTERMINATED)
        self.assertTrue(err)

        missing, err = frontmatter_status("---\ndescription: b\n---\nbody\n")
        self.assertEqual(missing, FRONTMATTER_MISSING_NAME)
        self.assertTrue(err)

    def test_normalization_ignores_whitespace_only_differences(self) -> None:
        a = "---\nname: a\n---\nbody\n"
        b = "---\nname: a\n---\r\n\nbody   \n\n"
        self.assertEqual(normalize_skill_content(a), normalize_skill_content(b))


class InventoryReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.conn = connect(self.root / "inv.db")
        init_db(self.conn)
        self.cursor_root = self.root / "cursor"
        self.codex_root = self.root / "codex"
        self.agents_root = self.root / "agents"
        self.missing_root = self.root / "does-not-exist"

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _roots(self) -> list[tuple[str, Path]]:
        return [
            ("cursor", self.cursor_root),
            ("codex", self.codex_root),
            ("agents", self.agents_root),
            ("claude-user", self.missing_root),
        ]

    def _index(self) -> None:
        index_skills(self.conn, self._roots(), now="2026-08-09T10:00:00+00:00")

    def test_exact_duplicate_across_two_roots(self) -> None:
        _skill(self.cursor_root, "playwright", "playwright")
        _skill(self.codex_root, "playwright", "playwright")
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["exact_duplicate_groups"], 1)
        self.assertEqual(report["totals"]["redundant_copies"], 1)
        self.assertEqual(report["totals"]["name_conflicts"], 0)
        group = report["exact_duplicates"][0]
        self.assertEqual(group["kind"], "exact_duplicate")
        self.assertEqual(group["copy_count"], 2)
        self.assertEqual(group["sources"], ["codex", "cursor"])
        self.assertEqual(group["names"], ["playwright"])
        self.assertTrue(group["cross_root"])

    def test_duplicate_within_one_root_is_still_detected(self) -> None:
        _skill(self.cursor_root, "12345/skills/tidy", "tidy")
        _skill(self.cursor_root, "tidy-plugin/skills/tidy", "tidy")
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["exact_duplicate_groups"], 1)
        group = report["exact_duplicates"][0]
        self.assertFalse(group["cross_root"])
        self.assertEqual(group["copy_count"], 2)

    def test_same_name_different_content_is_a_conflict_not_a_duplicate(self) -> None:
        _skill(self.cursor_root, "review", "review", "# review\n\nversion A\n")
        _skill(self.codex_root, "review", "review", "# review\n\nversion B\n")
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["exact_duplicate_groups"], 0)
        self.assertEqual(report["totals"]["name_conflicts"], 1)
        conflict = report["name_conflicts"][0]
        self.assertEqual(conflict["kind"], "name_conflict")
        self.assertEqual(conflict["name"], "review")
        self.assertEqual(conflict["variant_count"], 2)
        self.assertEqual(conflict["copy_count"], 2)
        self.assertEqual(sorted(conflict["sources"]), ["codex", "cursor"])

    def test_whitespace_only_difference_is_a_normalized_duplicate(self) -> None:
        _write(
            self.cursor_root / "tidy" / "SKILL.md",
            "---\nname: tidy\ndescription: d\n---\n# tidy\n\nbody\n",
        )
        _write(
            self.codex_root / "tidy" / "SKILL.md",
            "---\nname: tidy\ndescription: d\n---\n# tidy\n\n\nbody   \n\n",
        )
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["exact_duplicate_groups"], 0)
        self.assertEqual(report["totals"]["normalized_duplicate_groups"], 1)
        # Divergent bytes under one name is still reported as a conflict.
        self.assertEqual(report["totals"]["name_conflicts"], 1)

    def test_malformed_frontmatter_is_surfaced_not_dropped(self) -> None:
        _write(
            self.cursor_root / "broken" / "SKILL.md",
            "---\nname: broken\ndescription: never closed\n",
        )
        _write(
            self.codex_root / "nameless" / "SKILL.md",
            "---\ndescription: has no name\n---\nbody\n",
        )
        stats = index_skills(
            self.conn, self._roots(), now="2026-08-09T10:00:00+00:00"
        )
        self.assertEqual(stats.scanned, 2)
        self.assertEqual(stats.frontmatter_issues, 2)

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["skills_indexed"], 2)
        self.assertEqual(report["totals"]["frontmatter_issues"], 2)
        statuses = {i["name"]: i["frontmatter_status"] for i in report["frontmatter_issues"]}
        self.assertEqual(statuses["broken"], FRONTMATTER_UNTERMINATED)
        self.assertEqual(statuses["nameless"], FRONTMATTER_MISSING_NAME)
        for issue in report["frontmatter_issues"]:
            self.assertTrue(issue["frontmatter_error"])

    def test_no_frontmatter_falls_back_to_directory_name(self) -> None:
        _write(self.agents_root / "bare-skill" / "SKILL.md", "# bare\n\nno frontmatter\n")
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["totals"]["skills_indexed"], 1)
        issue = report["frontmatter_issues"][0]
        self.assertEqual(issue["name"], "bare-skill")
        self.assertEqual(issue["frontmatter_status"], FRONTMATTER_ABSENT)

    def test_missing_root_is_benign_and_reported(self) -> None:
        _skill(self.cursor_root, "solo", "solo")
        self._index()

        report = skill_inventory_report(self.conn, roots=self._roots())
        missing = {m["source"] for m in report["missing_roots"]}
        self.assertIn("claude-user", missing)
        self.assertIn("codex", missing)
        self.assertEqual(report["totals"]["skills_indexed"], 1)
        self.assertEqual(report["totals"]["exact_duplicate_groups"], 0)

    def test_t3_visibility_empty_cache_is_no_data_not_error(self) -> None:
        caches = self.root / "t3caches"
        caches.mkdir()
        _write(
            caches / "codex.json",
            json.dumps({"enabled": True, "installed": True, "status": "ready", "skills": []}),
        )
        _write(
            caches / "claudeAgent.json",
            json.dumps({"enabled": True, "installed": True, "skills": [{"name": "seen-skill"}]}),
        )
        stats = index_t3_visibility(self.conn, caches, now="2026-08-09T10:00:00+00:00")
        self.assertEqual(stats.providers, 2)
        self.assertEqual(stats.skills_seen, 1)
        self.assertEqual(stats.unreadable, [])

        report = skill_inventory_report(self.conn, roots=self._roots())
        vis = report["t3_visibility"]
        self.assertEqual(vis["skills_seen"], 1)
        providers = {p["provider"]: p for p in vis["providers"]}
        self.assertEqual(providers["codex"]["skill_count"], 0)
        self.assertTrue(providers["codex"]["note"])
        self.assertEqual(providers["claudeAgent"]["skill_names"], ["seen-skill"])

    def test_t3_missing_caches_dir_is_benign(self) -> None:
        stats = index_t3_visibility(self.conn, self.root / "no-t3-here")
        self.assertTrue(stats.caches_dir_missing)
        self.assertEqual(stats.providers, 0)
        report = skill_inventory_report(self.conn, roots=self._roots())
        self.assertEqual(report["t3_visibility"]["providers"], [])

    def test_list_profiles_carry_duplicate_markers(self) -> None:
        _skill(self.cursor_root, "playwright", "playwright")
        _skill(self.codex_root, "playwright", "playwright")
        _skill(self.agents_root, "review", "review", "# review\n\nA\n")
        _skill(self.cursor_root, "review", "review", "# review\n\nB\n")
        self._index()

        payload = list_skill_profiles(self.conn)
        self.assertEqual(payload["duplicates"]["exact_duplicate_groups"], 1)
        self.assertEqual(payload["duplicates"]["redundant_copies"], 1)
        self.assertEqual(payload["duplicates"]["name_conflicts"], 1)
        by_name: dict[str, list[dict]] = {}
        for item in payload["items"]:
            by_name.setdefault(str(item["name"]), []).append(item)
        self.assertTrue(all(i["duplicate_copies"] == 1 for i in by_name["playwright"]))
        self.assertTrue(all(i["name_conflict"] for i in by_name["review"]))


class InventoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "api.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        cursor_root = self.root / "cursor"
        codex_root = self.root / "codex"
        _skill(cursor_root, "playwright", "playwright")
        _skill(codex_root, "playwright", "playwright")
        _skill(cursor_root, "review", "review", "# review\n\nA\n")
        _skill(codex_root, "review", "review", "# review\n\nB\n")
        index_skills(
            self.conn,
            [("cursor", cursor_root), ("codex", codex_root)],
            now="2026-08-09T10:00:00+00:00",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_duplicates_endpoint(self) -> None:
        client = TestClient(create_app(self.db_path))
        r = client.get("/api/skills/duplicates")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["totals"]["exact_duplicate_groups"], 1)
        self.assertEqual(body["totals"]["name_conflicts"], 1)
        self.assertEqual(len(body["exact_duplicates"]), 1)
        self.assertIn("t3_visibility", body)

        summary = client.get("/api/skills/duplicates?include_groups=false").json()
        self.assertEqual(summary["exact_duplicates"], [])
        self.assertEqual(summary["totals"]["exact_duplicate_groups"], 1)

    def test_duplicates_route_not_shadowed_by_skill_id_route(self) -> None:
        client = TestClient(create_app(self.db_path))
        self.assertEqual(client.get("/api/skills/duplicates").status_code, 200)
        self.assertEqual(client.get("/api/skills/nope").status_code, 404)

    def test_list_endpoint_includes_duplicate_summary(self) -> None:
        client = TestClient(create_app(self.db_path))
        body = client.get("/api/skills?range=all").json()
        self.assertEqual(body["duplicates"]["exact_duplicate_groups"], 1)
        self.assertEqual(body["duplicates"]["name_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
