"""Map claims to on-disk config surfaces (read-only inventory)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

_SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".agentlog-quarantine",
        "go",
        ".cargo",
        ".nvm",
        ".codex/.tmp",
    }
)


@dataclass
class ConfigFile:
    path: Path
    kind: str  # agents_md | claude_md | cursor_rule | skill
    scope_type: str
    scope_id: str | None
    exists: bool
    preview: str = ""
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "exists": self.exists,
            "preview": self.preview,
            "content_hash": self.content_hash,
        }


@dataclass
class ConfigInventory:
    home: Path
    files: list[ConfigFile] = field(default_factory=list)

    def texts(self) -> list[tuple[Path, str]]:
        out: list[tuple[Path, str]] = []
        for f in self.files:
            if not f.exists:
                continue
            try:
                text = f.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append((f.path, text))
        return out

    def already_covers(self, needles: Iterable[str]) -> list[str]:
        """Return needles that already appear (case-insensitive) in inventory."""
        blob = "\n".join(text for _, text in self.texts()).lower()
        hits: list[str] = []
        for needle in needles:
            n = needle.lower().strip()
            if n and n in blob:
                hits.append(needle)
        return hits

    def by_kind(self, kind: str) -> list[ConfigFile]:
        return [f for f in self.files if f.kind == kind]

    def global_agents(self) -> ConfigFile | None:
        for f in self.files:
            if f.kind == "agents_md" and f.scope_type == "global":
                return f
        return None

    def repo_agents(self, repo_key: str) -> ConfigFile | None:
        for f in self.files:
            if f.kind == "agents_md" and f.scope_type == "repo" and f.scope_id == repo_key:
                return f
        return None


def project_label(repo: str | None, cwd: str | None) -> str:
    if repo:
        text = repo.strip()
        if text.startswith("http"):
            path = urlparse(text).path.rstrip("/")
            name = path.split("/")[-1]
            return name.removesuffix(".git") or text
        if "/" in text or text.startswith("-") or text.startswith("Users-"):
            return text.split("/")[-1].lstrip("-") or text
        return text
    if cwd:
        return cwd.rstrip("/").split("/")[-1] or "(unknown)"
    return "(unknown)"


def resolve_repo_root(cwd: str | None, repo: str | None, home: Path) -> Path | None:
    """Best-effort local path for a session's project (read-only)."""
    candidates: list[Path] = []
    if cwd:
        p = Path(cwd)
        if p.is_dir() and not str(p).startswith(str(home / ".claude" / "projects")):
            candidates.append(p)
        # Claude project dirs encode the real path after the prefix.
        marker = str(home / ".claude" / "projects")
        if str(p).startswith(marker):
            encoded = p.name
            if encoded.startswith("-"):
                decoded = "/" + encoded[1:].replace("-", "/")
                # Common macOS home rewrite: /Users/name/...
                decoded = decoded.replace("/Users/", "/Users/", 1)
                candidates.append(Path(decoded))
    if repo and not str(repo).startswith("http"):
        # Cursor-style: Users-ruttansh-side-projects-Plugin
        if repo.startswith("Users-") or repo.startswith("-Users-"):
            raw = repo.lstrip("-").replace("-", "/")
            if not raw.startswith("/"):
                raw = "/" + raw
            candidates.append(Path(raw))
    for cand in candidates:
        try:
            if cand.is_dir():
                return cand.resolve()
        except OSError:
            continue
    return None


def _sha1_file(path: Path) -> str | None:
    import hashlib

    try:
        data = path.read_bytes()
    except OSError:
        return None
    return hashlib.sha1(data).hexdigest()


def _preview(path: Path, limit: int = 240) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " ".join(text.split())[:limit]


def _add_if_file(
    files: list[ConfigFile],
    path: Path,
    *,
    kind: str,
    scope_type: str,
    scope_id: str | None,
) -> None:
    exists = path.is_file()
    files.append(
        ConfigFile(
            path=path,
            kind=kind,
            scope_type=scope_type,
            scope_id=scope_id,
            exists=exists,
            preview=_preview(path) if exists else "",
            content_hash=_sha1_file(path) if exists else None,
        )
    )


