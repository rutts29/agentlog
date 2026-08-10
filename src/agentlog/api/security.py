"""Bind policy and request-level trust boundary for the dashboard API.

The API serves the owner's complete transcript text and exposes mutating
routes. It is a single-user local tool, so the goal is to close the real
attack surface without turning the loopback dashboard into a login screen.

Three distinct threats, three distinct controls:

* LAN exposure. Binding off loopback publishes every transcript to the
  network. ``resolve_bind`` refuses a non-loopback bind unless the owner
  passes the unsafe flag *and* configures a token, and once a token exists
  every request must carry it.
* DNS rebinding. A web page can point a hostname it controls at 127.0.0.1
  and read responses as same-origin. The ``Host`` header then carries the
  attacker's name, so browser-originated requests must present a Host we
  recognise.
* Cross-site request forgery. A page can issue a simple cross-origin POST
  without preflight; CORS blocks reading the response but not the write.
  Browser-originated mutations must carry an allowed ``Origin``.

Browser detection is deliberate. curl and other non-browser clients are not
rebinding or CSRF vectors, but when a token is configured (the default for
``agentlog serve``) they must still present it — that is what stops arbitrary
local processes from reading transcripts on loopback. The dashboard SPA and
Vite proxy carry the token without a login screen.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from dataclasses import dataclass, field
from typing import Iterable

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from agentlog.config import API_TOKEN_ENV_VAR

TOKEN_ENV_VAR = API_TOKEN_ENV_VAR
TOKEN_HEADER = "x-agentlog-token"
TOKEN_QUERY_PARAM = "token"

LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", ""})
DASHBOARD_PORTS = (8787, 8722, 5173)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_BROWSER_HEADERS = (
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-dest",
    "origin",
    "cookie",
    "referer",
)


class BindPolicyViolation(RuntimeError):
    """Raised when a requested bind would expose the API beyond loopback."""


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def is_loopback_host(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1, and the localhost names."""
    if host is None:
        return False
    candidate = host.strip().strip("[]").lower()
    if candidate in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _hostname_of(header_value: str | None) -> str:
    if not header_value:
        return ""
    value = header_value.strip()
    if value.startswith("["):
        return value[1 : value.find("]")].lower() if "]" in value else value.lower()
    return value.rsplit(":", 1)[0].lower() if value.count(":") == 1 else value.lower()


