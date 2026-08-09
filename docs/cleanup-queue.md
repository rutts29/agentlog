# Harness Cleanup Queue

Approved 2026-08-09. Execute **after** the agentlog build, not before.
Source: [`skills-audit.md`](./skills-audit.md)

Each item requires confirmation before any deletion. Verify enabled-vs-cached
state in the product UI first — a cache entry is not the same as an enabled plugin.

## 1. Superpowers dual-install

Same 14 skills installed under both Claude and Cursor, so they are injected
twice in dual-harness workflows. Roughly 32k tokens of genuine duplication.

- Claude: `~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1`
- Cursor: `~/.cursor/plugins/cache/cursor-public/` (named + numeric alias)

Action: keep one harness as primary, disable the other through its UI.

## 2. Cross-harness tool drift

Firecrawl, Mintlify, and Playwright exist in both Cursor and Claude with
**divergent content**, so behavior differs by harness.

Action: diff each pair, choose one source of truth, disable the other.

## 3. Orphaned Claude plugin cache (~86 MB)

17 entries present in `~/.claude/plugins/cache/` but absent from
`installed_plugins.json`. Disk only — no token cost.

Largest: `semgrep` 79.3 MB, `data` 2.2 MB, `figma` 1.7 MB, `aws-serverless` 741 KB,
`plugin-dev` 538 KB, `deploy-on-aws` 506 KB.

Action: confirm none are enabled, then remove.

## 4. Repo skill duplication

`ai-challenge-loan-ref` carries the same Next/React guidelines in both
`.claude/skills` and `.agents/skills`.

Action: keep one, symlink or delete the other.

## Not actioned

- **Cursor numeric plugin IDs** (`684/`, `735/`, etc.) are aliases pointing at the
  same content hashes as the named directories. They inflate the duplicate count
  by ~62 copies but are not real duplication. No action.
- **Oversized skill splitting** — deferred pending review.
- **Codex `.system` skills** — platform-managed, leave alone.

## Deferred until agentlog can measure

Once skill-activation data exists, revisit removal decisions with evidence of
what actually fires rather than what merely exists on disk.

---

## Execution record — 2026-08-09T11:14:05Z

Quarantine root: `~/.agentlog-quarantine/20260809T111405Z/` (see `RESTORE.md` there).  
Operations used `mv` only; no `rm`.

### Task 1 — Orphaned Claude plugin cache

Re-verified each audit candidate against `installed_plugins.json`, `settings.json` → `enabledPlugins` (20 enabled; none of the orphans), empty leftover `plugins/data/*` dirs, and marketplace catalog hits (catalog ≠ enabled).

**Quarantined (9), ~7.5 MiB (`du`):**  
`adspirer-ads-agent`, `amazon-location-service`, `context7`, `data`, `deploy-on-aws`, `figma`, `pyright-lsp`, `ralph-loop`, `typescript-lsp`.

**Already absent (8; not moved):**  
`aws-serverless`, `gopls-lsp`, `learning-output-style`, `mintlify`, `plugin-dev`, `rust-analyzer-lsp`, `semgrep` (~79 MiB at audit time), `swift-lsp`.

Claude plugin cache: `1554568` KiB → `1546912` KiB (**7656 KiB / ~7.5 MiB reclaimed**).

### Task 2 — Repo skill duplication

`~/side_projects/ai-challenge-loan-ref`: `.claude/skills` vs `.agents/skills` for the three Next/React guidelines **diverged** (SKILL.md hash drift + `.claude` trees hold many supporting files the `.agents` stubs lack). **Nothing quarantined.** Diffs under `docs/tool-drift-diff-files/repo-*.diff`. Symlink not appropriate until content is reconciled.

### Task 3 — Tool drift review package

Wrote `docs/tool-drift-diffs.md` (+ side diffs). No harness files changed. Recommendations: Firecrawl SoT = Cursor named plugin; Mintlify SoT = Cursor (Claude copy already gone); Playwright = packaging drift (Claude `@latest` MCP vs Cursor fixed MCP tools).

### Explicitly left alone

