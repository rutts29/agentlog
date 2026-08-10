"""Privacy and trust-boundary regressions: LLM egress (H7) and the server (H3)."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentlog.analysis.extractors.llm_client import XAIChatClient  # noqa: E402
from agentlog.analysis.extractors.models import WindowContext  # noqa: E402
from agentlog.analysis.extractors.ux_extractor import (  # noqa: E402
    UxExtractor,
    build_user_message,
)
from agentlog.analysis.extractors.window_context import truncate_for_ux  # noqa: E402
from agentlog.api.app import create_app  # noqa: E402
from agentlog.api.security import (  # noqa: E402
    BindPolicyViolation,
    SecurityConfig,
    generate_token,
    is_loopback_host,
    resolve_bind,
)
from agentlog.db.schema import connect, init_db  # noqa: E402
from agentlog.safety.egress import (  # noqa: E402
    ACKNOWLEDGEMENT,
    EgressBlocked,
    disable_remote_extraction,
    remote_extraction,
)
from agentlog.safety.redaction import (  # noqa: E402
    REDACTION_VERSION,
    redact_text,
)

CANARY_KEY = "sk-canary1234567890abcdefghijklmnopqrstuvwxyz"
CANARY_AWS = "AKIAIOSFODNN7EXAMPLE"
CANARY_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
)
CANARY_EMAIL = "private.person@client-company.example"
CANARY_ASSIGNMENT = 'DATABASE_PASSWORD="hunter2-super-secret"'


def _canary_context() -> WindowContext:
    return WindowContext(
        window_id="w1",
        session_id="s1",
        harness="codex",
        model="gpt-5",
        request_text=(
            f"deploy with {CANARY_KEY} and {CANARY_AWS}\n"
            f"{CANARY_ASSIGNMENT}\n"
            f"token {CANARY_JWT}\n"
            f"mail {CANARY_EMAIL}\n"
            "see /Users/someoneelse/private/client.py"
        ),
        assistant_text=f"I used {CANARY_KEY} from /Users/someoneelse/.aws/credentials",
        next_user_text=f"rotate {CANARY_AWS} please",
        tool_timeline=[f"Bash|export TOKEN={CANARY_KEY}|1"],
        skill_names=["skill-at-/Users/someoneelse/x"],
        assistant_msg_count=1,
        tool_count=1,
        request_message_id="m1",
        response_message_id="m2",
    )


ALL_CANARIES = [
    CANARY_KEY,
    CANARY_AWS,
    CANARY_JWT,
    CANARY_EMAIL,
    "hunter2-super-secret",
    "someoneelse",
]


class RedactionTests(unittest.TestCase):
    def test_canary_secrets_absent_from_constructed_payload(self):
        payload = truncate_for_ux(_canary_context())
        blob = json.dumps(payload, ensure_ascii=False)
        for canary in ALL_CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, blob)

    def test_canary_secrets_absent_from_outbound_request_body(self):
        payload = truncate_for_ux(_canary_context())
        body = XAIChatClient().build_request_body(
            system="sys", user=build_user_message([payload]), model="grok-4.5"
        )
        blob = json.dumps(body, ensure_ascii=False)
        for canary in ALL_CANARIES:
            with self.subTest(canary=canary):
                self.assertNotIn(canary, blob)

    def test_payload_records_redaction_version(self):
        payload = truncate_for_ux(_canary_context())
        self.assertEqual(payload["redaction_version"], REDACTION_VERSION)

    def test_owner_home_becomes_tilde(self):
        home = str(Path.home())
        self.assertNotIn(home, redact_text(f"open {home}/notes/secret.md"))

    def test_redaction_runs_before_truncation(self):
        # A secret past the 4000-char cap must still be masked, not merely cut.
        ctx = WindowContext(
            window_id="w2",
            session_id="s1",
            harness="codex",
            request_text=("x" * 5000) + CANARY_KEY,
        )
        payload = truncate_for_ux(ctx)
        self.assertNotIn(CANARY_KEY, json.dumps(payload))

    def test_ordinary_text_survives(self):
        text = "please refactor parse_window() in windows.py and add a test"
        self.assertEqual(redact_text(text), text)

    def test_token_counts_are_not_treated_as_credentials(self):
        # This corpus is largely token accounting; masking counts would destroy
        # the analysis input while protecting nothing.
        for text in (
            "max_output_tokens=32000",
            '"total_tokens": 18422',
            "omitted_approx_tokens = 1200",
            "cacheReadInputTokens: 4096",
            "original_token_count=51",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_text(text), text)

    def test_credential_assignments_still_masked(self):
        for text in (
            'ANTHROPIC_API_KEY="abcd1234efgh5678"',
            "adminPassword = 'hunter2hunter2'",
            'discord_bot_token: "MTIzNDU2Nzg5MDEyMzQ1Njc4"',
        ):
            with self.subTest(text=text):
                self.assertNotEqual(redact_text(text), text)

    def test_report_counts_redactions(self):
        from agentlog.safety.redaction import RedactionReport

        rep = RedactionReport()
        redact_text(f"{CANARY_KEY} and {CANARY_AWS}", rep)
        self.assertGreaterEqual(rep.total, 2)
        self.assertEqual(rep.to_dict()["redaction_version"], REDACTION_VERSION)


class _ExplodingOpener:
    """Any real socket attempt during these tests is itself a failure."""

    def __init__(self, testcase):
        self.testcase = testcase

    def __call__(self, *args, **kwargs):
        raise AssertionError("network egress attempted during tests")


class EgressFailClosedTests(unittest.TestCase):
    def setUp(self):
        disable_remote_extraction()
        patcher = mock.patch.object(
            urllib.request, "urlopen", _ExplodingOpener(self)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(disable_remote_extraction)

    def test_client_refuses_without_optin_even_with_api_key(self):
        client = XAIChatClient(api_key="present-but-irrelevant")
        with self.assertRaises(EgressBlocked):
            client.complete_json(system="s", user="u", model="grok-4.5")

    def test_default_extractor_path_fails_closed(self):
        extractor = UxExtractor()
        with self.assertRaises(EgressBlocked):
            extractor.extract_many([_canary_context()])

    def test_boolean_flag_cannot_open_the_gate(self):
        from agentlog.safety.egress import enable_remote_extraction

        for bad in ("true", "1", "yes", ""):
            with self.subTest(ack=bad):
                with self.assertRaises(EgressBlocked):
                    enable_remote_extraction(
                        endpoint="https://api.x.ai/v1", acknowledgement=bad
                    )

    def test_optin_is_scoped_to_the_granted_host(self):
        with remote_extraction(
            endpoint="https://api.x.ai/v1", acknowledgement=ACKNOWLEDGEMENT
        ):
            other = XAIChatClient(
                api_key="k", base_url="https://evil.example/v1"
            )
            with self.assertRaises(EgressBlocked):
                other.complete_json(system="s", user="u", model="m")

    def test_gate_closes_again_after_scope_exits(self):
        with remote_extraction(
            endpoint="https://api.x.ai/v1", acknowledgement=ACKNOWLEDGEMENT
        ):
            pass
        with self.assertRaises(EgressBlocked):
            XAIChatClient(api_key="k").complete_json(
                system="s", user="u", model="m"
            )

    def test_preview_builds_payload_without_sending(self):
        from agentlog.analysis.extractors.egress_preview import (
            build_egress_preview,
            verify_preview_clean,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.db"
            conn = connect(db)
            init_db(conn)
            _seed_window_with_canary(conn)
            preview = build_egress_preview(conn, limit=10, ux_only=False)
            conn.close()

        self.assertFalse(preview["sent_anything"])
        self.assertFalse(preview["remote_extraction_enabled"])
        self.assertEqual(preview["redaction_version"], REDACTION_VERSION)
        self.assertEqual(verify_preview_clean(preview, ALL_CANARIES), [])


def _seed_window_with_canary(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sessions (id, harness, external_id, started_at) VALUES (?,?,?,?)",
        ("s1", "codex", "s1", "2026-01-01T00:00:00+00:00"),
    )
    for mid, seq, role, text in (
        ("m1", 1, "user", f"key {CANARY_KEY}"),
        ("m2", 2, "assistant", f"used {CANARY_AWS}"),
    ):
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, seq, role, timestamp, text, content_hash,
                is_tool_plumbing, authored_by_agent
            ) VALUES (?,?,?,?,?,?,?,0,0)
            """,
            (mid, "s1", seq, role, "2026-01-01T00:00:00+00:00", text, mid),
        )
    conn.execute(
        """
        INSERT INTO exchange_windows (
            id, session_id, request_message_id, response_message_id,
            input_hash, content_hash
        ) VALUES (?,?,?,?,?,?)
        """,
        ("w1", "s1", "m1", "m2", "ih1", "ch1"),
    )
    conn.commit()


