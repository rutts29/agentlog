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
