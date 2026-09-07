"""Cookie writes require the API origin, not another allowed local port.

The in-process endpoint records only synthetic writes. No database, browser,
network listener, or owner credentials are used.
"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentlog.api.security import (
    BROWSER_SESSION_COOKIE,
    MUTATING_METHODS,
    TOKEN_HEADER,
    SecurityConfig,
    browser_session_token,
    install_security,
)


class CookieOriginBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.token = "synthetic-cookie-origin-test-token"
        self.writes = []
        app = FastAPI()

        @app.api_route("/api/probe", methods=sorted(MUTATING_METHODS))
        def mutate():
            self.writes.append("synthetic write")
            return {"written": True}

        install_security(
            app,
            SecurityConfig.for_bind(
                host="127.0.0.1", port=3000, token=self.token
            ),
        )
        self.client = TestClient(app, base_url="http://127.0.0.1:3000")
        self.addCleanup(self.client.close)
        self.client.cookies.set(
            BROWSER_SESSION_COOKIE,
            browser_session_token(self.token),
            path="/api",
        )

    def test_same_origin_cookie_writes_reach_endpoint(self):
        for method in sorted(MUTATING_METHODS):
            with self.subTest(method=method):
                response = self.client.request(
                    method, "/api/probe",
                    headers={"Origin": "http://127.0.0.1:3000"},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.writes), len(MUTATING_METHODS))

    def test_other_allowed_local_port_cannot_write_with_cookie(self):
        for method in sorted(MUTATING_METHODS):
            with self.subTest(method=method):
                response = self.client.request(
                    method, "/api/probe",
                    headers={
                        "Origin": "http://127.0.0.1:5173",
                        "Sec-Fetch-Site": "same-site",
                    },
                )
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes, [])

    def test_other_allowed_hostname_cannot_write_with_cookie(self):
        response = self.client.post(
            "/api/probe", headers={"Origin": "http://localhost:3000"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes, [])

    def test_different_scheme_cannot_write_with_cookie(self):
        # A TLS request and an HTTP origin are distinct even on the same port.
        response = self.client.post(
            "https://127.0.0.1:3000/api/probe",
            headers={"Origin": "http://127.0.0.1:3000"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes, [])

    def test_vite_origin_can_write_with_explicit_credential(self):
        for credential in (
            {"Authorization": f"Bearer {self.token}"},
            {TOKEN_HEADER: self.token},
        ):
            with self.subTest(header=next(iter(credential))):
                response = self.client.post(
                    "/api/probe",
                    headers={"Origin": "http://127.0.0.1:5173", **credential},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.writes), 2)

    def test_invalid_bearer_does_not_relax_cookie_origin_boundary(self):
        response = self.client.post(
            "/api/probe",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Authorization": "Bearer wrong-synthetic-token",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes, [])

    def test_cookie_write_without_origin_keeps_existing_rejection(self):
        response = self.client.post(
            "/api/probe", headers={"User-Agent": "Mozilla/5.0"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json()["detail"],
            "browser-initiated writes must send an Origin header",
        )
        self.assertEqual(self.writes, [])

    def test_unlisted_origin_stays_rejected_even_with_bearer(self):
        response = self.client.post(
            "/api/probe",
            headers={
                "Origin": "https://untrusted.example",
                "Authorization": f"Bearer {self.token}",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
