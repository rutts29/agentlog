"""Deterministic redaction applied before any extraction payload is built.

Transcript text is the most sensitive data agentlog holds: it contains pasted
credentials, source snippets, private filesystem paths, and client data. Every
payload handed to a labeler — local subagent or remote API — passes through
``redact_payload`` first, so truncation is never the only thing standing
between a secret and an egress path.

``REDACTION_VERSION`` is recorded alongside run provenance. A label can always
be traced back to the exact rule set that produced the text the labeler saw;
changing any pattern below requires bumping the version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REDACTION_VERSION = "r1"

_SECRET = "[REDACTED:{kind}]"


@dataclass
class RedactionReport:
    version: str = REDACTION_VERSION
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def note(self, kind: str, n: int = 1) -> None:
        if n:
            self.counts[kind] = self.counts.get(kind, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {
            "redaction_version": self.version,
            "redactions": dict(sorted(self.counts.items())),
            "redaction_total": self.total,
        }


# (kind, pattern). Order matters: credential shapes run before path and PII
# rules so a token containing a path-like or mail-like substring is masked
# whole rather than partially rewritten.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b"),
    ),
    (
        "github_token",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    ),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b[rsp]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("digitalocean_token", re.compile(r"\bdop_v1_[a-f0-9]{40,}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("shopify_token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b")),
    (
        "authorization_header",
        re.compile(
            r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}",
        ),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    (
        "url_credentials",
        re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    ),
    (
        # KEY=VALUE / "key": "value" where the key names a credential.
        "secret_assignment",
        re.compile(
            r"(?i)\b[A-Za-z0-9_.-]*"
            r"(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|credential|client[_-]?secret|auth[_-]?key|session[_-]?key)"
            r"[A-Za-z0-9_.-]*\"?\s*[:=]\s*"
            r"(?:\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;)\]}\"']{4,})"
        ),
    ),
    (
        "ssn",
        re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    ),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "phone",
        re.compile(r"(?<![\w.-])\+\d{1,3}[ -]?\(?\d{2,4}\)?[ -]?\d{3,4}[ -]?\d{3,4}(?![\w.-])"),
    ),
)

_NUMERIC = re.compile(r"[\d_,.]+")
_ASSIGNMENT_SPLIT = re.compile(r"\s*[:=]\s*")
# "tokens" plural and counting words mean a quantity, not a credential. This
# corpus is largely about token accounting, so masking max_output_tokens=32000
# would destroy analysis input without protecting anything.
_COUNT_WORDS = frozenset(
    {
        "max",
        "min",
        "num",
        "n",
        "total",
        "sum",
        "avg",
        "count",
        "counts",
        "remaining",
        "used",
        "limit",
        "budget",
        "approx",
        "original",
        "input",
        "inputs",
        "output",
        "outputs",
        "cache",
        "cached",
        "window",
        "per",
    }
)
_STRONG_SECRET_KEY = re.compile(r"(?i)passw|secret|credential|private[_-]?key")
_NON_SECRET_VALUES = frozenset(
    {
        "none",
        "null",
        "nil",
        "undefined",
        "true",
        "false",
        "str",
        "string",
        "int",
        "bool",
        "number",
        "optional[str]",
    }
)


def _split_assignment(text: str) -> tuple[str, str]:
    parts = _ASSIGNMENT_SPLIT.split(text, maxsplit=1)
    if len(parts) != 2:
        return text, ""
    return parts[0].strip().strip("\"'"), parts[1].strip()


def _key_is_a_quantity(key: str) -> bool:
    low = key.lower()
    if low.endswith("tokens") or "count" in low:
        return True
    return bool(_COUNT_WORDS & set(re.split(r"[_.\-]+", low)))


def _is_secret_assignment(match: re.Match[str]) -> bool:
    key, value = _split_assignment(match.group(0))
    if not value or _key_is_a_quantity(key):
        return False
    bare = value.strip("\"'").strip()
    if not bare or bare.lower() in _NON_SECRET_VALUES:
        return False
    if _NUMERIC.fullmatch(bare) and not _STRONG_SECRET_KEY.search(key):
        return False
    return True


def _is_credit_card(match: re.Match[str]) -> bool:
    return _luhn_ok(match.group(0))


_VALIDATORS = {
    "secret_assignment": _is_secret_assignment,
    "credit_card": _is_credit_card,
}

# Home-directory shapes, applied after credential masking. The owner's real
# home is rewritten to "~"; other users' homes lose the account name.
_POSIX_HOME = re.compile(r"/(Users|home)/[^/\s\"':;,)\]}]+")
_WINDOWS_HOME = re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"':;,)\]}]+")


def _luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(nums)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _home_str() -> str:
    try:
        return str(Path.home())
    except (OSError, RuntimeError):
        return ""


def redact_text(text: str, report: RedactionReport | None = None) -> str:
    """Mask credentials, home paths, and obvious PII in ``text``."""
    if not text:
        return text
    rep = report if report is not None else RedactionReport()
    out = text

    for kind, pattern in _RULES:
        placeholder = _SECRET.format(kind=kind)

        validator = _VALIDATORS.get(kind)

        def _sub(
            match: re.Match[str],
            _kind: str = kind,
            _ph: str = placeholder,
            _ok=validator,
        ) -> str:
            if _ok is not None and not _ok(match):
                return match.group(0)
            rep.note(_kind)
            return _ph

        out = pattern.sub(_sub, out)

    home = _home_str()
    if home and home in out:
        rep.note("home_path", out.count(home))
        out = out.replace(home, "~")

    def _mask_home(match: re.Match[str]) -> str:
        rep.note("home_path")
        prefix = match.group(0).rsplit("/", 1)[0] if "/" in match.group(0) else match.group(0)
        return f"{prefix}/[REDACTED:user]"

    out = _POSIX_HOME.sub(_mask_home, out)

    def _mask_win_home(match: re.Match[str]) -> str:
        rep.note("home_path")
        return match.group(0).rsplit("\\", 1)[0] + "\\[REDACTED:user]"

    out = _WINDOWS_HOME.sub(_mask_win_home, out)
    return out


_TEXT_FIELDS = ("user", "assistant", "next_user")
_LIST_FIELDS = ("tool_timeline", "skills_loaded", "skill_exposure_types")


def redact_payload(
    payload: dict[str, Any],
    *,
    report: RedactionReport | None = None,
) -> tuple[dict[str, Any], RedactionReport]:
    """Redact every free-text field of an extraction payload."""
    rep = report if report is not None else RedactionReport()
    out = dict(payload)
    for key in _TEXT_FIELDS:
        val = out.get(key)
        if isinstance(val, str):
            out[key] = redact_text(val, rep)
    for key in _LIST_FIELDS:
        val = out.get(key)
        if isinstance(val, list):
            out[key] = [
                redact_text(v, rep) if isinstance(v, str) else v for v in val
            ]
    return out, rep


def redact_payloads(
    payloads: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], RedactionReport]:
    rep = RedactionReport()
    out = [redact_payload(p, report=rep)[0] for p in payloads]
    return out, rep
