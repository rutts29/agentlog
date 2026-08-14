"""Local loopback API token file and browser-session authentication."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentlog.api.app import create_app  # noqa: E402
from agentlog.api.local_token import (  # noqa: E402
    ensure_token_file,
    resolve_serve_token,
    write_token_file,
)
from agentlog.api.security import (  # noqa: E402
    BROWSER_SESSION_COOKIE,
    SecurityConfig,
    browser_session_token,
    generate_token,
)
from agentlog.db.schema import connect, init_db  # noqa: E402


class TokenFileTests(unittest.TestCase):
    def test_ensure_creates_0600_and_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".agentlog" / "api_token"
            token, written, created = ensure_token_file(path)
            self.assertTrue(created)
            self.assertEqual(written, path)
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            again, _, created2 = ensure_token_file(path)
            self.assertFalse(created2)
            self.assertEqual(again, token)

    def test_rotate_replaces_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_token"
            first, _, _ = ensure_token_file(path)
            second, _, rotated = ensure_token_file(path, rotate=True)
            self.assertTrue(rotated)
            self.assertNotEqual(first, second)

    def test_heals_group_readable_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_token"
            write_token_file(path, "abc")
            os.chmod(path, 0o644)
            token, _, created = ensure_token_file(path)
            self.assertFalse(created)
            self.assertEqual(token, "abc")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_resolve_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_token"
            ensure_token_file(path)
            cli = resolve_serve_token(
                cli_token="from-cli", env_token="from-env", token_path=path
            )
            self.assertEqual(cli.source, "cli")
            self.assertEqual(cli.token, "from-cli")
            env = resolve_serve_token(
                cli_token=None, env_token="from-env", token_path=path
            )
            self.assertEqual(env.source, "env")
            file_tok = resolve_serve_token(
                cli_token=None, env_token=None, token_path=path
            )
            self.assertEqual(file_tok.source, "file")
            self.assertEqual(file_tok.path, path)


class BrowserSessionTokenTests(unittest.TestCase):
    def test_is_stable_and_not_the_api_bearer(self):
        session = browser_session_token("sekrit")
        self.assertEqual(session, browser_session_token("sekrit"))
        self.assertNotEqual(session, "sekrit")


class TokenMiddlewareModesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "a.db"
        conn = connect(self.db)
        init_db(conn)
        # FK must survive init (migrations toggle it during rebuilds).
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        conn.close()
        self.token = generate_token()
        self.dist = self.root / "dist"
        self.dist.mkdir(parents=True)
        (self.dist / "index.html").write_text(
            "<html><head><title>t</title></head><body>dash</body></html>",
            encoding="utf-8",
        )
        (self.dist / "site.webmanifest").write_text("{}", encoding="utf-8")

    def _client(self) -> TestClient:
        cfg = SecurityConfig.for_bind(
            host="127.0.0.1", port=8787, token=self.token
        )
        return TestClient(
            create_app(self.db, security=cfg, dist_dir=self.dist)
        )

    def test_bearer_and_header_authenticate_api_clients(self):
        client = self._client()
        self.assertEqual(client.get("/api/health").status_code, 401)
        ok = client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(ok.status_code, 200)
        hdr = client.get(
            "/api/health", headers={"x-agentlog-token": self.token}
        )
        self.assertEqual(hdr.status_code, 200)
        # Query credentials leak through history and logs, so they are not accepted.
        self.assertEqual(
            client.get(f"/api/health?token={self.token}").status_code, 401
        )
        self.assertEqual(
            client.get(f"/api/events/stream?token={self.token}").status_code, 401
        )

    def test_served_spa_sets_http_only_session_without_bearer_in_html(self):
        client = self._client()
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(self.token, resp.text)
        self.assertNotIn("__AGENTLOG_TOKEN__", resp.text)
        self.assertEqual(
            client.cookies.get(BROWSER_SESSION_COOKIE),
            browser_session_token(self.token),
        )
        cookie = resp.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/api", cookie)
        self.assertNotIn("domain=", cookie)

    def test_browser_session_authenticates_browser_only(self):
        client = self._client()
        client.get("/")
        self.assertEqual(client.get("/api/health").status_code, 401)
        response = client.get(
            "/api/health",
            headers={
                "Host": "127.0.0.1:8787",
                "User-Agent": "Mozilla/5.0",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_forged_browser_session_is_rejected(self):
        client = self._client()
        client.cookies.set(BROWSER_SESSION_COOKIE, "forged", path="/api")
        response = client.get(
            "/api/health",
            headers={
                "Host": "127.0.0.1:8787",
                "User-Agent": "Mozilla/5.0",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(response.status_code, 401)

    def test_browser_session_cannot_bypass_cross_origin_write_protection(self):
        client = self._client()
        client.get("/")
        response = client.post(
            "/api/attribution/rebuild",
            headers={
                "Host": "127.0.0.1:8787",
                "Origin": "https://evil.example",
                "User-Agent": "Mozilla/5.0",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_browser_session_authenticates_eventsource_without_query_token(self):
        client = self._client()
        client.get("/")
        headers = {
            "Host": "127.0.0.1:8787",
            "User-Agent": "Mozilla/5.0",
            "Sec-Fetch-Site": "same-origin",
        }
        with mock.patch(
            "agentlog.api.events.iter_event_sse",
            return_value=iter((": connected\n\n",)),
        ):
            with client.stream("GET", "/api/events/stream", headers=headers) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
                self.assertIn(": connected", response.read().decode())

    def test_remote_bind_keeps_bearer_out_of_spa_html(self):
        cfg = SecurityConfig.for_bind(
            host="192.0.2.10", port=8787, token=self.token
        )
        client = TestClient(create_app(self.db, security=cfg, dist_dir=self.dist))
        response = client.get("/", headers={"Host": "192.0.2.10:8787"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.token, response.text)
        self.assertNotIn("set-cookie", response.headers)
        client.cookies.set(
            BROWSER_SESSION_COOKIE,
            browser_session_token(self.token),
            path="/api",
        )
        authenticated = client.get(
            "/api/health",
            headers={
                "Host": "192.0.2.10:8787",
                "User-Agent": "Mozilla/5.0",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(authenticated.status_code, 401)

    def test_spa_serves_dist_files_and_falls_back_for_routes(self):
        client = self._client()
        static = client.get("/site.webmanifest")
        self.assertEqual(static.status_code, 200)
        self.assertEqual(static.text, "{}")
        route = client.get("/sessions/example")
        self.assertEqual(route.status_code, 200)
        self.assertIn("dash", route.text)

    def test_spa_refuses_encoded_path_traversal(self):
        secret = self.root / "secret.txt"
        secret.write_text("do-not-serve", encoding="utf-8")
        client = self._client()
        for path in ("/%2e%2e/secret.txt", "/%2e%2e%2fsecret.txt"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("do-not-serve", response.text)

    def test_spa_refuses_symlink_escaping_dist(self):
        secret = self.root / "secret.txt"
        secret.write_text("do-not-serve", encoding="utf-8")
        (self.dist / "leak.txt").symlink_to(secret)
        response = self._client().get("/leak.txt")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("do-not-serve", response.text)

    def test_spa_refuses_symlinked_index_outside_dist(self):
        secret = self.root / "secret.html"
        secret.write_text("do-not-serve", encoding="utf-8")
        (self.dist / "index.html").unlink()
        (self.dist / "index.html").symlink_to(secret)
        response = self._client().get("/sessions/example")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("do-not-serve", response.text)

    def test_assets_symlink_root_is_not_mounted(self):
        secret = self.root / "secret.js"
        secret.write_text("do-not-serve", encoding="utf-8")
        (self.dist / "assets").symlink_to(self.root, target_is_directory=True)
        response = self._client().get("/assets/secret.js")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("do-not-serve", response.text)


if __name__ == "__main__":
    unittest.main()