def discover_config_inventory(
    home: Path | None = None,
    *,
    extra_repo_roots: Iterable[Path] | None = None,
) -> ConfigInventory:
    """Read-only scan of the owner's agent config surfaces."""
    base = home or Path.home()
    files: list[ConfigFile] = []

    _add_if_file(
        files,
        base / "AGENTS.md",
        kind="agents_md",
        scope_type="global",
        scope_id="global",
    )
    # Symlink target for global CLAUDE.md
    _add_if_file(
        files,
        base / ".claude" / "CLAUDE.md",
        kind="claude_md",
        scope_type="global",
        scope_id="global",
    )
    _add_if_file(
        files,
        base / ".codex" / "AGENTS.md",
        kind="agents_md",
        scope_type="harness",
        scope_id="codex",
    )

    rules_dir = base / ".cursor" / "rules"
    if rules_dir.is_dir():
        for path in sorted(rules_dir.glob("*.md")):
            if path.is_file():
                _add_if_file(
                    files,
                    path,
                    kind="cursor_rule",
                    scope_type="user_rules",
                    scope_id=path.stem,
                )
        for path in sorted(rules_dir.glob("*.mdc")):
            if path.is_file():
                _add_if_file(
                    files,
                    path,
                    kind="cursor_rule",
                    scope_type="user_rules",
                    scope_id=path.stem,
                )

    repo_roots: list[Path] = []
    for rel in (
        "ai_sec",
        "Documents/local-sec",
        "side_projects/Plugin",
        "side_projects/solprobe",
        "side_projects/research-papers",
        "side_projects/ai-challenge-loan-ref",
    ):
        cand = base / rel
        if cand.is_dir():
            repo_roots.append(cand)
    if extra_repo_roots:
        for root in extra_repo_roots:
            if root.is_dir():
                repo_roots.append(root)

    seen: set[Path] = set()
    for root in repo_roots:
        try:
            key = root.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        label = root.name
        _add_if_file(
            files,
            root / "AGENTS.md",
            kind="agents_md",
            scope_type="repo",
            scope_id=label,
        )
        _add_if_file(
            files,
            root / "CLAUDE.md",
            kind="claude_md",
            scope_type="repo",
            scope_id=label,
        )
        cursor_rules = root / ".cursor" / "rules"
        if cursor_rules.is_dir():
            for path in sorted(cursor_rules.rglob("*")):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in {".md", ".mdc"}:
                    continue
                if any(part in _SKIP_DIR_NAMES for part in path.parts):
                    continue
                _add_if_file(
                    files,
                    path,
                    kind="cursor_rule",
                    scope_type="repo",
                    scope_id=label,
                )

    return ConfigInventory(home=base, files=files)


_INSTRUCTION_NEEDLES = {
    "dont_act_yet_brake": [
        "don't act yet",
        "do not act yet",
        "wait for explicit go-ahead",
        "don't start editing",
        "do not start coding until",
    ],
    "verify_before_done": [
        "verify it actually works",
        "post-edit verification",
    ],
    "scope_narrow": [
        "stay inside the files",
        "do not expand into adjacent",
        "named scope",
        "micro patch only",
    ],
    "spawn_workers": [
        "prefer spawning workers",
        "spawn workers over doing the implementation",
    ],
}


def instruction_already_present(
    inventory: ConfigInventory, theme: str
) -> list[str]:
    needles = _INSTRUCTION_NEEDLES.get(theme, [])
    return inventory.already_covers(needles)


def preferred_target_for_theme(
    inventory: ConfigInventory,
    *,
    theme: str,
    repo_key: str | None,
) -> ConfigFile:
    """Choose where a new instruction should land."""
    if repo_key:
        repo = inventory.repo_agents(repo_key)
        if repo and repo.exists:
            return repo
        # Prefer creating repo AGENTS.md beside known root.
        for f in inventory.files:
            if f.scope_type == "repo" and f.scope_id == repo_key and f.kind == "agents_md":
                return f
    global_agents = inventory.global_agents()
    if global_agents is not None:
        return global_agents
    return ConfigFile(
        path=inventory.home / "AGENTS.md",
        kind="agents_md",
        scope_type="global",
        scope_id="global",
        exists=False,
    )


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_repo_key(label: str) -> str:
    return _NORMALIZE_RE.sub("-", label.lower()).strip("-")
