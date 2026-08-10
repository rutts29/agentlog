from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from agentlog.analysis.extractors.storage import PURPOSE_RESTORE
from agentlog.analysis.extractors.taxonomy import EXTRACTOR_NAME_UX, EXTRACTOR_VERSION
from agentlog.analysis.windows import (
    compute_window_content_hash,
    normalize_window_text,
)


@dataclass
class DiskLabel:
    packet_id: str
    old_window_id: str
    harness: str | None
    user_text: str
    assistant_text: str
    label: dict[str, Any]
    source_path: str


@dataclass
class MatchResult:
    disk: DiskLabel
    method: str
    new_window_id: str
    content_hash: str


@dataclass
class RestoreCensus:
    total_disk: int = 0
    restored_by_content_hash: int = 0
    restored_by_evidence_quote: int = 0
    restored_by_harness_text: int = 0
    unrestorable: list[str] = field(default_factory=list)
    unrestorable_reasons: dict[str, str] = field(default_factory=dict)
    unrestorable_content_changed: int = 0
    unrestorable_rekeyed: int = 0
    parse_generation: dict[str, Any] = field(default_factory=dict)
    written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_disk": self.total_disk,
            "restored_by_content_hash": self.restored_by_content_hash,
            "restored_by_evidence_quote": self.restored_by_evidence_quote,
            "restored_by_harness_text": self.restored_by_harness_text,
            "restored_total": (
                self.restored_by_content_hash
                + self.restored_by_evidence_quote
                + self.restored_by_harness_text
            ),
            "unrestorable_count": len(self.unrestorable),
            "unrestorable_packet_ids": sorted(set(self.unrestorable)),
            "unrestorable_reasons": dict(self.unrestorable_reasons),
            "unrestorable_content_changed": self.unrestorable_content_changed,
            "unrestorable_rekeyed": self.unrestorable_rekeyed,
            "parse_generation": dict(self.parse_generation),
            "written": self.written,
        }


