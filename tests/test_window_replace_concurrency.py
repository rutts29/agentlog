from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agentlog.analysis.windows import build_exchange_windows
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.normalize.models import (
    Harness,
    NormalizedMessage,
    NormalizedSession,
    ParseResult,
)

SESSIONS = 4
TURNS_PER_SESSION = 6
REPARSE_ROUNDS = 60


def _messages(session_idx: int, turns: int) -> list[NormalizedMessage]:
    msgs: list[NormalizedMessage] = []
    for t in range(turns):
        seq = 2 * t + 1
        msgs.append(
            NormalizedMessage(
                seq=seq,
                role="user",
                text=f"s{session_idx} question {t}",
                content_hash=f"u{session_idx}-{t}",
            )
        )
        msgs.append(
            NormalizedMessage(
                seq=seq + 1,
                role="assistant",
                text=f"s{session_idx} answer {t}",
                content_hash=f"a{session_idx}-{t}",
            )
        )
    return msgs


def _reparse(repo: Repository, session_idx: int, *, turns: int) -> str:
    art = repo.upsert_artifact(
        harness="cursor",
        path=f"/tmp/conc-{session_idx}.jsonl",
        size=10,
        mtime_ns=1,
        content_hash=f"art{session_idx}",
        parsed_offset=10,
        parser_version="test",
    )
    result = ParseResult(
        session=NormalizedSession(
            harness=Harness.CURSOR,
            external_id=f"conc-{session_idx}",
            model="grok-4.5",
        ),
        messages=_messages(session_idx, turns),
    )
    sid = repo.save_parse_result(artifact_id=art, result=result, append=False)
    repo.replace_exchange_windows(sid, build_exchange_windows(repo.list_messages(sid)))
    repo.conn.commit()
    return sid


