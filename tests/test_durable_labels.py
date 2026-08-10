from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.analysis.extractors.models import (
    EvidenceSpan,
    ExtractorMeta,
    UxObservation,
)
from agentlog.analysis.extractors.restore_labels import (
    DiskLabel,
    match_all,
    restore_from_run_dir,
    write_restored,
)
from agentlog.analysis.extractors.storage import write_ux_observations, start_ux_run
from agentlog.analysis.windows import build_exchange_windows, compute_window_content_hash
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)


def _seed_session(
    repo: Repository,
    *,
    external_id: str,
    messages: list[NormalizedMessage],
    artifact_path: str = "/tmp/durable.jsonl",
) -> str:
    art_id = repo.upsert_artifact(
        harness="cursor",
        path=artifact_path,
        size=10,
        mtime_ns=1,
        content_hash="abc",
        parsed_offset=10,
        parser_version="test",
    )
    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id=external_id,
            model="grok-4.5",
        ),
        messages=messages,
    )
    sid = repo.save_parse_result(artifact_id=art_id, result=result, append=False)
    windows = build_exchange_windows(repo.list_messages(sid))
    repo.replace_exchange_windows(sid, windows)
    repo.conn.commit()
    return sid


class ContentHashIdentityTests(unittest.TestCase):
    def test_same_content_different_seqs_same_window_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)

            msgs_a = [
                NormalizedMessage(seq=1, role="user", text="fix the bug", content_hash="u"),
                NormalizedMessage(
                    seq=2, role="assistant", text="Done.", content_hash="a"
                ),
            ]
            sid = _seed_session(repo, external_id="s1", messages=msgs_a)
            wid_before = conn.execute(
                "SELECT id, content_hash FROM exchange_windows WHERE session_id = ?",
                (sid,),
            ).fetchone()
            self.assertIsNotNone(wid_before)
            expected = compute_window_content_hash(sid, "fix the bug", "Done.")
            self.assertEqual(wid_before["id"], expected)
            self.assertEqual(wid_before["content_hash"], expected)

            # Re-parse with different seqs / extra plumbing message in between.
            msgs_b = [
                NormalizedMessage(
                    seq=1,
                    role="user",
                    text="system noise",
                    content_hash="p",
                    is_tool_plumbing=True,
                ),
                NormalizedMessage(
                    seq=2, role="user", text="fix the bug", content_hash="u2"
                ),
                NormalizedMessage(
                    seq=3, role="assistant", text="Done.", content_hash="a2"
                ),
            ]
            sid2 = _seed_session(
                repo,
                external_id="s1",
                messages=msgs_b,
                artifact_path="/tmp/durable-b.jsonl",
            )
            self.assertEqual(sid, sid2)
            wid_after = conn.execute(
                "SELECT id, content_hash FROM exchange_windows WHERE session_id = ?",
                (sid,),
            ).fetchone()
            self.assertEqual(wid_after["id"], expected)
            self.assertEqual(wid_after["content_hash"], expected)
            conn.close()


