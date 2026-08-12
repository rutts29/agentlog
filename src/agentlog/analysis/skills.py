"""Skill definition indexing and descriptive exposure-outcome profiles.

Descriptive stats only. Vocabulary for rates:
  "sessions where X was active showed rate R (n=N)"
No causal or comparative improvement claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import xxhash

from agentlog.session_identity import (
    build_identity_context,
    logical_projection,
    provider_root_shadow_ids,
)

DEFAULT_MIN_SESSIONS = 5
CONTENT_STORE_CAP = 512_000

FRONTMATTER_OK = "ok"
FRONTMATTER_ABSENT = "absent"
FRONTMATTER_UNTERMINATED = "unterminated"
FRONTMATTER_MISSING_NAME = "missing_name"

_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "vm_bundles",
        "upstream",
        ".tmp",
    }
)

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?",
    re.DOTALL,
)
_NAME_RE = re.compile(r"(?m)^name:\s*[\"']?([^\"'\n]+?)[\"']?\s*$")
_DESC_BLOCK_RE = re.compile(
    r"(?ms)^description:\s*(?:>-?\s*\n)?(.*?)(?=^[a-zA-Z0-9_-]+:|\Z)"
)


def default_skill_roots(home: Path | None = None) -> list[tuple[str, Path]]:
    """Authoritative inventory roots from docs/data-inventory.md §7 / §10.

    There is deliberately no t3 root: t3 code has no skills directory of its own
    and reaches skills through each provider driver's home. t3's view is recorded
    separately by ``index_t3_visibility``.
    """
    base = home or Path.home()
    return [
        ("cursor", base / ".cursor" / "skills-cursor"),
        ("cursor-plugins", base / ".cursor" / "plugins" / "cache"),
        ("codex", base / ".codex" / "skills"),
        ("claude-user", base / ".claude" / "skills"),
        ("claude-plugins", base / ".claude" / "plugins" / "cache"),
        ("agents", base / ".agents" / "skills"),
    ]


def default_t3_caches_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".t3" / "caches"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _skill_id_for_path(source_path: Path) -> str:
    return hashlib.sha1(str(source_path.resolve()).encode("utf-8")).hexdigest()[:24]


def _version_id(skill_id: str, content_hash: str) -> str:
    return hashlib.sha1(f"{skill_id}:{content_hash}".encode("utf-8")).hexdigest()[:24]


def parse_skill_frontmatter(text: str) -> dict[str, str]:
    """Extract name/description from SKILL.md YAML frontmatter without PyYAML."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    block = match.group(1)
    out: dict[str, str] = {}
    name_m = _NAME_RE.search(block)
    if name_m:
        out["name"] = name_m.group(1).strip()
    desc_m = _DESC_BLOCK_RE.search(block)
    if desc_m:
        raw = desc_m.group(1).strip()
        # Collapse folded YAML newlines into spaces for a short blurb.
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            out["description"] = " ".join(lines)
    return out


def frontmatter_status(text: str) -> tuple[str, str | None]:
    """Classify SKILL.md frontmatter so problems are surfaced, not dropped."""
    stripped = text.lstrip("\ufeff")
    if not stripped.lstrip().startswith("---"):
        return FRONTMATTER_ABSENT, "no YAML frontmatter block; using directory name"
    if not _FRONTMATTER_RE.match(stripped):
        return (
            FRONTMATTER_UNTERMINATED,
            "frontmatter opens with --- but has no closing --- delimiter",
        )
    meta = parse_skill_frontmatter(stripped)
    if not meta.get("name"):
        return (
            FRONTMATTER_MISSING_NAME,
            "frontmatter present but no parseable name key; using directory name",
        )
    return FRONTMATTER_OK, None