class BindPolicyTests(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5", "[::1]"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_host(host))
        for host in ("0.0.0.0", "192.168.1.20", "example.com", "::"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_host(host))

    def test_non_loopback_refused_by_default(self):
        for host in ("0.0.0.0", "192.168.1.20", "::"):
            with self.subTest(host=host):
                with self.assertRaises(BindPolicyViolation):
                    resolve_bind(host=host, port=8787)

    def test_non_loopback_refused_with_flag_but_no_token(self):
        with self.assertRaises(BindPolicyViolation):
            resolve_bind(host="0.0.0.0", port=8787, allow_remote_access=True)

    def test_non_loopback_allowed_with_flag_and_token_and_warns(self):
        decision = resolve_bind(
            host="0.0.0.0", port=8787, allow_remote_access=True, token="t0ken"
        )
        self.assertFalse(decision.loopback)
        self.assertEqual(decision.token, "t0ken")
        self.assertTrue(decision.warnings)
        self.assertIn("beyond this machine", decision.warnings[0])
        self.assertTrue(decision.security().requires_token)

    def test_loopback_bind_allows_missing_token_at_policy_layer(self):
        # resolve_bind still permits token=None; ``agentlog serve`` always
        # supplies a file/env/cli token on top of this.
        decision = resolve_bind(host="127.0.0.1", port=8787)
        self.assertTrue(decision.loopback)
        self.assertIsNone(decision.token)
        self.assertFalse(decision.security().requires_token)


class ServerTrustBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "a.db"
        conn = connect(self.db)
        init_db(conn)
        conn.close()
        self.token = generate_token()

    def _client(self, *, token: str | None = None) -> TestClient:
        cfg = SecurityConfig.for_bind(host="127.0.0.1", port=8787, token=token)
        return TestClient(create_app(self.db, security=cfg))

    def test_loopback_dashboard_needs_no_token(self):
        client = self._client()
        self.assertEqual(client.get("/api/health").status_code, 200)

    def test_token_required_for_reads_when_configured(self):
        client = self._client(token=self.token)
        self.assertEqual(client.get("/api/health").status_code, 401)
        ok = client.get(
            "/api/health", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(ok.status_code, 200)

    def test_token_401_keeps_cors_on_allowed_origin(self):
        """Missing Bearer must not surface as browser 'Failed to fetch'."""
        client = self._client(token=self.token)
        resp = client.get(
            "/api/health",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            resp.headers.get("access-control-allow-origin"),
            "http://127.0.0.1:8787",
        )

    def test_wrong_token_rejected(self):
        client = self._client(token=self.token)
        resp = client.get("/api/health", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    def test_mutating_route_requires_token_when_configured(self):
        client = self._client(token=self.token)
        resp = client.post("/api/attribution/rebuild")
        self.assertEqual(resp.status_code, 401)
        allowed = client.post(
            "/api/attribution/rebuild",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertNotIn(allowed.status_code, (401, 403))

    def test_cross_origin_browser_write_is_refused(self):
        client = self._client()
        resp = client.post(
            "/api/attribution/rebuild",
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_same_origin_browser_write_is_allowed(self):
        client = self._client()
        resp = client.post(
            "/api/attribution/rebuild",
            headers={
                "Origin": "http://127.0.0.1:8787",
                "Host": "127.0.0.1:8787",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertNotIn(resp.status_code, (401, 403))

    def test_browser_write_without_origin_is_refused(self):
        client = self._client()
        resp = client.post(
            "/api/attribution/rebuild",
            headers={"Sec-Fetch-Site": "cross-site", "User-Agent": "Mozilla/5.0"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_dns_rebinding_read_is_refused(self):
        client = self._client()
        resp = client.get(
            "/api/health",
            headers={
                "Host": "attacker.example",
                "Sec-Fetch-Site": "same-origin",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_cross_origin_browser_read_is_refused(self):
        client = self._client()
        resp = client.get(
            "/api/health",
            headers={
                "Origin": "https://evil.example",
                "User-Agent": "Mozilla/5.0",
            },
        )
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
