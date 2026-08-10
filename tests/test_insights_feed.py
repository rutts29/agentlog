from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.claims.models import Claim, Proposal
from agentlog.analysis.claims.store import upsert_claims, upsert_proposals
from agentlog.analysis.insights import import_session_fact_packet
from agentlog.api.queries import insights_feed
from agentlog.api.ranges import parse_range
from agentlog.db.schema import connect, init_db


def _claim(**kwargs) -> Claim:
    base = dict(
        id="c1",
        kind="recurring_instruction",
        subject="scope_narrow",
        predicate="observed_in_labeled_windows",
        value={
            "theme": "scope_narrow",
            "phrasing": "theme scope_narrow matched 11/374 sessions",
            "suggested_instruction": "- Prefer the smallest change.",
        },
        scope_type="global",
        scope_id="global",
        derivation="llm_derived",
        status="candidate",
        support_status="ok",
        sample_size=11,
        observed_at="2026-08-09T12:00:00+00:00",
        does_not_prove="Does not prove the instruction is missing.",
        created_at="2026-08-09T12:00:00+00:00",
        updated_at="2026-08-09T12:00:00+00:00",
    )
    base.update(kwargs)
    return Claim(**base)


def _proposal(**kwargs) -> Proposal:
    base = dict(
        id="p1",
        title="Add standing minimal-scope rule",
        action="add",
        status="pending",
        target_path="~/AGENTS.md",
        target_kind="agents_md",
        scope_type="global",
        scope_id="global",
        base_content_hash=None,
        unified_diff="--- a\n+++ b\n+bullet\n",
        proposed_content="- Prefer the smallest change.",
        rationale="Among labeled sessions, scope_narrow matched 11/374.",
        derivation_summary="llm instruction proposal",
        does_not_prove="Does not prove the edit would help.",
        sample_size=11,
        claim_ids=[],
        created_at="2026-08-09T13:00:00+00:00",
        updated_at="2026-08-09T13:00:00+00:00",
        model="cursor-grok-4.5-high-fast",
        run_id="proposals_test",
    )
    base.update(kwargs)
    return Proposal(**base)


class InsightsFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "insights.db"
        self.conn = connect(self.path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_empty_feed_keeps_empty_payload(self) -> None:
        tr = parse_range("all")
        body = insights_feed(self.conn, tr)
        self.assertEqual(body["items"], [])
        self.assertIn("empty", body)
        self.assertTrue(body["empty"]["title"])

    def test_feed_orders_coach_then_ok_facts(self) -> None:
        upsert_claims(
            self.conn,
            [
                _claim(
                    id="c_ok",
                    subject="scope_narrow",
                    sample_size=11,
                    support_status="ok",
                ),
                _claim(
                    id="c_insuf",
                    kind="harness_model_usage",
                    subject="demo",
                    predicate="usage_mix",
                    value={"phrasing": "project demo had 7 sessions"},
                    support_status="insufficient",
                    sample_size=7,
                    derivation="deterministic",
                ),
                _claim(
                    id="c_abstain",
                    subject="spawn_workers",
                    support_status="abstain",
                    sample_size=1,
                ),
                _claim(
                    id="c_unused",
                    kind="skill_unused",
                    subject="rare-skill",
                    predicate="zero_exposures",
                    value={"phrasing": "unused"},
                    support_status="ok",
                    sample_size=80,
                    derivation="deterministic",
                ),
                _claim(
                    id="c_corr",
                    kind="correction_theme",
                    subject="correction",
                    predicate="rate",
                    value={
                        "phrasing": "correction label rate 0.12 (n=60)",
                    },
                    support_status="ok",
                    sample_size=60,
                    derivation="llm_derived",
                    does_not_prove="Not a quality score.",
                ),
            ],
        )
        upsert_proposals(self.conn, [_proposal()])
        self.conn.commit()

        body = insights_feed(self.conn, parse_range("all"))
        kinds = [i["kind"] for i in body["items"]]
        sources = [i["source"] for i in body["items"]]
        titles = [i["title"] for i in body["items"]]

        self.assertEqual(sources[0], "proposal")
        self.assertEqual(kinds[0], "coach")
        self.assertIn("Add standing minimal-scope rule", titles[0])

        # Abstain recurring + skill_unused must stay off the feed.
        claim_ids = {
            i["source_id"] for i in body["items"] if i["source"] == "claim"
        }
        self.assertIn("c_ok", claim_ids)
        self.assertIn("c_insuf", claim_ids)
        self.assertIn("c_corr", claim_ids)
        self.assertNotIn("c_abstain", claim_ids)
        self.assertNotIn("c_unused", claim_ids)

        # Among facts: ok before insufficient (correction ok n=60, scope ok n=11, then usage insuf).
        fact_cards = [i for i in body["items"] if i["source"] == "claim"]
        self.assertEqual(fact_cards[0]["confidence"], "ok")
        self.assertGreaterEqual(
            fact_cards[0]["sample_size"], fact_cards[1]["sample_size"]
        )
        self.assertTrue(any(i["kind"] == "usage" for i in fact_cards))
        coach = body["items"][0]
        self.assertEqual(coach["href"], "/proposals")
        self.assertTrue(coach["does_not_prove"])

    def test_imported_session_fact_requires_verbatim_database_evidence(self) -> None:
        session_id = "cursor:project/session"
        self.conn.execute(
            """
            INSERT INTO sessions (id, harness, external_id, started_at, repo)
            VALUES (?, 'cursor', 'project/session', '2026-08-09T12:00:00+00:00', 'demo')
            """,
            (session_id,),
        )
        self.conn.execute(
            """
            INSERT INTO messages (id, session_id, seq, role, timestamp, text)
            VALUES ('m1', ?, 1, 'assistant', '2026-08-09T12:01:00+00:00', ?)
            """,
            (session_id, "Ran the requested verification before reporting done."),
        )
        packet = Path(self._tmp.name) / "facts.json"
        packet.write_text(
            json.dumps(
                {
                    "run_id": "facts-001",
                    "source": "session_llm_facts",
                    "items": [
                        {
                            "session_id": session_id,
                            "message_seq": 1,
                            "kind": "follow",
                            "title": "Verified before reporting completion",
                            "body": "The agent ran the requested verification first.",
                            "quote": "requested verification before reporting done",
                            "does_not_prove": "That every task was verified.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        stats = import_session_fact_packet(
            self.conn, packet, model="cursor-grok-4.5-high-fast"
        )
        self.conn.commit()

        self.assertEqual(stats["claims"], 1)
        body = insights_feed(self.conn, parse_range("all"))
        card = body["items"][0]
        self.assertEqual(card["source"], "claim")
        self.assertEqual(card["origin"], "session")
        self.assertEqual(card["title"], "Verified before reporting completion")
        self.assertEqual(card["href"], "/sessions/cursor%3Aproject%2Fsession")

        packet_payload = json.loads(packet.read_text(encoding="utf-8"))
        packet_payload["items"][0]["quote"] = "not present"
        packet.write_text(json.dumps(packet_payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence quote not found"):
            import_session_fact_packet(
                self.conn, packet, model="cursor-grok-4.5-high-fast"
            )


if __name__ == "__main__":
    unittest.main()