class LabelSurvivalTests(unittest.TestCase):
    def test_labels_survive_replace_exchange_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            msgs = [
                NormalizedMessage(seq=1, role="user", text="hello", content_hash="u"),
                NormalizedMessage(
                    seq=2, role="assistant", text="world", content_hash="a"
                ),
            ]
            sid = _seed_session(repo, external_id="survive", messages=msgs)
            wid = conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
            ).fetchone()["id"]

            run_id = start_ux_run(
                conn, model="m", batch_size=1, window_count=1, gated=True
            )
            write_ux_observations(
                conn,
                run_id,
                [
                    UxObservation(
                        window_id=wid,
                        extractor=ExtractorMeta(
                            name="ux_v1", version="0.1.0", model="m"
                        ),
                        turn_kind=["human_task"],
                        user_stance="neutral",
                        agent_stance="executing",
                        prior_outcome="abstain",
                        spans=[
                            EvidenceSpan(
                                role="user", quote="hello", supports=["human_task"]
                            )
                        ],
                    )
                ],
            )
            conn.execute(
                """
                INSERT INTO adjudications (
                    window_id, adjudicated_at, turn_kind, user_stance, agent_stance,
                    prior_outcome, notes, source, content_hash, link_status
                ) VALUES (?, '2026-08-09T00:00:00+00:00', '["human_task"]',
                          'neutral', 'executing', 'abstain', '', 'ad_hoc', ?, 'linked')
                """,
                (wid, wid),
            )
            conn.commit()

            ux_before = conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()[
                "c"
            ]
            adj_before = conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()[
                "c"
            ]
            self.assertEqual(ux_before, 1)
            self.assertEqual(adj_before, 1)

            # Simulate re-parse: different message ids/seqs, same texts.
            msgs2 = [
                NormalizedMessage(seq=10, role="user", text="hello", content_hash="uX"),
                NormalizedMessage(
                    seq=11, role="assistant", text="world", content_hash="aX"
                ),
            ]
            # Full session rewrite like ingest reparse.
            art_id = repo.upsert_artifact(
                harness="cursor",
                path="/tmp/survive2.jsonl",
                size=11,
                mtime_ns=2,
                content_hash="def",
                parsed_offset=11,
                parser_version="test",
            )
            result = ParseResult(
                session=NormalizedSession(
                    harness=Harness.CURSOR,
                    external_id="survive",
                    model="grok-4.5",
                ),
                messages=msgs2,
            )
            sid2 = repo.save_parse_result(
                artifact_id=art_id, result=result, append=False
            )
            windows = build_exchange_windows(repo.list_messages(sid2))
            repo.replace_exchange_windows(sid2, windows)
            conn.commit()

            ux_after = conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()[
                "c"
            ]
            adj_after = conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()[
                "c"
            ]
            self.assertEqual(ux_after, ux_before)
            self.assertEqual(adj_after, adj_before)
            ux = conn.execute(
                "SELECT window_id, link_status, content_hash FROM ux_observations"
            ).fetchone()
            self.assertEqual(ux["link_status"], "linked")
            self.assertEqual(
                ux["window_id"],
                compute_window_content_hash(sid2, "hello", "world"),
            )
            adj = conn.execute(
                "SELECT window_id, link_status FROM adjudications"
            ).fetchone()
            self.assertEqual(adj["link_status"], "linked")
            conn.close()

    def test_orphan_marking_instead_of_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            msgs = [
                NormalizedMessage(seq=1, role="user", text="keep me", content_hash="u"),
                NormalizedMessage(
                    seq=2, role="assistant", text="ok", content_hash="a"
                ),
            ]
            sid = _seed_session(repo, external_id="orphan", messages=msgs)
            wid = conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
            ).fetchone()["id"]
            run_id = start_ux_run(
                conn, model="m", batch_size=1, window_count=1, gated=True
            )
            write_ux_observations(
                conn,
                run_id,
                [
                    UxObservation(
                        window_id=wid,
                        extractor=ExtractorMeta(
                            name="ux_v1", version="0.1.0", model="m"
                        ),
                        turn_kind=["human_task"],
                    )
                ],
            )
            # Replace with entirely different content → old window removed.
            msgs2 = [
                NormalizedMessage(
                    seq=1, role="user", text="brand new", content_hash="u2"
                ),
                NormalizedMessage(
                    seq=2, role="assistant", text="reply", content_hash="a2"
                ),
            ]
            art_id = repo.get_artifact_by_path("/tmp/durable.jsonl")
            assert art_id is not None
            result = ParseResult(
                session=NormalizedSession(
                    harness=Harness.CURSOR,
                    external_id="orphan",
                    model="grok-4.5",
                ),
                messages=msgs2,
            )
            sid2 = repo.save_parse_result(
                artifact_id=art_id.id, result=result, append=False
            )
            repo.replace_exchange_windows(
                sid2, build_exchange_windows(repo.list_messages(sid2))
            )
            conn.commit()

            row = conn.execute(
                "SELECT link_status, window_id FROM ux_observations"
            ).fetchone()
            self.assertEqual(row["link_status"], "orphaned")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()[
                    "c"
                ],
                1,
            )
            conn.close()