def normalize_skill_content(text: str) -> str:
    """Whitespace-insensitive view of SKILL.md for near-duplicate detection."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


def content_hash_of(text: str) -> str:
    return xxhash.xxh64(text.encode("utf-8", errors="replace")).hexdigest()


def discover_skill_files(roots: Iterable[tuple[str, Path]]) -> list[tuple[str, Path]]:
    """Walk roots read-only; return (source, SKILL.md path) pairs."""
    found: list[tuple[str, Path]] = []
    for source, root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("SKILL.md")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            found.append((source, path))
    return found


def plugin_qualifier(source: str, path: Path) -> str | None:
    """Derive plugin-style prefix for claude-plugins paths when possible."""
    if source != "claude-plugins":
        return None
    parts = path.parts
    try:
        cache_idx = parts.index("cache")
    except ValueError:
        return None
    # .../cache/<marketplace>/<plugin>/<version>/skills/<name>/SKILL.md
    if len(parts) < cache_idx + 5:
        return None
    plugin = parts[cache_idx + 2]
    if plugin and plugin not in {".", ".."}:
        return plugin
    return None


def skill_aliases(name: str, source: str, path: Path) -> set[str]:
    aliases = {name}
    parent = path.parent.name
    if parent and parent not in {"skills", "skill"}:
        aliases.add(parent)
    qual = plugin_qualifier(source, path)
    if qual:
        aliases.add(f"{qual}:{name}")
        if parent:
            aliases.add(f"{qual}:{parent}")
    # Codex/Cursor often expose bare names; Claude may use plugin:skill.
    if ":" in name:
        aliases.add(name.split(":", 1)[1])
    return {a for a in aliases if a}


@dataclass
class IndexStats:
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    versions_added: int = 0
    missing_roots: list[str] = field(default_factory=list)
    frontmatter_issues: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "versions_added": self.versions_added,
            "missing_roots": list(self.missing_roots),
            "frontmatter_issues": self.frontmatter_issues,
        }


def index_skills(
    conn: sqlite3.Connection,
    roots: list[tuple[str, Path]] | None = None,
    *,
    now: str | None = None,
) -> IndexStats:
    """Scan live skill inventories and upsert skills / skill_versions.

    Idempotent: content-hash change detection. Read-only on source dirs.
    """
    roots = roots if roots is not None else default_skill_roots()
    now = now or _utc_now()
    stats = IndexStats()
    for source, root in roots:
        if not root.is_dir():
            stats.missing_roots.append(f"{source}:{root}")

    prepared: list[dict[str, str | None]] = []
    for source, path in discover_skill_files(roots):
        stats.scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = parse_skill_frontmatter(text)
        name = (meta.get("name") or path.parent.name or path.stem).strip()
        description = meta.get("description")
        fm_status, fm_error = frontmatter_status(text)
        if fm_status != FRONTMATTER_OK:
            stats.frontmatter_issues += 1
        content_hash = _sha1_text(text)
        normalized_hash = content_hash_of(normalize_skill_content(text))
        stored = text if len(text) <= CONTENT_STORE_CAP else text[:CONTENT_STORE_CAP]
        skill_id = _skill_id_for_path(path)
        source_path = str(path.resolve())
        prepared.append(
            {
                "skill_id": skill_id,
                "name": name,
                "source": source,
                "source_path": source_path,
                "description": description,
                "content_hash": content_hash,
                "normalized_hash": normalized_hash,
                "stored": stored,
                "fm_status": fm_status,
                "fm_error": fm_error,
            }
        )

    for item in prepared:
        skill_id = str(item["skill_id"])
        name = str(item["name"])
        source = str(item["source"])
        source_path = str(item["source_path"])
        description = item["description"]
        content_hash = str(item["content_hash"])
        normalized_hash = str(item["normalized_hash"])
        stored = str(item["stored"])
        fm_status = str(item["fm_status"])
        fm_error = item["fm_error"]
        existing = conn.execute(
            "SELECT id, current_content_hash FROM skills WHERE source_path = ?",
            (source_path,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, source, source_path, description,
                    current_content_hash, first_seen_at, last_seen_at, last_indexed_at,
                    normalized_content_hash, frontmatter_status, frontmatter_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    name,
                    source,
                    source_path,
                    description,
                    content_hash,
                    now,
                    now,
                    now,
                    normalized_hash,
                    fm_status,
                    fm_error,
                ),
            )
            stats.inserted += 1
        else:
            skill_id = str(existing["id"])
            if str(existing["current_content_hash"]) == content_hash:
                conn.execute(
                    """
                    UPDATE skills
                    SET name = ?, description = ?, last_seen_at = ?, last_indexed_at = ?,
                        normalized_content_hash = ?, frontmatter_status = ?,
                        frontmatter_error = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        description,
                        now,
                        now,
                        normalized_hash,
                        fm_status,
                        fm_error,
                        skill_id,
                    ),
                )
                stats.unchanged += 1
            else:
                conn.execute(
                    """
                    UPDATE skills
                    SET name = ?, description = ?, current_content_hash = ?,
                        last_seen_at = ?, last_indexed_at = ?,
                        normalized_content_hash = ?, frontmatter_status = ?,
                        frontmatter_error = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        description,
                        content_hash,
                        now,
                        now,
                        normalized_hash,
                        fm_status,
                        fm_error,
                        skill_id,
                    ),
                )
                stats.updated += 1

        ver = conn.execute(
            """
            SELECT id FROM skill_versions
            WHERE skill_id = ? AND content_hash = ?
            """,
            (skill_id, content_hash),
        ).fetchone()
        if ver is None:
            conn.execute(
                """
                INSERT INTO skill_versions (
                    id, skill_id, content_hash, content, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _version_id(skill_id, content_hash),
                    skill_id,
                    content_hash,
                    stored,
                    now,
                    now,
                ),
            )
            stats.versions_added += 1
        else:
            conn.execute(
                "UPDATE skill_versions SET last_seen_at = ? WHERE id = ?",
                (now, ver["id"]),
            )

    conn.commit()
    return stats