Superpowers dual-install; Cursor numeric aliases; Codex `.system` skills; oversized skill splitting; `~/.claude/projects` / transcripts / history; empty `plugins/data` leftovers; marketplace trees.

---

## Execution record — 2026-08-09T11:23:13Z

Quarantine root: `~/.agentlog-quarantine/20260809T112313Z/` (see `RESTORE.md` there).  
Operations used `mv` only; no `rm` of plugin content. Configs backed up under `config-backups/` before edit.

### Task 1 — Superpowers: Cursor primary

**Verified live:** Cursor named cache `superpowers/d884ae04…` and Claude `6.1.1` both declare **version 6.1.1** in `.claude-plugin/plugin.json` / `.cursor-plugin/plugin.json` / `package.json`. Skill sets are identical (14 skills); all 14 `SKILL.md` SHA-256 hashes match. The earlier “15 vs 14” count was the Cursor numeric alias `684/` contributing one extra `SKILL.md` path (out of scope), not an extra skill.

**Quarantined Claude Superpowers:** `5.1.0` (1272 KiB), `6.0.3` (1812 KiB), then after disable, enabled `6.1.1` (1812 KiB).

**Config edits:**  
- `~/.claude/settings.json` → `enabledPlugins["superpowers@claude-plugins-official"]=false`  
- `~/.claude/plugins/installed_plugins.json` → removed `superpowers@claude-plugins-official`  
Backups: `…/config-backups/claude-settings.json`, `…/config-backups/claude-installed_plugins.json`.

Cursor Superpowers left in place as primary.

### Task 2 — Unreferenced plugin *versions*

Claude sweep of `~/.claude/plugins/cache/**/<version>` vs `installed_plugins.json` + `enabledPlugins`.

**Quarantined (unambiguous orphans of still-enabled plugins):**  
- `chrome-devtools-mcp/1.2.0` (348356 KiB) — installed points at `1.6.0` only  
- `chrome-devtools-mcp/1.3.0` (406648 KiB) — same  
- `vercel/0.43.0` (141388 KiB) — installed points at `0.45.1` only  

Stale `.in_use` lockfiles from Jun 15 on the orphans; active versions retain Jul 24 locks. No config path references to the orphan versions.

**Cursor sweep:** each named plugin under `cursor-public/` has a single hash dir (no multi-version orphans). Numeric aliases left untouched. Named `firecrawl/866f30d4…` and `postman/f5ea7c56…` lack a same-hash numeric alias and did not appear in shallow JSON configs / `state` JSON; enablement likely lives in `state.vscdb`. **Not moved** (uncertain + Firecrawl is the approved SoT).

Claude cache: `1546912` KiB → `645464` KiB (**901448 KiB / ~880.3 MiB reclaimed** including Task 1/3 moves).  
Cursor cache: `188004` KiB → `188004` KiB (**0 reclaimed**).

### Task 3 — Approved drift decisions

**Firecrawl:** Claude copy disabled then quarantined (`firecrawl/1.0.9`, 160 KiB). Same config backup/edit pattern as Superpowers (`enabledPlugins` → false; removed install entry). Cursor named Firecrawl kept.

**Mintlify:** Claude cache / install / enabled entries **absent** — confirmed, no action.

**Playwright:** Pinned Claude MCP launcher from `@playwright/mcp@latest` → `@playwright/mcp@0.0.78` in  
`~/.claude/plugins/cache/claude-plugins-official/playwright/unknown/.mcp.json`.  
Version choice: `0.0.79` is current `latest` but only **3 days** old (2026-08-06); owner required not-last-few-days. `0.0.78` published **2026-07-09** (~30 days), non-prerelease, on a package with ~26.9M downloads last month and a long 0.0.x history. Backup: `…/config-backups/claude-playwright-.mcp.json`. Marketplace template copies under `~/.claude/plugins/marketplaces/**` still say `@latest` (not the live install path); left alone.

### Explicitly left alone

Cursor numeric aliases; Cursor Firecrawl/Postman named trees (uncertain refs); Codex `.system` skills; `ai-challenge-loan-ref` skill merge; oversized skill splitting; `~/.claude/projects` / transcripts / history; marketplace trees (except noting Playwright template still `@latest`).
