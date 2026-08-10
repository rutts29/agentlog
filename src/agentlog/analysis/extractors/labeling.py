from __future__ import annotations

import json
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from agentlog.safety.write_guard import assert_writable

# Gate-relevant keys — abstain is as easy as any positive label.
KEY_MAP = {
    "r": "redirect_or_brake",
    "c": "correction",
    "s": "soft_approval",
    "d": "dont_act_yet",
    "p": "pushing_back",
    "f": "frustrated",
    "u": "abstain",
}

HELP_TEXT = """
Keys:
  r  redirect_or_brake     c  correction
  s  soft_approval         d  dont_act_yet
  p  agent pushing_back    f  frustrated
  u  abstain / unsure      n  add note
  e  expand / collapse     b  previous window
  ?  help                  q  quit (progress saved)
"""

CONDENSE_USER = 400
CONDENSE_ASSISTANT = 280
CONDENSE_NEXT = 500
CONDENSE_TOOLS = 12


def read_key(stdin: TextIO | None = None) -> str:
    """Single keystroke via termios (stdlib only)."""
    stream = stdin or sys.stdin
    fd = stream.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = stream.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch


def _clip(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def format_window(row: dict[str, Any], *, expanded: bool = False) -> str:
    payload = row.get("payload") or {}
    user = str(payload.get("user") or "")
    assistant = str(payload.get("assistant") or "")
    next_user = str(payload.get("next_user") or "")
    tools = list(payload.get("tool_timeline") or [])
    harness = row.get("harness") or payload.get("harness") or "?"
    wid = row.get("window_id") or payload.get("window_id") or "?"

    if expanded:
        tool_block = "\n".join(f"  - {t}" for t in tools) or "  (none)"
        return (
            f"window_id={wid}  harness={harness}\n"
            f"── USER ──\n{user or '(empty)'}\n"
            f"── AGENT (full) ──\n{assistant or '(empty)'}\n"
            f"── TOOLS ──\n{tool_block}\n"
            f"── NEXT USER ──\n{next_user or '(empty)'}\n"
        )

    tool_preview = tools[:CONDENSE_TOOLS]
    more = len(tools) - len(tool_preview)
    tool_line = ", ".join(str(t).split("|")[0] for t in tool_preview)
    if more > 0:
        tool_line += f" (+{more})"
    asst = _clip(assistant.replace("\n", " "), CONDENSE_ASSISTANT)
    return (
        f"window_id={wid}  harness={harness}\n"
        f"── USER ──\n{_clip(user, CONDENSE_USER) or '(empty)'}\n"
        f"── AGENT (condensed) ──\n{asst or '(empty)'}\n"
        f"── TOOLS ──\n{tool_line or '(none)'}\n"
        f"── NEXT USER ──\n{_clip(next_user, CONDENSE_NEXT) or '(empty)'}\n"
    )


def empty_labels() -> dict[str, Any]:
    return {
        "turn_kind": [],
        "user_stance": None,
        "agent_stance": None,
        "prior_outcome": None,
        "flags": {},
        "notes": "",
    }


def apply_label_key(labels: dict[str, Any], key: str) -> dict[str, Any]:
    """Map a single keystroke onto the gold label schema."""
    action = KEY_MAP.get(key)
    if action is None:
        raise KeyError(key)
    out = dict(labels)
    out.setdefault("turn_kind", [])
    out["turn_kind"] = list(out.get("turn_kind") or [])
    if action == "abstain":
        out["turn_kind"] = []
        out["user_stance"] = "abstain"
        out["agent_stance"] = None
        out["prior_outcome"] = None
        return out
    if action == "pushing_back":
        out["agent_stance"] = "pushing_back"
        return out
    if action == "frustrated":
        out["user_stance"] = "frustrated"
        if "frustrated" not in out["turn_kind"]:
            # frustrated is primarily a stance; keep turn_kind free unless already set
            pass
        return out
    # turn_kind gate labels — replace prior gate kinds for speed (single primary)
    gate_kinds = {
        "redirect_or_brake",
        "correction",
        "soft_approval",
        "dont_act_yet",
    }
    kept = [k for k in out["turn_kind"] if k not in gate_kinds]
    kept.append(action)
    out["turn_kind"] = kept
    if action == "correction":
        out["user_stance"] = "correcting"
    elif action == "redirect_or_brake":
        out["user_stance"] = "redirecting"
    elif action == "soft_approval":
        out["user_stance"] = "approving"
    return out


def load_pack_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_gold_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_id[str(row["window_id"])] = row
    return by_id


def write_gold_rows(path: Path, rows_by_id: dict[str, dict[str, Any]]) -> None:
    """Rewrite gold file sorted by original insertion / window_id order of keys."""
    path = assert_writable(path, purpose="gold labels")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for wid in rows_by_id:
            f.write(json.dumps(rows_by_id[wid], ensure_ascii=False) + "\n")
    tmp.replace(path)


@dataclass
class LabelSession:
    pack_path: Path
    gold_path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)
    gold: dict[str, dict[str, Any]] = field(default_factory=dict)
    index: int = 0
    expanded: bool = False
    started_at: float = field(default_factory=time.time)
    order: list[str] = field(default_factory=list)

    @classmethod
    def open(cls, pack_path: Path, gold_path: Path) -> LabelSession:
        rows = load_pack_rows(pack_path)
        gold = load_gold_rows(gold_path)
        order = [str(r["window_id"]) for r in rows]
        # Resume at first unlabeled in pack order.
        index = 0
        for i, wid in enumerate(order):
            g = gold.get(wid)
            if g is None or g.get("label_status") == "unlabeled":
                index = i
                break
        else:
            index = max(0, len(order) - 1)
        return cls(
            pack_path=pack_path,
            gold_path=gold_path,
            rows=rows,
            gold=gold,
            index=index,
            order=order,
        )

    @property
    def current(self) -> dict[str, Any]:
        return self.rows[self.index]

    @property
    def labeled_count(self) -> int:
        return sum(
            1
            for wid in self.order
            if self.gold.get(wid, {}).get("label_status") == "labeled"
        )

    def progress_line(self) -> str:
        elapsed = int(time.time() - self.started_at)
        return (
            f"[{self.index + 1}/{len(self.rows)}]  "
            f"labeled={self.labeled_count}  "
            f"elapsed={elapsed}s"
        )

    def save_current(
        self,
        labels: dict[str, Any],
        *,
        status: str = "labeled",
    ) -> None:
        row = self.current
        wid = str(row["window_id"])
        out = {
            "window_id": wid,
            "harness": row.get("harness"),
            "session_id": row.get("session_id"),
            "payload": row.get("payload"),
            "labels": labels,
            "label_status": status,
        }
        self.gold[wid] = out
        # Preserve pack order in gold file.
        ordered = {w: self.gold[w] for w in self.order if w in self.gold}
        write_gold_rows(self.gold_path, ordered)

    def apply_and_advance(self, key: str) -> None:
        base = empty_labels()
        existing = self.gold.get(str(self.current["window_id"]), {}).get("labels")
        if isinstance(existing, dict) and existing.get("notes"):
            base["notes"] = existing["notes"]
        labels = apply_label_key(base, key)
        self.save_current(labels)
        if self.index < len(self.rows) - 1:
            self.index += 1
        self.expanded = False

    def go_back(self) -> None:
        if self.index > 0:
            self.index -= 1
        self.expanded = False

    def set_note(self, note: str) -> None:
        wid = str(self.current["window_id"])
        existing = self.gold.get(wid)
        if existing and existing.get("label_status") == "labeled":
            labels = dict(existing.get("labels") or empty_labels())
        else:
            labels = empty_labels()
        labels["notes"] = note
        # Note alone does not complete labeling.
        if existing and existing.get("label_status") == "labeled":
            self.save_current(labels, status="labeled")
        else:
            self.save_current(labels, status="unlabeled")

    def distribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for wid in self.order:
            g = self.gold.get(wid)
            if not g or g.get("label_status") != "labeled":
                continue
            labels = g.get("labels") or {}
            kinds = list(labels.get("turn_kind") or [])
            if labels.get("agent_stance") == "pushing_back":
                kinds.append("pushing_back")
            if labels.get("user_stance") == "frustrated":
                kinds.append("frustrated")
            if labels.get("user_stance") == "abstain" and not kinds:
                kinds = ["abstain"]
            if not kinds:
                kinds = ["(empty)"]
            for k in kinds:
                counts[k] = counts.get(k, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def run_labeling_loop(
    pack_path: Path,
    gold_path: Path,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    read_key_fn: Callable[[], str] | None = None,
    read_line_fn: Callable[[], str] | None = None,
) -> LabelSession:
    """Interactive labeling. Never shows model predictions."""
    out = stdout or sys.stdout
    get_key = read_key_fn or (lambda: read_key(stdin))
    get_line = read_line_fn or (lambda: (stdin or sys.stdin).readline())

    session = LabelSession.open(pack_path, gold_path)
    out.write(
        "Hand labeling — no model predictions are shown.\n"
        f"Pack: {pack_path}\nGold: {gold_path}\n"
        f"{HELP_TEXT}\n"
    )
    out.flush()

    while True:
        if not session.rows:
            out.write("Empty pack.\n")
            break
        row = session.current
        out.write("\n" + "=" * 60 + "\n")
        out.write(session.progress_line() + "\n")
        out.write(format_window(row, expanded=session.expanded))
        existing = session.gold.get(str(row["window_id"]))
        if existing and existing.get("label_status") == "labeled":
            labs = existing.get("labels") or {}
            out.write(f"(current gold) turn_kind={labs.get('turn_kind')} "
                      f"agent_stance={labs.get('agent_stance')} "
                      f"user_stance={labs.get('user_stance')}\n")
            if labs.get("notes"):
                out.write(f"(note) {labs['notes']}\n")
        out.write("label> ")
        out.flush()
        try:
            ch = get_key()
        except KeyboardInterrupt:
            out.write("\nInterrupted — progress saved.\n")
            break
        out.write(ch + "\n")
        out.flush()

        if ch in KEY_MAP:
            session.apply_and_advance(ch)
            if session.labeled_count >= len(session.rows):
                # Check if we just finished last
                if all(
                    session.gold.get(w, {}).get("label_status") == "labeled"
                    for w in session.order
                ):
                    out.write("\nAll windows labeled.\n")
                    break
            continue
        if ch == "b":
            session.go_back()
            continue
        if ch == "e":
            session.expanded = not session.expanded
            continue
        if ch == "n":
            out.write("note> ")
            out.flush()
            note = get_line().rstrip("\n")
            session.set_note(note)
            continue
        if ch in ("?", "h"):
            out.write(HELP_TEXT + "\n")
            continue
        if ch in ("q", "\x04"):
            out.write("Quit — progress saved.\n")
            break
        out.write(f"Unknown key {ch!r}. Press ? for help.\n")

    dist = session.distribution()
    out.write("\nLabel distribution (labeled rows):\n")
    if not dist:
        out.write("  (none yet)\n")
    else:
        for k, v in dist.items():
            out.write(f"  {k}: {v}\n")
    out.write(f"Total labeled: {session.labeled_count}/{len(session.rows)}\n")
    out.flush()
    return session
