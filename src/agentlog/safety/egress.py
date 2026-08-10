"""Network egress policy for agentlog.

agentlog is a local observatory. Nothing derived from the owner's transcripts
may leave the machine unless the owner turns remote extraction on for that
specific run, in-process, with the exact acknowledgement string below.

The gate lives here rather than in the CLI so no library caller, service, or
future code path can reach a remote endpoint implicitly: the only way through
``assert_egress_allowed`` is an explicit ``enable_remote_extraction`` call.
Default state is deny, and the state is never persisted between processes.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse

ACKNOWLEDGEMENT = "i-understand-transcript-text-leaves-this-machine"

EGRESS_DISCLOSURE = (
    "Remote extraction sends, for each labeled window: your prompt text, the "
    "assistant's reply text, your following prompt text, the tool-call "
    "timeline, the model name, and the names of loaded skills. Text is "
    "redacted for credentials, home paths, and obvious PII first, and "
    "truncated, but it remains verbatim excerpts of your coding transcripts. "
    "It is sent to a third-party API over the network and is subject to that "
    "provider's retention policy."
)


class EgressBlocked(RuntimeError):
    """Raised when code attempts a network send that policy does not permit."""


@dataclass(frozen=True)
class EgressGrant:
    endpoint: str
    purpose: str

    @property
    def host(self) -> str:
        return _host_of(self.endpoint)


_lock = threading.Lock()
_grant: EgressGrant | None = None


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def remote_extraction_grant() -> EgressGrant | None:
    with _lock:
        return _grant


def remote_extraction_enabled() -> bool:
    return remote_extraction_grant() is not None


def enable_remote_extraction(
    *,
    endpoint: str,
    acknowledgement: str,
    purpose: str = "ux_extraction",
) -> EgressGrant:
    """Permit sends to one endpoint for the rest of this process.

    ``acknowledgement`` must equal :data:`ACKNOWLEDGEMENT` exactly. A boolean
    flag or truthy config value cannot satisfy this, which is the point: an
    accidental ``allow_network=True`` somewhere must not open egress.
    """
    if acknowledgement != ACKNOWLEDGEMENT:
        raise EgressBlocked(
            "remote extraction requires the exact acknowledgement string "
            f"{ACKNOWLEDGEMENT!r}. {EGRESS_DISCLOSURE}"
        )
    host = _host_of(endpoint)
    if not host:
        raise EgressBlocked(f"cannot grant egress to an endpoint without a host: {endpoint!r}")
    grant = EgressGrant(endpoint=endpoint, purpose=purpose)
    global _grant
    with _lock:
        _grant = grant
    return grant


def disable_remote_extraction() -> None:
    global _grant
    with _lock:
        _grant = None


@contextmanager
def remote_extraction(
    *,
    endpoint: str,
    acknowledgement: str,
    purpose: str = "ux_extraction",
) -> Iterator[EgressGrant]:
    previous = remote_extraction_grant()
    grant = enable_remote_extraction(
        endpoint=endpoint, acknowledgement=acknowledgement, purpose=purpose
    )
    try:
        yield grant
    finally:
        global _grant
        with _lock:
            _grant = previous


def assert_egress_allowed(url: str, *, purpose: str = "network send") -> EgressGrant:
    """Fail closed unless remote extraction was explicitly enabled for ``url``."""
    grant = remote_extraction_grant()
    if grant is None:
        raise EgressBlocked(
            f"no-network mode: refusing to {purpose} to {url}. agentlog analyzes "
            "your transcripts locally by default. Remote extraction is a separate, "
            "explicit opt-in (agentlog extract egress-preview shows exactly what "
            f"would be sent). {EGRESS_DISCLOSURE}"
        )
    if grant.host != _host_of(url):
        raise EgressBlocked(
            f"remote extraction was authorized for host {grant.host!r}, "
            f"not {_host_of(url)!r}"
        )
    return grant
