from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agentlog.analysis.claims.extract import derive_claims, link_supersessions
from agentlog.analysis.claims.models import (
    MIN_SESSIONS_FINDING,
    MIN_SESSIONS_FLOOR,
    Claim,
)
from agentlog.analysis.claims.proposals import (
    generate_proposals,
    refresh_learnings,
    unified_diff,
)
from agentlog.analysis.claims.scope import (
    discover_config_inventory,
    instruction_already_present,
)
from agentlog.analysis.claims.store import (
    list_decision_events,
    list_proposals,
    set_proposal_status,
    target_state,
)
from agentlog.api.app import create_app
from agentlog.db.schema import connect, init_db


def _seed_session(
    conn,
    *,
    sid: str,
    harness: str = "codex",
    model: str = "gpt-5.5",
    repo: str | None = "https://github.com/example/demo.git",
    cwd: str | None = "/tmp/demo",
    parent: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sessions (
            id, harness, external_id, parent_session_id, repo, cwd,
            model, model_canonical, started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sid,
            harness,
            sid.split(":", 1)[-1],
            parent,
            repo,
            cwd,
            model,
            model,
            "2026-08-01T10:00:00+00:00",
            "2026-08-01T11:00:00+00:00",
        ),
    )


def _seed_window_with_label(
    conn,
    *,
    sid: str,
    wid: str,
    text: str,
    turn_kinds: list[str],
    user_stance: str = "correcting",
) -> None:
    mid_req = f"{wid}:req"
    mid_resp = f"{wid}:resp"
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash, authored_by_agent)
        VALUES (?, ?, 1, 'user', ?, 'h1', 0)
        """,
        (mid_req, sid, text),
    )
    conn.execute(
        """
        INSERT INTO messages (id, session_id, seq, role, text, content_hash)
        VALUES (?, ?, 2, 'assistant', 'ok', 'h2')
        """,
        (mid_resp, sid),
    )
    conn.execute(
        """
        INSERT INTO exchange_windows
            (id, session_id, request_message_id, response_message_id,
             input_hash, content_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (wid, sid, mid_req, mid_resp, wid, wid),
    )
    conn.execute(
        """
        INSERT INTO derivation_runs
            (id, kind, extractor_name, extractor_version, started_at, status)
        VALUES (?, 'ux', 'test', '1', '2026-08-01T10:00:00+00:00', 'completed')
        """,
        (f"run-{wid}",),
    )
    conn.execute(
        """
        INSERT INTO ux_observations (
            id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
            prior_outcome, flags_json, spans_json, confidence_json,
            abstain_reasons_json, novel_observations_json, extractor_name,
            extractor_version, model, prompt_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, 'executing', 'abstain', '{}', ?, '{}', '[]', '[]',
                  'ux', '1', 'test', 'p', '2026-08-01T10:00:00+00:00')
        """,
        (
            f"ux-{wid}",
            wid,
            f"run-{wid}",
            json.dumps(turn_kinds),
            user_stance,
            json.dumps(
                [
                    {
                        "role": "user",
                        "quote": text[:120],
                        "supports": turn_kinds,
                    }
                ]
            ),
        ),
    )


class ClaimDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "t.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        # Ensure migration 16 applied
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("claims", tables)
        self.assertIn("proposals", tables)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_skill_exposure_and_unused_deterministic(self) -> None:
        for i in range(12):
            _seed_session(self.conn, sid=f"codex:s{i}")
        skill_path = self.root / "skills" / "my-skill" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: my-skill\ndescription: x\n---\n# my-skill\n",
            encoding="utf-8",
        )
        unused_path = self.root / "skills" / "unused-skill" / "SKILL.md"
        unused_path.parent.mkdir(parents=True)
        unused_path.write_text(
            "---\nname: unused-skill\ndescription: y\n---\n# unused\n",
            encoding="utf-8",
        )
        self.conn.execute(
            """
            INSERT INTO skills (
                id, name, source, source_path, description, current_content_hash,
                first_seen_at, last_seen_at, last_indexed_at
            ) VALUES
            ('sk1', 'my-skill', 'agents', ?, 'x', 'h1', 't', 't', 't'),
            ('sk2', 'unused-skill', 'agents', ?, 'y', 'h2', 't', 't', 't')
            """,
            (str(skill_path), str(unused_path)),
        )
        for i in range(6):
            self.conn.execute(
                """
                INSERT INTO skill_exposures (id, session_id, message_id, skill_name, exposure_type)
                VALUES (?, ?, NULL, 'my-skill', 'invoked')
                """,
                (f"exp{i}", f"codex:s{i}"),
            )
        self.conn.commit()

        claims = derive_claims(self.conn, include_llm_derived=False)
        kinds = {c.kind for c in claims}
        self.assertIn("skill_exposure", kinds)
        self.assertIn("skill_unused", kinds)
        unused = next(c for c in claims if c.kind == "skill_unused")
        self.assertEqual(unused.derivation, "deterministic")
        self.assertEqual(unused.value["exposure_count"], 0)
        self.assertEqual(unused.support_status, "abstain")
        self.assertEqual(
            unused.value.get("abstain_reason"), "exposure coverage insufficient"
        )
        exposed = next(c for c in claims if c.subject == "my-skill")
        self.assertEqual(exposed.sample_size, 6)
        self.assertEqual(exposed.derivation, "deterministic")
        props = generate_proposals(self.conn, claims=claims, home=self.root)
        self.assertFalse(any(p.action == "archive_skill" for p in props))

    def test_sample_size_gating(self) -> None:
        for i in range(3):
            _seed_session(self.conn, sid=f"codex:s{i}")
            _seed_window_with_label(
                self.conn,
                sid=f"codex:s{i}",
                wid=f"w{i}",
                text="don't act yet, just plan first",
                turn_kinds=["dont_act_yet", "correction"],
            )
        self.conn.commit()
        claims = derive_claims(self.conn, include_llm_derived=True)
        theme = next(c for c in claims if c.kind == "recurring_instruction")
        self.assertEqual(theme.support_status, "abstain")
        self.assertLess(theme.sample_size, MIN_SESSIONS_FLOOR)

        for i in range(3, 8):
            _seed_session(self.conn, sid=f"codex:s{i}")
            _seed_window_with_label(
                self.conn,
                sid=f"codex:s{i}",
                wid=f"w{i}",
                text="hold on, don't act yet — plan first",
                turn_kinds=["dont_act_yet"],
            )
        self.conn.commit()
        claims2 = derive_claims(self.conn, include_llm_derived=True)
        theme2 = next(c for c in claims2 if c.subject == "dont_act_yet_brake")
        self.assertEqual(theme2.support_status, "insufficient")
        self.assertGreaterEqual(theme2.sample_size, MIN_SESSIONS_FLOOR)
        self.assertLess(theme2.sample_size, MIN_SESSIONS_FINDING)

    def test_supersession_links(self) -> None:
        prior = Claim(
            id="old1",
            kind="skill_exposure",
            subject="x",
            predicate="session_exposure_rate",
            value={"exposure_count": 1},
            scope_type="skill",
            scope_id="sk",
            derivation="deterministic",
            sample_size=1,
        )
        newer = Claim(
            id="new1",
            kind="skill_exposure",
            subject="x",
            predicate="session_exposure_rate",
            value={"exposure_count": 5},
            scope_type="skill",
            scope_id="sk",
            derivation="deterministic",
            sample_size=5,
        )
        linked = link_supersessions([newer], [prior])
        self.assertEqual(linked[0].supersedes_id, "old1")


class ScopeAndDiffTests(unittest.TestCase):
    def test_scope_dedupe_against_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            agents = home / "AGENTS.md"
            agents.write_text(
                "# Rules\n\n## Verification\n\nverify it actually works before done.\n",
                encoding="utf-8",
            )
            inv = discover_config_inventory(home)
            hits = instruction_already_present(inv, "verify_before_done")
            self.assertTrue(hits)

    def test_unified_diff_generation(self) -> None:
        diff = unified_diff(
            path="/tmp/AGENTS.md",
            old="# A\n",
            new="# A\n\n## Wait\n\n- stop\n",
        )
        self.assertIn("@@", diff)
        self.assertIn("+## Wait", diff)
        self.assertIn("--- a/tmp/AGENTS.md", diff)
        self.assertNotIn("--- a//", diff)


class ProposalStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.agents = self.home / "AGENTS.md"
        self.agents.write_text("# Global\n\nKeep responses short.\n", encoding="utf-8")
        self.db_path = self.root / "t.db"
        self.conn = connect(self.db_path)
        init_db(self.conn)
        for i in range(12):
            _seed_session(
                self.conn,
                sid=f"codex:s{i}",
                cwd=str(self.root / "demo"),
                repo="demo",
            )
            _seed_window_with_label(
                self.conn,
                sid=f"codex:s{i}",
                wid=f"w{i}",
                text="don't act yet — plan first, do not edit",
                turn_kinds=["dont_act_yet", "correction"],
            )
        skill_path = self.home / ".agents" / "skills" / "dusty" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            "---\nname: dusty\ndescription: unused\n---\n# dusty\n",
            encoding="utf-8",
        )
        self.conn.execute(
            """
            INSERT INTO skills (
                id, name, source, source_path, description, current_content_hash,
                first_seen_at, last_seen_at, last_indexed_at
            ) VALUES ('skd', 'dusty', 'agents', ?, 'unused', 'h', 't', 't', 't')
            """,
            (str(skill_path),),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _insert_llm_proposal(self) -> str:
        """Board source is LLM packets; seed one pending card for decision tests."""
        from agentlog.analysis.claims.models import Proposal
        from agentlog.analysis.claims.store import upsert_proposals

        old = self.agents.read_text(encoding="utf-8")
        new = old + "\n## Wait for explicit go-ahead\n\n- Do not edit until go-ahead.\n"
        prop = Proposal(
            id="testllmproposalfixture0001",
            title="Add wait-for-go-ahead instruction",
            action="add",
            status="pending",
            target_path=str(self.agents),
            target_kind="agents_md",
            scope_type="global",
            scope_id="global",
            base_content_hash=None,
            unified_diff="--- a\n+++ b\n+## Wait\n",
            proposed_content=new,
            rationale="fixture LLM proposal",
            derivation_summary="LLM packet proposal (cursor_subagent_packet/fixture)",
            does_not_prove="fixture",
            sample_size=11,
            claim_ids=[],
            created_at="2026-08-01T00:00:00+00:00",
            updated_at="2026-08-01T00:00:00+00:00",
            provenance={"provider": "cursor_subagent_packet", "model": "fixture"},
            run_id="proposals_fixture",
            model="cursor-grok-4.5-high-fast",
            prompt_hash="fixture",
            evidence_pack_hash="fixture",
        )
        upsert_proposals(self.conn, [prop])
        self.conn.commit()
        return prop.id

    def test_decisions_never_touch_the_target_file(self) -> None:
        before = self.agents.read_text(encoding="utf-8")
        stats = refresh_learnings(self.conn, home=self.home)
        self.conn.commit()
        self.assertGreater(stats["claims_total"], 0)
        self.assertEqual(stats["proposals_total"], 0)

        pid = self._insert_llm_proposal()
        props = list_proposals(self.conn, status="pending")
        self.assertTrue(props)
        self.assertEqual(self.agents.read_text(encoding="utf-8"), before)

        target = next(p for p in props if p.id == pid)
        for decision in ("accepted", "deferred", "rejected", "pending"):
            prop = set_proposal_status(self.conn, target.id, decision, note=decision)
            self.conn.commit()
            self.assertEqual(prop.status, decision)
            self.assertEqual(self.agents.read_text(encoding="utf-8"), before)

        accepted = set_proposal_status(self.conn, target.id, "accepted", note="by hand")
        self.conn.commit()
        self.assertIsNotNone(accepted.decided_at)
        self.assertEqual(accepted.decision_note, "by hand")

        events = list_decision_events(self.conn, target.id)
        self.assertGreaterEqual(len(events), 5)
        self.assertEqual(events[-1]["decision"], "accepted")

        with self.assertRaises(ValueError):
            set_proposal_status(self.conn, target.id, "applied")

    def test_target_state_reports_manual_application(self) -> None:
        self._insert_llm_proposal()
        prop = next(
            p
            for p in list_proposals(self.conn, status="pending")
            if p.proposed_content
        )
        state = target_state(prop)
        self.assertFalse(state["matches_proposed"])

        target = Path(prop.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prop.proposed_content or "", encoding="utf-8")
        applied_state = target_state(prop)
        self.assertTrue(applied_state["matches_proposed"])

    def test_api_exposes_decisions_and_no_apply_route(self) -> None:
        self._insert_llm_proposal()
        client = TestClient(create_app(self.db_path))
        r = client.get("/api/proposals")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["count"], 1)
        self.assertTrue(body["advisory_only"])
        self.assertIn("counts_by_status", body)

        pid = body["items"][0]["id"]
        detail = client.get(f"/api/proposals/{pid}")
        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        for key in ("unified_diff", "rationale", "support", "target_state", "claims"):
            self.assertIn(key, payload)

        before = self.agents.read_text(encoding="utf-8")
        ar = client.post(
            f"/api/proposals/{pid}/decision",
            json={"decision": "accepted", "note": "applied by hand"},
        )
        self.assertEqual(ar.status_code, 200)
        self.assertEqual(ar.json()["status"], "accepted")
        self.assertEqual(self.agents.read_text(encoding="utf-8"), before)

        bad = client.post(
            f"/api/proposals/{pid}/decision", json={"decision": "applied"}
        )
        self.assertEqual(bad.status_code, 422)

        routes = {
            getattr(route, "path", "") for route in create_app(self.db_path).routes
        }
        self.assertNotIn("/api/proposals/{proposal_id}/apply", routes)
        self.assertNotIn("/api/proposals/{proposal_id}/rollback", routes)
        self.assertIn(
            client.post(f"/api/proposals/{pid}/apply").status_code, (404, 405)
        )

        claims = client.get("/api/claims?status=candidate")
        self.assertEqual(claims.status_code, 200)


if __name__ == "__main__":
    unittest.main()
