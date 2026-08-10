from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agentlog.analysis.extractors.models import (
    EvidenceSpan,
    ExtractorMeta,
    UxObservation,
)
from agentlog.analysis.extractors.storage import start_ux_run, write_ux_observations
from agentlog.analysis.windows import build_exchange_windows
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.cursor import (
    CursorAdapter,
    canonical_external_id,
    prefer_repo,
)
from agentlog.ingest.cursor_merge import merge_cursor_duplicates
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
    ToolEvent,
)


def _ts() -> datetime:
    return datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _seed(
    repo: Repository,
    *,
    external_id: str,
    repo_slug: str | None,
    messages: list[NormalizedMessage],
    tools: list[ToolEvent] | None = None,
    path: str,
    parent: str | None = None,
) -> str:
    art = repo.upsert_artifact(
        harness="cursor",
        path=path,
        size=10,
        mtime_ns=1,
        content_hash=path,
        parsed_offset=10,
        parser_version="test",
    )
    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id=external_id,
            parent_session_id=parent,
            repo=repo_slug,
            cwd=f"/{repo_slug}" if repo_slug else None,
            started_at=_ts(),
        ),
        messages=messages,
        tool_events=tools or [],
    )
    sid = repo.save_parse_result(artifact_id=art, result=result, append=False)
    windows = build_exchange_windows(repo.list_messages(sid))
    repo.replace_exchange_windows(sid, windows)
    repo.conn.commit()
    return sid


