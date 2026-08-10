"""Local loopback API token file, SPA injection, and SSE query auth."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentlog.api.app import create_app  # noqa: E402
from agentlog.api.local_token import (  # noqa: E402
    ensure_token_file,
    inject_spa_token,
    resolve_serve_token,
    write_token_file,
)
from agentlog.api.security import (  # noqa: E402
    SecurityConfig,
    _token_from,
    generate_token,
)
from agentlog.db.schema import connect, init_db  # noqa: E402
from starlette.requests import Request  # noqa: E402


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


class SpaInjectionTests(unittest.TestCase):
    def test_injects_before_head_close(self):
        html = "<html><head><title>t</title></head><body></body></html>"
        out = inject_spa_token(html, "sekrit")
        self.assertIn('window.__AGENTLOG_TOKEN__="sekrit"', out)
        self.assertLess(out.index("__AGENTLOG_TOKEN__"), out.index("</head>"))


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

    def _client(self) -> TestClient:
        cfg = SecurityConfig.for_bind(
            host="127.0.0.1", port=8787, token=self.token
        )
        return TestClient(
            create_app(self.db, security=cfg, dist_dir=self.dist)
        )

    def test_bearer_and_header_and_sse_query(self):
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
        # Query token must not work on ordinary routes.
        self.assertEqual(
            client.get(f"/api/health?token={self.token}").status_code, 401
        )
        # EventSource cannot set headers — query token is accepted only on SSE paths.
        sse_req = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/events/stream",
                "raw_path": b"/api/events/stream",
                "query_string": f"token={self.token}".encode(),
                "headers": [],
                "client": ("127.0.0.1", 123),
                "server": ("127.0.0.1", 8787),
            }
        )
        self.assertEqual(_token_from(sse_req), self.token)
        health_req = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/api/health",
                "raw_path": b"/api/health",
                "query_string": f"token={self.token}".encode(),
                "headers": [],
                "client": ("127.0.0.1", 123),
                "server": ("127.0.0.1", 8787),
            }
        )
        self.assertIsNone(_token_from(health_req))

    def test_served_spa_embeds_token(self):
        client = self._client()
        # HTML route itself does not require the bearer (browser loads it first).
        resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'window.__AGENTLOG_TOKEN__="{self.token}"', resp.text)


if __name__ == "__main__":
    unittest.main()