class RestoreMatcherTests(unittest.TestCase):
    def test_restore_matcher_on_synthetic_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            msgs = [
                NormalizedMessage(
                    seq=1,
                    role="user",
                    text="Create a complete implementation plan for only the assigned wave.",
                    content_hash="u",
                ),
                NormalizedMessage(
                    seq=2,
                    role="assistant",
                    text="I'll draft the plan carefully.",
                    content_hash="a",
                ),
            ]
            sid = _seed_session(
                repo,
                external_id="restore-sess",
                messages=msgs,
                artifact_path=str(root / "a.jsonl"),
            )
            wid = conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
            ).fetchone()["id"]

            run_dir = root / "run"
            (run_dir / "packets").mkdir(parents=True)
            (run_dir / "results").mkdir(parents=True)
            old_wid = "deadbeefdeadbeefdeadbeef"
            packet = {
                "packet_id": "pkt_0001",
                "windows": [
                    {
                        "window_id": old_wid,
                        "harness": "cursor",
                        "user": msgs[0].text,
                        "assistant": msgs[1].text,
                    }
                ],
            }
            (run_dir / "packets" / "pkt_0001.json").write_text(
                json.dumps(packet), encoding="utf-8"
            )
            result = {
                "packet_id": "pkt_0001",
                "windows": [
                    {
                        "window_id": old_wid,
                        "turn_kind": ["human_task", "dont_act_yet"],
                        "user_stance": "neutral",
                        "agent_stance": "investigating",
                        "prior_outcome": "abstain",
                        "flags": {},
                        "spans": [
                            {
                                "role": "user",
                                "quote": "Create a complete implementation plan for only the assigned wave.",
                                "supports": ["human_task"],
                            }
                        ],
                        "confidence": {},
                        "abstain_reasons": [],
                        "novel_observations": [],
                    }
                ],
            }
            (run_dir / "results" / "pkt_0001.json").write_text(
                json.dumps(result), encoding="utf-8"
            )

            census = restore_from_run_dir(conn, run_dir, dry_run=False)
            self.assertEqual(census.total_disk, 1)
            self.assertEqual(census.restored_by_content_hash, 1)
            self.assertEqual(census.written, 1)
            self.assertEqual(census.unrestorable, [])
            row = conn.execute(
                "SELECT window_id, link_status, content_hash FROM ux_observations"
            ).fetchone()
            self.assertEqual(row["window_id"], wid)
            self.assertEqual(row["link_status"], "linked")
            conn.close()

    def test_evidence_quote_match_when_text_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            user = "UNIQUE_QUOTE_TOKEN_XYZ " + ("body " * 50)
            asst = "assistant unique reply ZZZ"
            msgs = [
                NormalizedMessage(seq=1, role="user", text=user, content_hash="u"),
                NormalizedMessage(seq=2, role="assistant", text=asst, content_hash="a"),
            ]
            sid = _seed_session(
                repo,
                external_id="quote-sess",
                messages=msgs,
                artifact_path=str(root / "a.jsonl"),
            )
            wid = conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
            ).fetchone()["id"]

            disk = DiskLabel(
                packet_id="pkt_9",
                old_window_id="oldidoldidoldidoldidoldi",
                harness="cursor",
                user_text=user[:40] + "…",
                assistant_text=asst[:20] + "…",
                label={
                    "window_id": "oldidoldidoldidoldidoldi",
                    "spans": [
                        {
                            "role": "user",
                            "quote": "UNIQUE_QUOTE_TOKEN_XYZ",
                            "supports": ["human_task"],
                        }
                    ],
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                    "flags": {},
                    "confidence": {},
                    "abstain_reasons": [],
                    "novel_observations": [],
                },
                source_path="x",
            )
            # Truncated packet text still unique via prefix → content_hash path.
            matched, failed = match_all(conn, [disk])
            self.assertEqual(failed, [])
            self.assertEqual(len(matched), 1)
            self.assertIn(matched[0].method, {"content_hash", "evidence_quote"})
            self.assertEqual(matched[0].new_window_id, wid)

            # Force evidence-only: ambiguous truncated user text across two windows.
            msgs_b = [
                NormalizedMessage(
                    seq=1, role="user", text=user + " EXTRA", content_hash="u2"
                ),
                NormalizedMessage(
                    seq=2, role="assistant", text="other reply", content_hash="a2"
                ),
            ]
            _seed_session(
                repo,
                external_id="quote-sess-b",
                messages=msgs_b,
                artifact_path=str(root / "b.jsonl"),
            )
            disk2 = DiskLabel(
                packet_id="pkt_10",
                old_window_id="oldidoldidoldidoldidold2",
                harness="cursor",
                user_text=user[:40] + "…",
                assistant_text="nope…",
                label={
                    "window_id": "oldidoldidoldidoldidold2",
                    "spans": [
                        {
                            "role": "assistant",
                            "quote": "assistant unique reply ZZZ",
                            "supports": ["executing"],
                        }
                    ],
                    "turn_kind": ["human_task"],
                    "user_stance": "neutral",
                    "agent_stance": "executing",
                    "prior_outcome": "abstain",
                    "flags": {},
                    "confidence": {},
                    "abstain_reasons": [],
                    "novel_observations": [],
                },
                source_path="y",
            )
            matched2, failed2 = match_all(conn, [disk2])
            self.assertEqual(failed2, [])
            self.assertEqual(matched2[0].method, "evidence_quote")
            self.assertEqual(matched2[0].new_window_id, wid)
            write_restored(conn, matched)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()[
                    "c"
                ],
                1,
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
