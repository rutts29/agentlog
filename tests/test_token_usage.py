from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentlog.api.ranges import parse_range
from agentlog.api import tokens as tokens_api
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


class ClaudeTokenUsageTests(unittest.TestCase):
    def test_parses_message_usage(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "assistant",
                        "timestamp": "2026-08-01T12:00:00Z",
                        "sessionId": "sess-tok-claude",
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-6",
                            "content": [{"type": "text", "text": "hi"}],
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "cache_creation_input_tokens": 5,
                                "cache_read_input_tokens": 40,
                                "service_tier": "standard",
                            },
                        },
                    }
                ),
                _line(
                    {
                        "type": "user",
                        "timestamp": "2026-08-01T12:00:01Z",
                        "sessionId": "sess-tok-claude",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "ok"}],
                        },
                    }
                ),
            ]
        )
        result = ClaudeAdapter().parse_chunk(
            Path("/tmp/sess-tok-claude.jsonl"), data, start_offset=0
        )
        self.assertEqual(len(result.token_usages), 1)
        u = result.token_usages[0]
        self.assertEqual(u.granularity, "message")
        self.assertEqual(u.usage_source, "claude_message_usage")
        self.assertEqual(u.input_tokens, 100)
        self.assertEqual(u.output_tokens, 20)
        self.assertEqual(u.cache_creation_input_tokens, 5)
        self.assertEqual(u.cache_read_input_tokens, 40)
        self.assertIsNone(u.cached_input_tokens)
        self.assertIsNone(u.reasoning_output_tokens)
        self.assertEqual(u.extras.get("service_tier"), "standard")


class CodexTokenUsageTests(unittest.TestCase):
    def test_stores_last_and_cumulative_separately(self) -> None:
        data = b"".join(
            [
                _line(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-08-01T12:00:00Z",
                        "payload": {"id": "sess-tok-codex", "cwd": "/tmp"},
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-08-01T12:00:01Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "cache_write_input_tokens": 0,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 2,
                                    "total_tokens": 110,
                                },
                                "last_token_usage": {
                                    "input_tokens": 100,
                                    "cached_input_tokens": 40,
                                    "cache_write_input_tokens": 0,
                                    "output_tokens": 10,
                                    "reasoning_output_tokens": 2,
                                    "total_tokens": 110,
                                },
                            },
                        },
                    }
                ),
                _line(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-08-01T12:00:02Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 250,
                                    "cached_input_tokens": 90,
                                    "cache_write_input_tokens": 10,
                                    "output_tokens": 25,
                                    "reasoning_output_tokens": 5,
                                    "total_tokens": 275,
                                },
                                "last_token_usage": {
                                    "input_tokens": 150,
                                    "cached_input_tokens": 50,
                                    "cache_write_input_tokens": 10,
                                    "output_tokens": 15,
                                    "reasoning_output_tokens": 3,
                                    "total_tokens": 165,
                                },
                            },
                        },
                    }
                ),
            ]
        )
        result = CodexAdapter().parse_chunk(
            Path("/tmp/rollout-sess-tok-codex.jsonl"), data, start_offset=0
        )
        lasts = [
            u for u in result.token_usages if u.usage_source == "codex_last_token_usage"
        ]
        totals = [
            u
            for u in result.token_usages
            if u.usage_source == "codex_total_token_usage"
        ]
        self.assertEqual(len(lasts), 2)
        self.assertEqual(len(totals), 2)
        self.assertEqual(lasts[0].granularity, "turn")
        self.assertEqual(totals[0].granularity, "session_cumulative")
        self.assertEqual(lasts[1].total_tokens, 165)
        self.assertEqual(totals[1].total_tokens, 275)
        self.assertEqual(lasts[1].reasoning_output_tokens, 3)
        self.assertIsNone(lasts[0].cache_read_input_tokens)

    def test_aggregation_uses_final_cumulative_not_sum_of_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            repo = Repository(conn)
            data = b"".join(
                [
                    _line(
                        {
                            "type": "session_meta",
                            "timestamp": "2026-08-01T12:00:00Z",
                            "payload": {"id": "agg-codex", "cwd": "/tmp"},
                        }
                    ),
                    _line(
                        {
                            "type": "turn_context",
                            "timestamp": "2026-08-01T12:00:00Z",
                            "payload": {"model": "gpt-5"},
                        }
                    ),
                    _line(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-08-01T12:00:01Z",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 100,
                                        "output_tokens": 10,
                                        "cached_input_tokens": 0,
                                        "cache_write_input_tokens": 0,
                                        "reasoning_output_tokens": 0,
                                        "total_tokens": 110,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 100,
                                        "output_tokens": 10,
                                        "cached_input_tokens": 0,
                                        "cache_write_input_tokens": 0,
                                        "reasoning_output_tokens": 0,
                                        "total_tokens": 110,
                                    },
                                },
                            },
                        }
                    ),
                    _line(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-08-01T12:00:02Z",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 250,
                                        "output_tokens": 25,
                                        "cached_input_tokens": 40,
                                        "cache_write_input_tokens": 5,
                                        "reasoning_output_tokens": 3,
                                        "total_tokens": 275,
                                    },
                                    "last_token_usage": {
                                        "input_tokens": 150,
                                        "output_tokens": 15,
                                        "cached_input_tokens": 40,
                                        "cache_write_input_tokens": 5,
                                        "reasoning_output_tokens": 3,
                                        "total_tokens": 165,
                                    },
                                },
                            },
                        }
                    ),
                ]
            )
            path = Path(tmp) / "rollout.jsonl"
            path.write_bytes(data)
            result = CodexAdapter().parse_chunk(path, data, start_offset=0)
            art = repo.upsert_artifact(
                harness="codex",
                path=str(path),
                size=len(data),
                mtime_ns=1,
                content_hash="abc",
                parsed_offset=len(data),
                parser_version="6",
            )
            sid = repo.save_parse_result(
                artifact_id=art, result=result, append=False
            )
            conn.commit()
            tr = parse_range("all")
            summary = tokens_api.corpus_totals(conn, tr)
            # Must equal final cumulative (275), not sum of lasts (275) coincidentally
            # nor sum of cumulatives (110+275=385).
            self.assertEqual(summary["totals"]["total_tokens"], 275)
            self.assertEqual(summary["totals"]["input_tokens"], 250)
            # Summing both cumulative snapshots would wrongly yield 385.
            self.assertNotEqual(summary["totals"]["total_tokens"], 110 + 275)
            detail = tokens_api.session_tokens(conn, sid)
            assert detail is not None
            self.assertEqual(detail["totals"]["total_tokens"], 275)
            self.assertEqual(detail["aggregation"], "final session_cumulative")
            self.assertIsNone(detail["totals"]["cache_read_input_tokens"])
            conn.close()


class TokenMigrationTests(unittest.TestCase):
    def test_migration_creates_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "t.db"
            conn = connect(db)
            init_db(conn)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
            ).fetchone()
            self.assertIsNotNone(row)
            version = conn.execute(
                "SELECT MAX(version) AS v FROM schema_migrations"
            ).fetchone()
            self.assertGreaterEqual(int(version["v"]), 4)
            conn.close()


if __name__ == "__main__":
    unittest.main()