def _origin_host_port(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def default_allowed_origins(ports: Iterable[int] = DASHBOARD_PORTS) -> frozenset[str]:
    out: set[str] = set()
    for port in ports:
        for host in ("127.0.0.1", "localhost", "[::1]"):
            out.add(f"http://{host}:{port}")
    return frozenset(out)


def default_allowed_hosts() -> frozenset[str]:
    return frozenset({"127.0.0.1", "localhost", "::1", "localhost.localdomain"})


@dataclass(frozen=True)
class SecurityConfig:
    token: str | None = None
    allowed_hosts: frozenset[str] = field(default_factory=default_allowed_hosts)
    allowed_origins: frozenset[str] = field(default_factory=default_allowed_origins)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787

    @property
    def requires_token(self) -> bool:
        return bool(self.token)

    @classmethod
    def for_bind(
        cls,
        *,
        host: str,
        port: int,
        token: str | None = None,
        extra_hosts: Iterable[str] = (),
    ) -> "SecurityConfig":
        hosts = set(default_allowed_hosts())
        hosts.add(host.strip().strip("[]").lower())
        hosts.update(h.strip().lower() for h in extra_hosts if h.strip())
        hosts.discard("0.0.0.0")
        hosts.discard("::")
        origins = set(default_allowed_origins())
        origins.update(default_allowed_origins((port,)))
        if not is_loopback_host(host) and host not in ("0.0.0.0", "::"):
            origins.add(f"http://{host}:{port}")
        return cls(
            token=token,
            allowed_hosts=frozenset(hosts),
            allowed_origins=frozenset(origins),
            bind_host=host,
            bind_port=port,
        )


@dataclass(frozen=True)
class BindDecision:
    host: str
    port: int
    token: str | None
    loopback: bool
    warnings: tuple[str, ...] = ()

    def security(self, *, extra_hosts: Iterable[str] = ()) -> SecurityConfig:
        return SecurityConfig.for_bind(
            host=self.host, port=self.port, token=self.token, extra_hosts=extra_hosts
        )


def resolve_bind(
    *,
    host: str,
    port: int,
    allow_remote_access: bool = False,
    token: str | None = None,
) -> BindDecision:
    """Validate a requested bind. Raises ``BindPolicyViolation`` when unsafe.

    A non-loopback bind needs both the explicit flag and a token: the flag
    alone would still publish an unauthenticated transcript archive.
    """
    loopback = is_loopback_host(host)
    warnings: list[str] = []
    if loopback:
        return BindDecision(
            host=host, port=port, token=token, loopback=True, warnings=()
        )

    if not allow_remote_access:
        raise BindPolicyViolation(
            f"refusing to bind {host}:{port}. The dashboard serves your full "
            "transcript text and has mutating endpoints, so it listens on "
            "127.0.0.1 only. Pass --allow-remote-access together with a token "
            f"({TOKEN_ENV_VAR} or --token) if you really want it reachable."
        )
    if not token:
        raise BindPolicyViolation(
            f"refusing to bind {host}:{port} without authentication. Set "
            f"{TOKEN_ENV_VAR} or pass --token; every request will then need it."
        )
    warnings.append(
        f"agentlog is listening on {host}:{port}, reachable beyond this machine. "
        "Every request requires the API token, but transcript text will cross "
        "the network in plaintext HTTP."
    )
    return BindDecision(
        host=host,
        port=port,
        token=token,
        loopback=False,
        warnings=tuple(warnings),
    )


def _looks_like_browser(request: Request) -> bool:
    headers = request.headers
    if any(h in headers for h in _BROWSER_HEADERS):
        return True
    return headers.get("user-agent", "").startswith("Mozilla/")


def _token_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header = request.headers.get(TOKEN_HEADER)
    if header:
        return header.strip()
    # EventSource cannot set Authorization; allow ?token= on the SSE stream only.
    path = request.url.path
    if path.endswith("/stream") or "/events/" in path:
        query = request.query_params.get(TOKEN_QUERY_PARAM)
        if query:
            return query.strip()
    return None


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


class LocalTrustBoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, config: SecurityConfig) -> None:
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        cfg = self.config
        path = request.url.path

        # Token gates the API only. The SPA shell (and /assets) must load
        # unauthenticated so the HTML can embed the token for subsequent calls.
        if cfg.requires_token and (path == "/api" or path.startswith("/api/")):
            supplied = _token_from(request)
            if supplied is None or not hmac.compare_digest(supplied, cfg.token or ""):
                return _deny(
                    401,
                    "missing or invalid API token; send Authorization: Bearer <token>",
                )

        browser = _looks_like_browser(request)

        if browser:
            host = _hostname_of(request.headers.get("host"))
            if host and host not in cfg.allowed_hosts:
                return _deny(
                    403,
                    f"host {host!r} is not an allowed host for this server",
                )

        origin = request.headers.get("origin")
        if origin and _origin_host_port(origin) not in cfg.allowed_origins:
            return _deny(403, f"origin {origin!r} is not allowed")

        if request.method in MUTATING_METHODS and browser:
            if not origin:
                return _deny(
                    403,
                    "browser-initiated writes must send an Origin header",
                )

        return await call_next(request)


def install_security(app: FastAPI, config: SecurityConfig | None = None) -> SecurityConfig:
    cfg = config or SecurityConfig()
    app.state.security = cfg
    app.add_middleware(LocalTrustBoundaryMiddleware, config=cfg)
    return cfg