def load_disk_labels(run_dir: Path) -> list[DiskLabel]:
    """Load labels from results/ and results_overlap/, keyed by packet window text."""
    packets_by_id: dict[str, dict[str, Any]] = {}
    packets_dir = run_dir / "packets"
    if packets_dir.is_dir():
        for path in packets_dir.glob("pkt_*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            packets_by_id[str(data.get("packet_id") or path.stem)] = data

    out: list[DiskLabel] = []
    seen_windows: set[str] = set()
    for sub in ("results", "results_overlap"):
        results_dir = run_dir / sub
        if not results_dir.is_dir():
            continue
        for path in sorted(results_dir.glob("pkt_*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            packet_id = str(data.get("packet_id") or path.stem)
            pkt = packets_by_id.get(packet_id, {})
            pkt_windows = {
                str(w.get("window_id")): w
                for w in (pkt.get("windows") or [])
                if isinstance(w, dict) and w.get("window_id")
            }
            for win in data.get("windows") or []:
                if not isinstance(win, dict):
                    continue
                wid = str(win.get("window_id") or "")
                if not wid or wid in seen_windows:
                    continue
                seen_windows.add(wid)
                meta = pkt_windows.get(wid, {})
                out.append(
                    DiskLabel(
                        packet_id=packet_id,
                        old_window_id=wid,
                        harness=(
                            str(meta["harness"])
                            if meta.get("harness") is not None
                            else None
                        ),
                        user_text=str(meta.get("user") or ""),
                        assistant_text=str(meta.get("assistant") or ""),
                        label=win,
                        source_path=str(path),
                    )
                )
    return out


def _live_window_index(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT w.id, w.session_id, w.content_hash, s.harness,
               req.text AS req_text, resp.text AS resp_text,
               req.seq AS req_seq
        FROM exchange_windows w
        JOIN sessions s ON s.id = w.session_id
        JOIN messages req ON req.id = w.request_message_id
        JOIN messages resp ON resp.id = w.response_message_id
        """
    ).fetchall()
    # next non-plumbing user after each request (evidence spans often quote it)
    users_by_session: dict[str, list[tuple[int, str]]] = {}
    for r in conn.execute(
        """
        SELECT session_id, seq, text FROM messages
        WHERE role = 'user' AND COALESCE(is_tool_plumbing, 0) = 0
        ORDER BY session_id, seq
        """
    ):
        users_by_session.setdefault(str(r["session_id"]), []).append(
            (int(r["seq"]), str(r["text"] or ""))
        )

    def next_user_text(session_id: str, req_seq: int) -> str:
        users = users_by_session.get(session_id) or []
        for seq, text in users:
            if seq > req_seq:
                return text
        return ""

    out: list[dict[str, Any]] = []
    for r in rows:
        req_text = str(r["req_text"] or "")
        resp_text = str(r["resp_text"] or "")
        session_id = str(r["session_id"])
        nxt = next_user_text(session_id, int(r["req_seq"]))
        ch = str(r["content_hash"] or "") or compute_window_content_hash(
            session_id, req_text, resp_text
        )
        out.append(
            {
                "id": str(r["id"]),
                "session_id": session_id,
                "content_hash": ch,
                "harness": str(r["harness"]),
                "req_text": req_text,
                "resp_text": resp_text,
                "next_user_text": nxt,
                "req_norm": normalize_window_text(req_text),
                "resp_norm": normalize_window_text(resp_text),
            }
        )
    return out


def _strip_truncation(text: str) -> str:
    t = text or ""
    if t.endswith("…"):
        return t[:-1]
    return t


def match_label(
    disk: DiskLabel,
    live: list[dict[str, Any]],
    *,
    by_content_hash: dict[str, list[dict[str, Any]]],
    by_old_id: dict[str, dict[str, Any]],
) -> MatchResult | tuple[None, str]:
    # 1) Exact content-hash from packet texts + each candidate session.
    if disk.user_text and disk.assistant_text:
        user_full = _strip_truncation(disk.user_text)
        asst_full = _strip_truncation(disk.assistant_text)
        # Prefer exact full-text equality (packet may be truncated with …).
        text_hits = [
            w
            for w in live
            if (
                (disk.harness is None or w["harness"] == disk.harness)
                and (
                    w["req_norm"] == normalize_window_text(disk.user_text)
                    or (
                        disk.user_text.endswith("…")
                        and w["req_norm"].startswith(normalize_window_text(user_full))
                    )
                )
                and (
                    w["resp_norm"] == normalize_window_text(disk.assistant_text)
                    or (
                        disk.assistant_text.endswith("…")
                        and w["resp_norm"].startswith(normalize_window_text(asst_full))
                    )
                )
            )
        ]
        if len(text_hits) == 1:
            w = text_hits[0]
            return MatchResult(
                disk=disk,
                method="content_hash",
                new_window_id=w["id"],
                content_hash=w["content_hash"],
            )
        # Ambiguous or empty text hits: fall through to evidence quotes.

        # Content hash using each live session_id that shares harness.
        if not (disk.user_text.endswith("…") or disk.assistant_text.endswith("…")):
            hash_hits: list[dict[str, Any]] = []
            seen: set[str] = set()
            for w in live:
                if disk.harness is not None and w["harness"] != disk.harness:
                    continue
                ch = compute_window_content_hash(
                    w["session_id"], disk.user_text, disk.assistant_text
                )
                if ch == w["content_hash"] and w["id"] not in seen:
                    hash_hits.append(w)
                    seen.add(w["id"])
            if len(hash_hits) == 1:
                w = hash_hits[0]
                return MatchResult(
                    disk=disk,
                    method="content_hash",
                    new_window_id=w["id"],
                    content_hash=w["content_hash"],
                )
            # Ambiguous hash: fall through rather than guessing.

    # Legacy id still present (pre-migration or identical content-hash collision).
    if disk.old_window_id in by_old_id:
        w = by_old_id[disk.old_window_id]
        return MatchResult(
            disk=disk,
            method="content_hash",
            new_window_id=w["id"],
            content_hash=w["content_hash"],
        )

    # 2) Verbatim evidence quotes — every quote must appear in the matching role text.
    spans = disk.label.get("spans") or []
    quotes: list[tuple[str, str]] = []
    for sp in spans:
        if not isinstance(sp, dict):
            continue
        quote = str(sp.get("quote") or "")
        role = str(sp.get("role") or "")
        if quote:
            quotes.append((role, quote))
    if quotes:
        quote_hits: list[dict[str, Any]] = []
        for w in live:
            if disk.harness is not None and w["harness"] != disk.harness:
                continue
            if _window_has_all_quotes(w, quotes):
                quote_hits.append(w)
        if len(quote_hits) == 1:
            w = quote_hits[0]
            return MatchResult(
                disk=disk,
                method="evidence_quote",
                new_window_id=w["id"],
                content_hash=w["content_hash"],
            )
        if len(quote_hits) > 1:
            return None, "rekeyed_ambiguous_evidence"
        # Quotes present on the label but no live window contains them all.
        if _quotes_missing_from_corpus(quotes, live, disk.harness):
            return None, "content_changed"
        return None, "content_changed"

    # 3) Harness + normalized user prefix heuristic (strict uniqueness only).
    if disk.user_text:
        prefix = normalize_window_text(_strip_truncation(disk.user_text))[:200]
        if len(prefix) >= 40:
            heur_hits = [
                w
                for w in live
                if (disk.harness is None or w["harness"] == disk.harness)
                and w["req_norm"].startswith(prefix)
            ]
            if len(heur_hits) == 1:
                w = heur_hits[0]
                return MatchResult(
                    disk=disk,
                    method="harness_text",
                    new_window_id=w["id"],
                    content_hash=w["content_hash"],
                )
            if len(heur_hits) > 1:
                return None, "rekeyed_ambiguous_harness_text"

    return None, "rekeyed_no_match"


def _haystack_for_role(w: dict[str, Any], role: str) -> str:
    if role == "assistant":
        return w["resp_text"]
    if role == "next_user":
        return w.get("next_user_text") or ""
    if role == "user":
        return w["req_text"]
    return "\n".join(
        [
            w["req_text"],
            w["resp_text"],
            w.get("next_user_text") or "",
        ]
    )


def _window_has_all_quotes(
    w: dict[str, Any], quotes: list[tuple[str, str]]
) -> bool:
    for role, quote in quotes:
        if quote not in _haystack_for_role(w, role):
            return False
    return True


def _quotes_missing_from_corpus(
    quotes: list[tuple[str, str]],
    live: list[dict[str, Any]],
    harness: str | None,
) -> bool:
    """True when at least one evidence quote appears nowhere in the harness corpus."""
    if not quotes:
        return False
    for role, quote in quotes:
        found = False
        for w in live:
            if harness is not None and w["harness"] != harness:
                continue
            if quote in _haystack_for_role(w, role) or quote in (
                w["req_text"] + "\n" + w["resp_text"] + "\n" + (w.get("next_user_text") or "")
            ):
                found = True
                break
        if not found:
            return True
    return False


def match_all(
    conn: sqlite3.Connection, disks: Iterable[DiskLabel]
) -> tuple[list[MatchResult], list[tuple[DiskLabel, str]]]:
    live = _live_window_index(conn)
    by_old_id = {w["id"]: w for w in live}
    by_content_hash: dict[str, list[dict[str, Any]]] = {}
    for w in live:
        by_content_hash.setdefault(w["content_hash"], []).append(w)

    matched: list[MatchResult] = []
    failed: list[tuple[DiskLabel, str]] = []
    for disk in disks:
        result = match_label(
            disk,
            live,
            by_content_hash=by_content_hash,
            by_old_id=by_old_id,
        )
        if isinstance(result, MatchResult):
            matched.append(result)
        else:
            failed.append((disk, result[1]))
    return matched, failed


def _ensure_restore_run(conn: sqlite3.Connection) -> str:
    now = datetime.now(timezone.utc).isoformat()
    run_id = hashlib.sha1(f"ux-restore:{now}".encode()).hexdigest()[:24]
    conn.execute(
        """
        INSERT INTO derivation_runs (
            id, kind, extractor_name, extractor_version, model, prompt_hash,
            started_at, completed_at, status, meta_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "ux_llm",
            EXTRACTOR_NAME_UX,
            EXTRACTOR_VERSION,
            "restore-from-disk",
            "restore",
            now,
            now,
            "completed",
            json.dumps({"source": "extraction-run-001-restore"}),
        ),
    )
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(derivation_runs)")}
    if "purpose" in cols:
        conn.execute(
            "UPDATE derivation_runs SET purpose = ? WHERE id = ?",
            (PURPOSE_RESTORE, run_id),
        )
    return run_id


def write_restored(
    conn: sqlite3.Connection,
    matches: list[MatchResult],
    *,
    run_id: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    if run_id is None:
        run_id = _ensure_restore_run(conn)
    written = 0
    for m in matches:
        lab = m.disk.label
        row_id = hashlib.sha1(
            f"{run_id}:{m.new_window_id}".encode()
        ).hexdigest()[:24]
        raw = {
            **lab,
            "window_id": m.new_window_id,
            "restore": {
                "method": m.method,
                "old_window_id": m.disk.old_window_id,
                "packet_id": m.disk.packet_id,
            },
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO ux_observations (
                id, window_id, run_id, turn_kinds_json, user_stance, agent_stance,
                prior_outcome, flags_json, spans_json, confidence_json,
                abstain_reasons_json, novel_observations_json,
                extractor_name, extractor_version, model, prompt_hash,
                batch_size, raw_json, created_at,
                content_hash, link_status, orphaned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                m.new_window_id,
                run_id,
                json.dumps(lab.get("turn_kind") or []),
                lab.get("user_stance"),
                lab.get("agent_stance"),
                lab.get("prior_outcome"),
                json.dumps(lab.get("flags") or {}),
                json.dumps(lab.get("spans") or []),
                json.dumps(lab.get("confidence") or {}),
                json.dumps(lab.get("abstain_reasons") or []),
                json.dumps(lab.get("novel_observations") or []),
                EXTRACTOR_NAME_UX,
                EXTRACTOR_VERSION,
                "restore-from-disk",
                "restore",
                1,
                json.dumps(raw),
                now,
                m.content_hash,
                "linked",
                None,
            ),
        )
        written += 1
    conn.commit()
    return written


def _parse_generation(conn: sqlite3.Connection) -> dict[str, Any]:
    from agentlog.config import PARSER_VERSION

    by_ver = {
        str(r["parser_version"]): int(r["c"])
        for r in conn.execute(
            "SELECT parser_version, COUNT(*) AS c FROM artifacts GROUP BY 1"
        )
    }
    return {
        "code_parser_version": PARSER_VERSION,
        "artifacts_by_parser_version": by_ver,
        "exchange_windows": int(
            conn.execute("SELECT COUNT(*) AS c FROM exchange_windows").fetchone()["c"]
        ),
        "messages": int(
            conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        ),
        "sessions": int(
            conn.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"]
        ),
        "tool_events_null_message_id": int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM tool_events WHERE message_id IS NULL"
            ).fetchone()["c"]
        ),
    }


def restore_from_run_dir(
    conn: sqlite3.Connection,
    run_dir: Path,
    *,
    dry_run: bool = False,
) -> RestoreCensus:
    disks = load_disk_labels(run_dir)
    census = RestoreCensus(
        total_disk=len(disks),
        parse_generation=_parse_generation(conn),
    )
    matched, failed = match_all(conn, disks)
    for m in matched:
        if m.method == "content_hash":
            census.restored_by_content_hash += 1
        elif m.method == "evidence_quote":
            census.restored_by_evidence_quote += 1
        elif m.method == "harness_text":
            census.restored_by_harness_text += 1
    for disk, reason in failed:
        key = f"{disk.packet_id}:{disk.old_window_id}"
        census.unrestorable.append(disk.packet_id)
        census.unrestorable_reasons[key] = reason
        if reason == "content_changed":
            census.unrestorable_content_changed += 1
        else:
            census.unrestorable_rekeyed += 1
    if not dry_run:
        census.written = write_restored(conn, matched)
    else:
        census.written = 0
    return census