class CanonicalIdUnitTests(unittest.TestCase):
    def test_canonical_strips_path_and_subagent(self) -> None:
        uid = "be6ee399-8665-4f22-8fdd-50ff020c71d8"
        self.assertEqual(canonical_external_id(f"empty-window/{uid}"), uid)
        self.assertEqual(
            canonical_external_id(f"Users-demo-Plugin/{uid}"), uid
        )
        self.assertEqual(
            canonical_external_id(f"Users-demo-Plugin/subagent:{uid}"), uid
        )
        self.assertEqual(canonical_external_id(uid), uid)

    def test_prefer_repo_skips_empty_window(self) -> None:
        self.assertEqual(
            prefer_repo("empty-window", "Users-demo-Plugin"),
            "Users-demo-Plugin",
        )
        self.assertEqual(
            prefer_repo("Users-demo-Plugin", "empty-window"),
            "Users-demo-Plugin",
        )
        self.assertIsNone(prefer_repo(None, "empty-window"))
        self.assertEqual(
            prefer_repo("Users-demo-A", "Users-demo-B"),
            "Users-demo-A",
        )

    def test_adapter_id_ignores_workspace_path(self) -> None:
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Mimic CURSOR_PROJECTS_DIR layout via direct path stems.
            p1 = (
                root
                / "empty-window"
                / "agent-transcripts"
                / uid
                / f"{uid}.jsonl"
            )
            p2 = (
                root
                / "Users-demo-Plugin"
                / "agent-transcripts"
                / uid
                / f"{uid}.jsonl"
            )
            p3 = (
                root
                / "Users-demo-Plugin"
                / "agent-transcripts"
                / "parent-uuid"
                / "subagents"
                / f"{uid}.jsonl"
            )
            for p in (p1, p2, p3):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(
                    json.dumps(
                        {
                            "role": "user",
                            "message": {
                                "content": [{"type": "text", "text": "hi"}]
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

            adapter = CursorAdapter()
            ids = set()
            for p in (p1, p2, p3):
                data = p.read_bytes()
                result = adapter.parse_chunk(p, data, start_offset=0)
                ids.add(result.session.external_id)
                if p == p3:
                    self.assertEqual(result.session.parent_session_id, "parent-uuid")
                if "empty-window" in str(p):
                    self.assertIsNone(result.session.repo)
            self.assertEqual(ids, {uid})


class MergePreservesRicherCopyTests(unittest.TestCase):
    def test_merge_keeps_richer_messages_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            # Migration v013 may already have run on empty DB — fine.
            repo = Repository(conn)
            uid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

            rich_msgs = [
                NormalizedMessage(seq=1, role="user", text="do the thing", content_hash="u1"),
                NormalizedMessage(
                    seq=2, role="assistant", text="done richly", content_hash="a1"
                ),
                NormalizedMessage(seq=3, role="user", text="more", content_hash="u2"),
                NormalizedMessage(
                    seq=4, role="assistant", text="ok", content_hash="a2"
                ),
            ]
            poor_msgs = rich_msgs[:2]
            rich_tools = [
                ToolEvent(seq=1, message_seq=2, tool_name="Shell", action="call"),
                ToolEvent(seq=2, message_seq=2, tool_name="Shell", action="result"),
            ]

            rich_id = _seed(
                repo,
                external_id=f"Users-demo-Plugin/{uid}",
                repo_slug="Users-demo-Plugin",
                messages=rich_msgs,
                tools=rich_tools,
                path=f"/tmp/rich-{uid}.jsonl",
            )
            poor_id = _seed(
                repo,
                external_id=f"empty-window/{uid}",
                repo_slug="empty-window",
                messages=poor_msgs,
                path=f"/tmp/poor-{uid}.jsonl",
            )
            self.assertNotEqual(rich_id, poor_id)

            # Label on the richer copy's window.
            rich_win = conn.execute(
                "SELECT id FROM exchange_windows WHERE session_id = ? LIMIT 1",
                (rich_id,),
            ).fetchone()
            self.assertIsNotNone(rich_win)
            wid = str(rich_win["id"])
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
                                role="user",
                                quote="do the thing",
                                supports=["human_task"],
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

            # Also seed a path-only single session that should rename.
            other = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            _seed(
                repo,
                external_id=f"Users-demo-Other/{other}",
                repo_slug="Users-demo-Other",
                messages=[
                    NormalizedMessage(seq=1, role="user", text="solo", content_hash="s"),
                    NormalizedMessage(
                        seq=2, role="assistant", text="yep", content_hash="y"
                    ),
                ],
                path=f"/tmp/solo-{other}.jsonl",
            )

            stats = merge_cursor_duplicates(conn)
            conn.commit()

            self.assertEqual(stats.groups_merged, 1)
            self.assertEqual(stats.sessions_deleted, 1)

            canon_id = f"cursor:{uid}"
            row = conn.execute(
                "SELECT id, external_id, repo FROM sessions WHERE external_id = ?",
                (uid,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["id"], canon_id)
            self.assertEqual(row["repo"], "Users-demo-Plugin")

            msg_n = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (canon_id,),
            ).fetchone()["c"]
            tool_n = conn.execute(
                "SELECT COUNT(*) AS c FROM tool_events WHERE session_id = ?",
                (canon_id,),
            ).fetchone()["c"]
            self.assertEqual(msg_n, 4)
            self.assertEqual(tool_n, 2)

            # No leftover duplicates.
            dups = conn.execute(
                """
                SELECT COUNT(*) AS c FROM sessions
                WHERE harness = 'cursor' AND (
                    external_id LIKE '%/' || ? OR external_id LIKE '%subagent:' || ?
                )
                """,
                (uid, uid),
            ).fetchone()["c"]
            self.assertEqual(dups, 0)

            ux_after = conn.execute("SELECT COUNT(*) AS c FROM ux_observations").fetchone()[
                "c"
            ]
            adj_after = conn.execute("SELECT COUNT(*) AS c FROM adjudications").fetchone()[
                "c"
            ]
            self.assertEqual(ux_after, ux_before)
            self.assertEqual(adj_after, adj_before)
            linked = conn.execute(
                """
                SELECT link_status FROM ux_observations
                WHERE window_id IN (SELECT id FROM exchange_windows)
                """
            ).fetchone()
            self.assertIsNotNone(linked)
            self.assertEqual(linked["link_status"], "linked")

            # Solo session renamed to bare UUID.
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM sessions WHERE id = ?",
                    (f"cursor:{other}",),
                ).fetchone()
            )
            conn.close()

    def test_poorer_reparse_does_not_clobber_richer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            uid = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            rich = [
                NormalizedMessage(seq=1, role="user", text="a", content_hash="a"),
                NormalizedMessage(seq=2, role="assistant", text="b", content_hash="b"),
                NormalizedMessage(seq=3, role="user", text="c", content_hash="c"),
            ]
            poor = rich[:2]
            _seed(
                repo,
                external_id=uid,
                repo_slug="Users-demo-Plugin",
                messages=rich,
                path=f"/tmp/r-{uid}.jsonl",
            )
            art = repo.upsert_artifact(
                harness="cursor",
                path=f"/tmp/p-{uid}.jsonl",
                size=5,
                mtime_ns=2,
                content_hash="poor",
                parsed_offset=5,
                parser_version="test",
            )
            result = ParseResult(
                session=NormalizedSession(
                    harness=Harness.CURSOR,
                    external_id=uid,
                    repo=None,
                ),
                messages=poor,
            )
            sid = repo.save_parse_result(artifact_id=art, result=result, append=False)
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?",
                (sid,),
            ).fetchone()["c"]
            self.assertEqual(n, 3)
            repo_slug = conn.execute(
                "SELECT repo FROM sessions WHERE id = ?", (sid,)
            ).fetchone()["repo"]
            self.assertEqual(repo_slug, "Users-demo-Plugin")
            conn.close()


if __name__ == "__main__":
    unittest.main()