def _tables_ready(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM sqlite_master
        WHERE type = 'table' AND name IN ('skills', 'skill_versions')
        """
    ).fetchone()
    return bool(row and int(row["c"]) == 2)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


@dataclass
class T3ViewStats:
    providers: int = 0
    skills_seen: int = 0
    unreadable: list[str] = field(default_factory=list)
    caches_dir_missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": self.providers,
            "skills_seen": self.skills_seen,
            "unreadable": list(self.unreadable),
            "caches_dir_missing": self.caches_dir_missing,
        }


def _t3_skill_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            value = entry.get("name") or entry.get("id") or entry.get("slug")
            if isinstance(value, str):
                names.append(value)
    return sorted({n for n in names if n})


def index_t3_visibility(
    conn: sqlite3.Connection,
    caches_dir: Path | None = None,
    *,
    now: str | None = None,
) -> T3ViewStats:
    """Record what t3 code reports it can see, per provider driver.

    t3 owns no skills directory; it delegates to each provider's home. An absent
    caches dir or an empty ``skills`` array is a benign "no data yet" state.
    """
    caches_dir = caches_dir if caches_dir is not None else default_t3_caches_dir()
    now = now or _utc_now()
    stats = T3ViewStats()
    if not _table_exists(conn, "skill_inventory_views"):
        return stats
    if not caches_dir.is_dir():
        stats.caches_dir_missing = True
        return stats

    prepared: list[tuple[str, dict[str, Any], list[str], str]] = []
    for path in sorted(caches_dir.glob("*.json")):
        provider = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            stats.unreadable.append(f"{provider}: {exc}")
            continue
        if not isinstance(data, dict):
            stats.unreadable.append(f"{provider}: cache root is not an object")
            continue
        names = _t3_skill_names(data.get("skills"))
        stats.providers += 1
        stats.skills_seen += len(names)
        prepared.append((provider, data, names, str(path.resolve())))

    for provider, data, names, source_path in prepared:
        note = None if names else "no skills reported by this provider yet"
        conn.execute(
            """
            INSERT INTO skill_inventory_views (
                id, viewer, provider, enabled, installed, status,
                skill_count, skill_names_json, source_path, observed_at, note
            ) VALUES (?, 't3', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(viewer, provider) DO UPDATE SET
                enabled = excluded.enabled,
                installed = excluded.installed,
                status = excluded.status,
                skill_count = excluded.skill_count,
                skill_names_json = excluded.skill_names_json,
                source_path = excluded.source_path,
                observed_at = excluded.observed_at,
                note = excluded.note
            """,
            (
                f"t3:{provider}",
                provider,
                1 if data.get("enabled") else 0,
                1 if data.get("installed") else 0,
                str(data.get("status")) if data.get("status") is not None else None,
                len(names),
                json.dumps(names),
                source_path,
                now,
                note,
            ),
        )
    conn.commit()
    return stats


def t3_visibility(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read back t3's cached view of skills. Empty is a valid state."""
    if not _table_exists(conn, "skill_inventory_views"):
        return {
            "providers": [],
            "skills_seen": 0,
            "note": "t3 visibility not indexed yet",
        }
    rows = conn.execute(
        """
        SELECT provider, enabled, installed, status, skill_count,
               skill_names_json, source_path, observed_at, note
        FROM skill_inventory_views
        WHERE viewer = 't3'
        ORDER BY provider
        """
    ).fetchall()
    providers = []
    seen: set[str] = set()
    for r in rows:
        try:
            names = json.loads(r["skill_names_json"])
        except json.JSONDecodeError:
            names = []
        if isinstance(names, list):
            seen |= {str(n) for n in names}
        providers.append(
            {
                "provider": r["provider"],
                "enabled": bool(r["enabled"]),
                "installed": bool(r["installed"]),
                "status": r["status"],
                "skill_count": int(r["skill_count"]),
                "skill_names": names if isinstance(names, list) else [],
                "source_path": r["source_path"],
                "observed_at": r["observed_at"],
                "note": r["note"],
            }
        )
    return {
        "providers": providers,
        "skills_seen": len(seen),
        "note": (
            "t3 code has no skills directory; it reaches skills through each "
            "provider driver's own home. These counts are t3's own cached view. "
            "Zero across all providers means t3 has not populated its cache yet, "
            "not that the skills are absent."
        ),
    }


def _inventory_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _tables_ready(conn):
        return []
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(skills)")}
    has_new = {
        "normalized_content_hash",
        "frontmatter_status",
        "frontmatter_error",
    } <= cols
    extra = (
        ", normalized_content_hash, frontmatter_status, frontmatter_error"
        if has_new
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT id, name, source, source_path, description,
               current_content_hash, first_seen_at, last_seen_at{extra}
        FROM skills
        ORDER BY name, source, source_path
        """
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item.setdefault("normalized_content_hash", None)
        item.setdefault("frontmatter_status", None)
        item.setdefault("frontmatter_error", None)
        out.append(item)
    return out


def _copy_ref(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "source": row["source"],
        "source_path": row["source_path"],
        "content_hash": row["current_content_hash"],
        "normalized_content_hash": row["normalized_content_hash"],
        "last_seen_at": row["last_seen_at"],
    }


def skill_inventory_report(
    conn: sqlite3.Connection,
    *,
    roots: list[tuple[str, Path]] | None = None,
) -> dict[str, Any]:
    """Cross-harness inventory with duplicate (same content) and conflict
    (same name, different content) detection.

    Duplicates and conflicts need different remedies: a duplicate is redundant
    and can be collapsed onto one canonical copy, whereas a conflict means two
    harnesses will load materially different instructions under one name.
    """
    rows = _inventory_rows(conn)
    roots = roots if roots is not None else default_skill_roots()
    missing_roots = [
        {"source": source, "path": str(path)}
        for source, path in roots
        if not path.is_dir()
    ]

    by_source: dict[str, int] = {}
    for row in rows:
        by_source[str(row["source"])] = by_source.get(str(row["source"]), 0) + 1

    exact: dict[str, list[dict[str, Any]]] = {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        exact.setdefault(str(row["current_content_hash"]), []).append(row)
        nh = row["normalized_content_hash"]
        if nh:
            normalized.setdefault(str(nh), []).append(row)
        by_name.setdefault(str(row["name"]), []).append(row)

    exact_groups = []
    for chash, group in exact.items():
        if len(group) < 2:
            continue
        exact_groups.append(
            {
                "kind": "exact_duplicate",
                "content_hash": chash,
                "names": sorted({str(r["name"]) for r in group}),
                "sources": sorted({str(r["source"]) for r in group}),
                "copies": [_copy_ref(r) for r in group],
                "copy_count": len(group),
                "redundant_copies": len(group) - 1,
                "cross_root": len({str(r["source"]) for r in group}) > 1,
                "remedy": (
                    "byte-identical SKILL.md at several paths; keep one canonical "
                    "copy and drop or link the rest"
                ),
            }
        )
    exact_groups.sort(key=lambda g: (-int(g["copy_count"]), str(g["content_hash"])))

    normalized_groups = []
    for nhash, group in normalized.items():
        if len(group) < 2:
            continue
        if len({str(r["current_content_hash"]) for r in group}) < 2:
            continue
        normalized_groups.append(
            {
                "kind": "normalized_duplicate",
                "normalized_content_hash": nhash,
                "names": sorted({str(r["name"]) for r in group}),
                "sources": sorted({str(r["source"]) for r in group}),
                "copies": [_copy_ref(r) for r in group],
                "copy_count": len(group),
                "redundant_copies": len(group) - 1,
                "cross_root": len({str(r["source"]) for r in group}) > 1,
                "remedy": (
                    "identical after whitespace normalization; differences are "
                    "cosmetic, collapse onto one canonical copy"
                ),
            }
        )
    normalized_groups.sort(
        key=lambda g: (-int(g["copy_count"]), str(g["normalized_content_hash"]))
    )

    conflicts = []
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        variants: dict[str, list[dict[str, Any]]] = {}
        for row in group:
            variants.setdefault(str(row["current_content_hash"]), []).append(row)
        if len(variants) < 2:
            continue
        conflicts.append(
            {
                "kind": "name_conflict",
                "name": name,
                "copy_count": len(group),
                "variant_count": len(variants),
                "sources": sorted({str(r["source"]) for r in group}),
                "variants": [
                    {
                        "content_hash": chash,
                        "copies": [_copy_ref(r) for r in members],
                    }
                    for chash, members in sorted(
                        variants.items(), key=lambda kv: -len(kv[1])
                    )
                ],
                "remedy": (
                    "same skill name resolves to different content depending on "
                    "harness; reconcile the variants before deduping"
                ),
            }
        )
    conflicts.sort(key=lambda g: (-int(g["variant_count"]), str(g["name"])))

    issues = [
        {
            "id": row["id"],
            "name": row["name"],
            "source": row["source"],
            "source_path": row["source_path"],
            "frontmatter_status": row["frontmatter_status"],
            "frontmatter_error": row["frontmatter_error"],
        }
        for row in rows
        if row["frontmatter_status"]
        and str(row["frontmatter_status"]) != FRONTMATTER_OK
    ]

    return {
        "totals": {
            "skills_indexed": len(rows),
            "distinct_names": len(by_name),
            "distinct_content_hashes": len(exact),
            "by_source": dict(sorted(by_source.items())),
            "exact_duplicate_groups": len(exact_groups),
            "normalized_duplicate_groups": len(normalized_groups),
            "name_conflicts": len(conflicts),
            "redundant_copies": sum(int(g["redundant_copies"]) for g in exact_groups),
            "frontmatter_issues": len(issues),
        },
        "exact_duplicates": exact_groups,
        "normalized_duplicates": normalized_groups,
        "name_conflicts": conflicts,
        "frontmatter_issues": issues,
        "missing_roots": missing_roots,
        "t3_visibility": t3_visibility(conn),
        "note": (
            "Duplicates are the same content at several paths. Conflicts are the "
            "same name with different content across roots, which is the more "
            "dangerous case because resolution depends on which harness loads it. "
            "Inventory is read-only; agentlog never edits harness config."
        ),
    }


def _duration_seconds(started: str | None, ended: str | None) -> int | None:
    if not started or not ended:
        return None
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((b - a).total_seconds()))


def _turn_kinds(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(x) for x in data}
    if isinstance(data, str):
        return {data}
    return set()


def _flags(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _rate_payload(
    *,
    skill_name: str,
    metric: str,
    numerator: int,
    denominator: int,
    ux_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rate = (numerator / denominator) if denominator > 0 else None
    phrasing = None
    if rate is not None:
        phrasing = (
            f"sessions where {skill_name} was active showed rate "
            f"{rate:.4f} (n={denominator})"
        )
    out: dict[str, Any] = {
        "metric": metric,
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "phrasing": phrasing,
    }
    if ux_coverage is not None:
        out["ux_coverage"] = ux_coverage
    return out


def _session_outcomes(
    conn: sqlite3.Connection,
    session_ids: list[str],
    *,
    metric_session_ids: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    if not session_ids:
        return {}
    metrics = {
        session_id: (metric_session_ids or {}).get(session_id, session_id)
        for session_id in session_ids
    }
    source_ids = sorted(set(metrics.values()))
    placeholders = ",".join("?" for _ in source_ids)
    sessions = {
        str(r["id"]): dict(r)
        for r in conn.execute(
            f"""
            SELECT id, started_at, ended_at FROM sessions
            WHERE id IN ({placeholders})
            """,
            source_ids,
        ).fetchall()
    }
    msg_counts = {
        str(r["session_id"]): int(r["c"])
        for r in conn.execute(
            f"""
            SELECT session_id, COUNT(*) AS c FROM messages
            WHERE session_id IN ({placeholders})
            GROUP BY session_id
            """,
            source_ids,
        ).fetchall()
    }
    tool_fail = {
        str(r["session_id"]): int(r["c"])
        for r in conn.execute(
            f"""
            SELECT session_id, COUNT(*) AS c FROM tool_events
            WHERE session_id IN ({placeholders}) AND success = 0
            GROUP BY session_id
            """,
            source_ids,
        ).fetchall()
    }
    window_totals = {
        str(r["session_id"]): int(r["c"])
        for r in conn.execute(
            f"""
            SELECT session_id, COUNT(*) AS c FROM exchange_windows
            WHERE session_id IN ({placeholders})
            GROUP BY session_id
            """,
            source_ids,
        ).fetchall()
    }
    ux_rows = conn.execute(
        f"""
        SELECT w.session_id, w.id AS window_id,
               u.turn_kinds_json, u.flags_json
        FROM exchange_windows w
        JOIN ux_observations u ON u.window_id = w.id
        WHERE w.session_id IN ({placeholders})
        """,
        source_ids,
    ).fetchall()
    ux_by_session: dict[str, list[Any]] = {sid: [] for sid in source_ids}
    labeled_windows: dict[str, set[str]] = {sid: set() for sid in source_ids}
    for r in ux_rows:
        sid = str(r["session_id"])
        labeled_windows[sid].add(str(r["window_id"]))
        ux_by_session[sid].append(r)

    out: dict[str, dict[str, Any]] = {}
    for sid in session_ids:
        metric_session_id = metrics[sid]
        s = sessions.get(metric_session_id, {})
        kinds: set[str] = set()
        flag_redirect = False
        for u in ux_by_session.get(metric_session_id, []):
            kinds |= _turn_kinds(u["turn_kinds_json"])
            flags = _flags(u["flags_json"])
            if flags.get("redirect_brake") or flags.get("had_redirect_brake"):
                flag_redirect = True
        windows_total = window_totals.get(metric_session_id, 0)
        windows_labeled = len(labeled_windows.get(metric_session_id, set()))
        out[sid] = {
            "duration_seconds": _duration_seconds(
                s.get("started_at"), s.get("ended_at")
            ),
            "message_count": msg_counts.get(metric_session_id, 0),
            "tool_failure_count": tool_fail.get(metric_session_id, 0),
            "windows_total": windows_total,
            "windows_labeled": windows_labeled,
            "has_redirect_or_brake": (
                "redirect_or_brake" in kinds or flag_redirect
            ),
            "has_correction": "correction" in kinds,
            "started_at": s.get("started_at"),
            "ended_at": s.get("ended_at"),
        }
    return out


def _aggregate_profile(
    *,
    skill_name: str,
    session_ids: list[str],
    session_meta: dict[str, dict[str, Any]],
    exposure_count: int,
    min_sessions: int,
) -> dict[str, Any]:
    n = len(session_ids)
    # Claude harness emits skill-inventory sessions (id prefix skills:) with
    # exposures but no messages. Keep them in session_count; outcome rates use
    # sessions that have at least one message.
    contentful = [
        s for s in session_ids if session_meta[s]["message_count"] > 0
    ]
    n_content = len(contentful)
    started = [
        session_meta[s]["started_at"]
        for s in session_ids
        if session_meta[s].get("started_at")
    ]
    ended = [
        session_meta[s]["ended_at"]
        for s in session_ids
        if session_meta[s].get("ended_at")
    ]
    durations = [
        session_meta[s]["duration_seconds"]
        for s in contentful
        if session_meta[s]["duration_seconds"] is not None
    ]
    msg_counts = [session_meta[s]["message_count"] for s in contentful]
    tool_fail_sessions = sum(
        1 for s in contentful if session_meta[s]["tool_failure_count"] > 0
    )
    tool_fail_total = sum(
        session_meta[s]["tool_failure_count"] for s in contentful
    )

    labeled_sessions = [
        s for s in contentful if session_meta[s]["windows_labeled"] > 0
    ]
    windows_labeled = sum(
        session_meta[s]["windows_labeled"] for s in contentful
    )
    windows_total = sum(session_meta[s]["windows_total"] for s in contentful)
    ux_coverage = {
        "windows_labeled": windows_labeled,
        "windows_total": windows_total,
        "sessions_with_labels": len(labeled_sessions),
        "sessions_total": n_content,
        "sessions_with_exposures": n,
        "sessions_without_messages": n - n_content,
        "coverage_ratio": (
            (windows_labeled / windows_total) if windows_total > 0 else None
        ),
    }

    redirect_n = sum(
        1 for s in labeled_sessions if session_meta[s]["has_redirect_or_brake"]
    )
    correction_n = sum(
        1 for s in labeled_sessions if session_meta[s]["has_correction"]
    )

    def _median(vals: list[float | int]) -> float | None:
        if not vals:
            return None
        ordered = sorted(vals)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[mid])
        return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0

    return {
        "exposure_count": exposure_count,
        "session_count": n,
        "sessions_with_messages": n_content,
        "sessions_without_messages": n - n_content,
        "date_range": {
            "first": min(started) if started else None,
            "last": max(started) if started else None,
            "last_ended": max(ended) if ended else None,
        },
        "insufficient_data": n_content < min_sessions,
        "min_sessions": min_sessions,
        "outcomes": {
            "median_duration_seconds": {
                "value": _median(durations),
                "denominator": len(durations),
                "sessions_total": n_content,
                "missing": n_content - len(durations),
            },
            "mean_message_count": {
                "value": (sum(msg_counts) / n_content) if n_content else None,
                "denominator": n_content,
            },
            "tool_failures": {
                "total_events": tool_fail_total,
                "sessions_with_failure": tool_fail_sessions,
                "rate": (tool_fail_sessions / n_content) if n_content else None,
                "denominator": n_content,
                "phrasing": (
                    f"sessions where {skill_name} was active showed rate "
                    f"{(tool_fail_sessions / n_content):.4f} (n={n_content})"
                    if n_content
                    else None
                ),
                "note": (
                    "Counts tool_events with success=0 only; NULL success is "
                    "treated as unknown, not failure. Denominator is exposure "
                    "sessions with at least one message."
                ),
            },
            "redirect_or_brake": _rate_payload(
                skill_name=skill_name,
                metric="sessions_with_redirect_or_brake",
                numerator=redirect_n,
                denominator=len(labeled_sessions),
                ux_coverage=ux_coverage,
            ),
            "correction": _rate_payload(
                skill_name=skill_name,
                metric="sessions_with_correction",
                numerator=correction_n,
                denominator=len(labeled_sessions),
                ux_coverage=ux_coverage,
            ),
        },
        "language_contract": {
            "allowed": (
                "sessions where X was active showed rate R (n=N)"
            ),
            "forbidden": [
                "improves",
                "causes",
                "better than",
                "effectiveness score",
            ],
        },
    }


def _load_indexed_skills(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _tables_ready(conn):
        return []
    rows = conn.execute(
        """
        SELECT id, name, source, source_path, description,
               current_content_hash, first_seen_at, last_seen_at, last_indexed_at
        FROM skills
        ORDER BY name, source
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _exposure_index(
    conn: sqlite3.Connection,
    *,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Map exposure skill_name -> {session_ids, exposure_count, last_fired}."""
    clauses = ["1=1"]
    params: list[Any] = []
    if start_iso is not None:
        clauses.append("COALESCE(s.started_at, '') >= ?")
        params.append(start_iso)
    if end_iso is not None:
        clauses.append("COALESCE(s.started_at, '') < ?")
        params.append(end_iso)
    where = " AND ".join(clauses)
    identity = build_identity_context(conn)
    shadow_ids = provider_root_shadow_ids(conn, context=identity)
    bindings: dict[str, str] = {}
    session_rows = conn.execute(
        f"""
        SELECT s.id, s.harness
        FROM sessions s
        WHERE {where}
        """,
        params,
    ).fetchall()
    for session in session_rows:
        session_id = str(session["id"])
        if session_id in shadow_ids:
            continue
        projection = logical_projection(
            conn,
            session_id,
            str(session["harness"]),
            context=identity,
        )
        metric_session_id = str(projection["transcript_session_id"] or session_id)
        bindings[metric_session_id] = session_id
    rows = conn.execute(
        f"""
        SELECT se.skill_name, se.session_id, s.started_at
        FROM skill_exposures se
        JOIN sessions s ON s.id = se.session_id
        WHERE {where}
        """,
        params,
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        metric_session_id = str(r["session_id"])
        logical_session_id = bindings.get(metric_session_id)
        if logical_session_id is None:
            continue
        name = str(r["skill_name"])
        bucket = out.setdefault(
            name,
            {
                "session_ids": set(),
                "metric_session_ids": {},
                "exposure_count": 0,
                "last_fired": None,
            },
        )
        bucket["exposure_count"] += 1
        bucket["session_ids"].add(logical_session_id)
        bucket["metric_session_ids"][logical_session_id] = metric_session_id
        started = r["started_at"]
        if started and (
            bucket["last_fired"] is None or str(started) > str(bucket["last_fired"])
        ):
            bucket["last_fired"] = started
    return out


def _match_exposures(
    aliases: set[str], exposure_index: dict[str, dict[str, Any]]
) -> tuple[set[str], dict[str, str], int, str | None, list[str]]:
    session_ids: set[str] = set()
    metric_session_ids: dict[str, str] = {}
    exposure_count = 0
    last_fired: str | None = None
    matched_names: list[str] = []
    for exp_name, bucket in exposure_index.items():
        bare = exp_name.split(":", 1)[-1] if ":" in exp_name else exp_name
        if exp_name in aliases or bare in aliases or exp_name.lower() in {
            a.lower() for a in aliases
        }:
            matched_names.append(exp_name)
            session_ids |= set(bucket["session_ids"])
            metric_session_ids.update(bucket["metric_session_ids"])
            exposure_count += int(bucket["exposure_count"])
            if bucket["last_fired"] and (
                last_fired is None or str(bucket["last_fired"]) > last_fired
            ):
                last_fired = str(bucket["last_fired"])
    return (
        session_ids,
        metric_session_ids,
        exposure_count,
        last_fired,
        sorted(set(matched_names)),
    )


def list_skill_profiles(
    conn: sqlite3.Connection,
    *,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    start_iso: str | None = None,
    end_iso: str | None = None,
    include_unmatched_exposures: bool = True,
) -> dict[str, Any]:
    """Build per-skill descriptive profiles for the API list endpoint."""
    indexed = _load_indexed_skills(conn)
    hash_counts: dict[str, int] = {}
    name_hashes: dict[str, set[str]] = {}
    for row in indexed:
        chash = str(row["current_content_hash"])
        hash_counts[chash] = hash_counts.get(chash, 0) + 1
        name_hashes.setdefault(str(row["name"]), set()).add(chash)
    exposure_index = _exposure_index(conn, start_iso=start_iso, end_iso=end_iso)
    claimed_exposure_names: set[str] = set()
    items: list[dict[str, Any]] = []

    all_session_ids: set[str] = set()
    all_metric_session_ids: dict[str, str] = {}
    pending: list[
        tuple[dict[str, Any], set[str], dict[str, str], int, str | None, list[str]]
    ] = []
    for skill in indexed:
        aliases = skill_aliases(
            str(skill["name"]),
            str(skill["source"]),
            Path(str(skill["source_path"])),
        )
        session_ids, metric_session_ids, exposure_count, last_fired, matched = _match_exposures(
            aliases, exposure_index
        )
        claimed_exposure_names.update(matched)
        pending.append(
            (skill, session_ids, metric_session_ids, exposure_count, last_fired, matched)
        )
        all_session_ids |= session_ids
        all_metric_session_ids.update(metric_session_ids)

    unmatched_pending: list[tuple[str, set[str], dict[str, str], int, str | None]] = []
    if include_unmatched_exposures:
        for exp_name, bucket in exposure_index.items():
            if exp_name in claimed_exposure_names:
                continue
            sids = set(bucket["session_ids"])
            unmatched_pending.append(
                (
                    exp_name,
                    sids,
                    dict(bucket["metric_session_ids"]),
                    int(bucket["exposure_count"]),
                    bucket["last_fired"],
                )
            )
            all_session_ids |= sids
            all_metric_session_ids.update(bucket["metric_session_ids"])

    session_meta = _session_outcomes(
        conn,
        sorted(all_session_ids),
        metric_session_ids=all_metric_session_ids,
    )

    for skill, session_ids, _metric_session_ids, exposure_count, last_fired, matched in pending:
        ordered = sorted(session_ids)
        profile = _aggregate_profile(
            skill_name=str(skill["name"]),
            session_ids=ordered,
            session_meta=session_meta,
            exposure_count=exposure_count,
            min_sessions=min_sessions,
        )
        items.append(
            {
                "id": skill["id"],
                "name": skill["name"],
                "source": skill["source"],
                "source_path": skill["source_path"],
                "description": skill["description"],
                "content_hash": skill["current_content_hash"],
                "first_seen_at": skill["first_seen_at"],
                "last_seen_at": skill["last_seen_at"],
                "indexed": True,
                "duplicate_copies": hash_counts.get(
                    str(skill["current_content_hash"]), 1
                )
                - 1,
                "name_conflict": len(name_hashes.get(str(skill["name"]), set())) > 1,
                "matched_exposure_names": matched,
                "skill": skill["name"],
                "fires": exposure_count,
                "sessions": len(ordered),
                "last_fired": last_fired,
                "profile": profile,
            }
        )

    for exp_name, session_ids, _metric_session_ids, exposure_count, last_fired in unmatched_pending:
        ordered = sorted(session_ids)
        profile = _aggregate_profile(
            skill_name=exp_name,
            session_ids=ordered,
            session_meta=session_meta,
            exposure_count=exposure_count,
            min_sessions=min_sessions,
        )
        items.append(
            {
                "id": None,
                "name": exp_name,
                "source": None,
                "source_path": None,
                "description": None,
                "content_hash": None,
                "first_seen_at": None,
                "last_seen_at": None,
                "indexed": False,
                "duplicate_copies": 0,
                "name_conflict": False,
                "matched_exposure_names": [exp_name],
                "skill": exp_name,
                "fires": exposure_count,
                "sessions": len(ordered),
                "last_fired": last_fired,
                "profile": profile,
            }
        )

    items.sort(
        key=lambda it: (-int(it["sessions"]), -int(it["fires"]), str(it["name"]))
    )
    activations = sum(int(i["fires"]) for i in items)
    distinct_fired = sum(1 for i in items if int(i["fires"]) > 0)
    return {
        "indexed_count": len(indexed),
        "activations": activations,
        "distinct_fired": distinct_fired,
        "duplicates": {
            "exact_duplicate_groups": sum(1 for c in hash_counts.values() if c > 1),
            "redundant_copies": sum(c - 1 for c in hash_counts.values() if c > 1),
            "name_conflicts": sum(1 for h in name_hashes.values() if len(h) > 1),
            "detail_endpoint": "/api/skills/duplicates",
        },
        "items": items,
        "note": (
            "Profiles are descriptive co-occurrence stats for sessions where each "
            "skill was active. Rates always include denominators. Skills with "
            f"fewer than {min_sessions} exposure sessions are flagged "
            "insufficient_data for comparison. No causal effectiveness claims."
        ),
    }


def skill_detail(
    conn: sqlite3.Connection,
    skill_id: str,
    *,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    start_iso: str | None = None,
    end_iso: str | None = None,
) -> dict[str, Any] | None:
    if not _tables_ready(conn):
        return None
    row = conn.execute(
        """
        SELECT id, name, source, source_path, description,
               current_content_hash, first_seen_at, last_seen_at, last_indexed_at
        FROM skills WHERE id = ?
        """,
        (skill_id,),
    ).fetchone()
    if row is None:
        return None
    skill = dict(row)
    versions = [
        {
            "id": r["id"],
            "content_hash": r["content_hash"],
            "content_bytes": len(r["content"] or ""),
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
            "is_current": r["content_hash"] == skill["current_content_hash"],
        }
        for r in conn.execute(
            """
            SELECT id, content_hash, content, first_seen_at, last_seen_at
            FROM skill_versions
            WHERE skill_id = ?
            ORDER BY first_seen_at DESC
            """,
            (skill_id,),
        ).fetchall()
    ]
    aliases = skill_aliases(
        str(skill["name"]),
        str(skill["source"]),
        Path(str(skill["source_path"])),
    )
    exposure_index = _exposure_index(conn, start_iso=start_iso, end_iso=end_iso)
    session_ids, metric_session_ids, exposure_count, last_fired, matched = _match_exposures(
        aliases, exposure_index
    )
    ordered = sorted(session_ids)
    session_meta = _session_outcomes(
        conn,
        ordered,
        metric_session_ids=metric_session_ids,
    )
    profile = _aggregate_profile(
        skill_name=str(skill["name"]),
        session_ids=ordered,
        session_meta=session_meta,
        exposure_count=exposure_count,
        min_sessions=min_sessions,
    )
    exposure_sessions = []
    for sid in ordered:
        meta = session_meta[sid]
        exposure_sessions.append(
            {
                "session_id": sid,
                "transcript_session_id": metric_session_ids.get(sid, sid),
                "started_at": meta.get("started_at"),
                "ended_at": meta.get("ended_at"),
                "duration_seconds": meta.get("duration_seconds"),
                "message_count": meta.get("message_count"),
                "tool_failure_count": meta.get("tool_failure_count"),
                "windows_labeled": meta.get("windows_labeled"),
                "windows_total": meta.get("windows_total"),
                "has_redirect_or_brake": meta.get("has_redirect_or_brake"),
                "has_correction": meta.get("has_correction"),
            }
        )
    return {
        "id": skill["id"],
        "name": skill["name"],
        "source": skill["source"],
        "source_path": skill["source_path"],
        "description": skill["description"],
        "content_hash": skill["current_content_hash"],
        "first_seen_at": skill["first_seen_at"],
        "last_seen_at": skill["last_seen_at"],
        "last_indexed_at": skill["last_indexed_at"],
        "aliases": sorted(aliases),
        "matched_exposure_names": matched,
        "versions": versions,
        "last_fired": last_fired,
        "profile": profile,
        "exposure_sessions": exposure_sessions,
        "note": (
            "Descriptive co-occurrence only. "
            "sessions where X was active showed rate R (n=N)."
        ),
    }