class ReplaceExchangeWindowsIdempotencyTests(unittest.TestCase):
    def test_reparse_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            sid = _reparse(repo, 0, turns=TURNS_PER_SESSION)
            first = {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
                )
            }
            self.assertEqual(len(first), TURNS_PER_SESSION)
            for _ in range(3):
                _reparse(repo, 0, turns=TURNS_PER_SESSION)
            again = {
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM exchange_windows WHERE session_id = ?", (sid,)
                )
            }
            self.assertEqual(first, again)
            conn.close()

    def test_new_window_id_on_same_message_pair(self) -> None:
        """Edited turn text yields a new id for an unchanged message pair.

        The row PK is content-derived but message ids are seq-derived, so the
        superseded row still occupies UNIQUE(session_id, request_message_id,
        response_message_id) when the replacement is inserted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            art = repo.upsert_artifact(
                harness="cursor",
                path="/tmp/edit.jsonl",
                size=10,
                mtime_ns=1,
                content_hash="e",
                parsed_offset=10,
                parser_version="test",
            )
            result = ParseResult(
                session=NormalizedSession(
                    harness=Harness.CURSOR, external_id="edit", model="m"
                ),
                messages=[
                    NormalizedMessage(seq=1, role="user", text="q", content_hash="u"),
                    NormalizedMessage(
                        seq=2, role="assistant", text="a", content_hash="a"
                    ),
                ],
            )
            sid = repo.save_parse_result(artifact_id=art, result=result, append=False)
            req, resp = f"{sid}:m:1", f"{sid}:m:2"

            repo.replace_exchange_windows(sid, [(req, resp, "ih", "old", "old")])
            conn.commit()
            repo.replace_exchange_windows(sid, [(req, resp, "ih", "new", "new")])
            conn.commit()

            rows = conn.execute(
                "SELECT id, request_message_id FROM exchange_windows"
            ).fetchall()
            self.assertEqual([(r["id"], r["request_message_id"]) for r in rows],
                             [("new", req)])
            conn.close()


class ConcurrentAdjudicationDurabilityTests(unittest.TestCase):
    def test_adjudications_survive_concurrent_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            for i in range(SESSIONS):
                _reparse(repo, i, turns=TURNS_PER_SESSION)
            conn.close()

            errors: list[BaseException] = []
            written: set[str] = set()
            written_lock = threading.Lock()
            stop = threading.Event()
            started = threading.Barrier(2)
            # Handshake so a fast reparser cannot finish before the adjudicator
            # seeds labels, and so we prove commits span reparse progress.
            labels_seeded = threading.Event()
            saw_reparse_progress = threading.Event()
            passes = {"reparse": 0}
            reparse_marks: list[int] = []
            seed_target = max(4, SESSIONS)

            def reparser() -> None:
                worker = connect(db)
                try:
                    r = Repository(worker)
                    started.wait(timeout=30)
                    if not labels_seeded.wait(timeout=30):
                        raise TimeoutError("adjudicator never seeded labels")
                    for _ in range(REPARSE_ROUNDS):
                        for i in range(SESSIONS):
                            _reparse(r, i, turns=TURNS_PER_SESSION)
                        passes["reparse"] += 1
                    if not saw_reparse_progress.wait(timeout=30):
                        raise TimeoutError(
                            "adjudicator never committed across a reparse pass"
                        )
                except BaseException as exc:  # noqa: BLE001 - surfaced below
                    errors.append(exc)
                finally:
                    stop.set()
                    worker.close()

            def adjudicator() -> None:
                worker = connect(db)
                try:
                    started.wait(timeout=30)
                    while not (stop.is_set() and saw_reparse_progress.is_set()):
                        rows = worker.execute(
                            "SELECT id, content_hash FROM exchange_windows"
                        ).fetchall()
                        if not rows:
                            time.sleep(0.001)
                            continue
                        for row in rows:
                            worker.execute(
                                """
                                INSERT INTO adjudications (
                                    window_id, adjudicated_at, turn_kind, user_stance,
                                    agent_stance, prior_outcome, notes, source,
                                    content_hash, link_status
                                ) VALUES (?, '2026-08-09T00:00:00+00:00', '["human_task"]',
                                          'neutral', 'executing', 'abstain', '',
                                          'ad_hoc', ?, 'linked')
                                ON CONFLICT(window_id) DO NOTHING
                                """,
                                (row["id"], row["content_hash"]),
                            )
                            worker.commit()
                            mark = passes["reparse"]
                            reparse_marks.append(mark)
                            with written_lock:
                                written.add(str(row["content_hash"]))
                                seeded = len(written) >= seed_target
                            if seeded:
                                labels_seeded.set()
                            if mark > 0:
                                saw_reparse_progress.set()
                            # Every previously committed label must still be
                            # visible while the other writer churns windows.
                            live = {
                                str(r["content_hash"])
                                for r in worker.execute(
                                    "SELECT content_hash FROM adjudications"
                                )
                            }
                            with written_lock:
                                missing = written - live
                            if missing:
                                raise AssertionError(
                                    f"adjudications vanished mid-run: {sorted(missing)}"
                                )
                            if stop.is_set() and saw_reparse_progress.is_set():
                                break
                            time.sleep(0.001)
                except BaseException as exc:  # noqa: BLE001 - surfaced below
                    errors.append(exc)
                finally:
                    worker.close()

            threads = [
                threading.Thread(target=reparser),
                threading.Thread(target=adjudicator),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
            self.assertFalse([t for t in threads if t.is_alive()], "worker hung")
            self.assertEqual(errors, [], f"worker raised: {errors}")
            self.assertGreaterEqual(len(reparse_marks), 2)
            self.assertGreater(
                reparse_marks[-1] - reparse_marks[0],
                0,
                "writers did not actually interleave",
            )
            self.assertTrue(
                any(m == 0 for m in reparse_marks)
                and any(m > 0 for m in reparse_marks),
                "commits must span pre- and post-reparse epochs",
            )

            check = connect(db)
            stored = check.execute(
                "SELECT content_hash, link_status FROM adjudications"
            ).fetchall()
            self.assertTrue(written, "adjudicator never wrote a row")
            self.assertEqual(
                {str(r["content_hash"]) for r in stored},
                written,
                "adjudication rows were lost across concurrent re-parse",
            )
            self.assertEqual(
                [r for r in stored if r["link_status"] != "linked"],
                [],
                "adjudications were silently orphaned by an idempotent re-parse",
            )
            dangling = check.execute(
                """
                SELECT COUNT(*) AS c FROM adjudications
                WHERE window_id NOT IN (SELECT id FROM exchange_windows)
                """
            ).fetchone()["c"]
            self.assertEqual(dangling, 0)
            check.close()


class WindowlessSessionTests(unittest.TestCase):
    def test_user_turn_without_assistant_reply_yields_no_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            art = repo.upsert_artifact(
                harness="cursor",
                path="/tmp/unanswered.jsonl",
                size=10,
                mtime_ns=1,
                content_hash="c",
                parsed_offset=10,
                parser_version="test",
            )
            result = ParseResult(
                session=NormalizedSession(
                    harness=Harness.CURSOR, external_id="unanswered", model="m"
                ),
                messages=[
                    NormalizedMessage(seq=1, role="user", text="q1", content_hash="u1"),
                    NormalizedMessage(seq=2, role="user", text="q2", content_hash="u2"),
                    NormalizedMessage(
                        seq=3,
                        role="assistant",
                        text="",
                        content_hash="a1",
                        is_tool_plumbing=True,
                    ),
                ],
            )
            sid = repo.save_parse_result(artifact_id=art, result=result, append=False)
            windows = build_exchange_windows(repo.list_messages(sid))
            self.assertEqual(windows, [])
            repo.replace_exchange_windows(sid, windows)
            conn.commit()
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM exchange_windows WHERE session_id = ?",
                    (sid,),
                ).fetchone()["c"],
                0,
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
