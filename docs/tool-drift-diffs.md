# Cross-harness tool drift review package

**Prepared:** 2026-08-09 (UTC execution window)  
**Scope:** Firecrawl, Mintlify, Playwright — Cursor vs Claude  
**Constraint:** Inspection and diffs only. No files under either harness were modified for this item.

Full unified diffs for Firecrawl skill pairs (and repo skill pairs from Task 2) live in [`tool-drift-diff-files/`](./tool-drift-diff-files/).

---

## 1. Firecrawl

### Paths, sizes, mtimes

| Side | Path | Size (`du`) | Newest content mtime |
|------|------|-------------|----------------------|
| Claude | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/firecrawl/1.0.9` | 160 KiB | 2026-06-08 11:00:26 |
| Cursor (named) | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/firecrawl/866f30d4dc0b3eca6a05884f6b6db6d914a5967c` | 364 KiB | 2026-08-05 22:01:42 |

Both sides expose the same 10 skill directories (`firecrawl-agent`, `firecrawl-cli`, `firecrawl-crawl`, `firecrawl-download`, `firecrawl-interact`, `firecrawl-map`, `firecrawl-monitor`, `firecrawl-parse`, `firecrawl-scrape`, `firecrawl-search`).

Also present (not the drift pair; out of scope to remove): Cursor numeric alias `789/` holds a different shape (single `firecrawl` skill), older than the named plugin.

Claude-only extras: `commands/skill-gen.md`.  
Cursor-only extras: top-level `rules/install.mdc`, larger plugin packaging / assets.

### Skill content comparison

| Skill | Status | Diff file |
|-------|--------|-----------|
| `firecrawl-agent` | Identical | [`firecrawl-firecrawl-agent.diff`](./tool-drift-diff-files/firecrawl-firecrawl-agent.diff) |
| `firecrawl-crawl` | Identical | [`firecrawl-firecrawl-crawl.diff`](./tool-drift-diff-files/firecrawl-firecrawl-crawl.diff) |
| `firecrawl-download` | Identical | [`firecrawl-firecrawl-download.diff`](./tool-drift-diff-files/firecrawl-firecrawl-download.diff) |
| `firecrawl-interact` | Identical | [`firecrawl-firecrawl-interact.diff`](./tool-drift-diff-files/firecrawl-firecrawl-interact.diff) |
| `firecrawl-map` | Identical | [`firecrawl-firecrawl-map.diff`](./tool-drift-diff-files/firecrawl-firecrawl-map.diff) |
| `firecrawl-parse` | Identical | [`firecrawl-firecrawl-parse.diff`](./tool-drift-diff-files/firecrawl-firecrawl-parse.diff) |
| `firecrawl-scrape` | Identical | [`firecrawl-firecrawl-scrape.diff`](./tool-drift-diff-files/firecrawl-firecrawl-scrape.diff) |
| `firecrawl-cli` | **Divergent** | [`firecrawl-firecrawl-cli.diff`](./tool-drift-diff-files/firecrawl-firecrawl-cli.diff) |
| `firecrawl-monitor` | **Divergent** | [`firecrawl-firecrawl-monitor.diff`](./tool-drift-diff-files/firecrawl-firecrawl-monitor.diff) |
| `firecrawl-search` | **Divergent** | [`firecrawl-firecrawl-search.diff`](./tool-drift-diff-files/firecrawl-firecrawl-search.diff) |

### Readable unified diffs (truncated)

#### `firecrawl-cli` (Claude → Cursor)

```diff
--- claude/.../firecrawl/1.0.9/skills/firecrawl-cli/SKILL.md
+++ cursor/.../firecrawl/.../skills/firecrawl-cli/SKILL.md
@@ Prerequisites
-Must be installed and authenticated. Check with `firecrawl --status`.
+Must be installed. Check with `firecrawl --status`.
+
+Authenticating gives the best results. Prefer a free account via
+`firecrawl init --browser` ... or a `FIRECRAWL_API_KEY` ...
+... keyless free tier (rate-limited) ...
+
+## Endpoint job feedback
+For non-search endpoint jobs, use `firecrawl feedback <endpoint> <jobId>` ...
+**Opt out:** `export FIRECRAWL_NO_ENDPOINT_FEEDBACK=1` ...
```

#### `firecrawl-monitor` (Claude → Cursor)

Cursor expands the skill from **page monitors only** to also cover **web monitors** (search the whole web for new results matching a goal via `--queries` + `--goal`), and adds target-mode table: single page / URL batch / whole-site crawl / web search.

#### `firecrawl-search` (Claude → Cursor)

Cursor adds **developer search**: `--categories developer`, dedicated `firecrawl developer` command, and instructions to read `data.developer` / full passages — aimed at coding-agent queries (GitHub issues/PRs/READMEs/docs).

### Behavioral summary (what a model would do differently)

1. **Auth gate (`firecrawl-cli`)** — Claude instructs “installed **and authenticated**.” Cursor allows proceeding on a **keyless free tier** and pushes browser/API-key onboarding as preferred but not mandatory.  
   **Unexpected-behavior flag:** Claude-side agents may refuse or stall when no API key is present; Cursor-side agents will attempt keyless calls.

2. **Feedback loops** — Cursor tells the model to send endpoint job feedback (`firecrawl feedback …`) unless `FIRECRAWL_NO_ENDPOINT_FEEDBACK=1`. Claude lacks that section.  
   **Unexpected-behavior flag:** Cursor sessions may emit background feedback API calls the owner did not explicitly request.

3. **Monitoring surface** — Cursor can invent **web monitors** (query+goal over the whole web). Claude only knows URL/page/site change watches. Same skill name, different product surface.

4. **Search routing** — Cursor will prefer `--categories developer` / `firecrawl developer` for programming questions. Claude will use the older category set (`github,research,pdf`) and never the developer index.

### Recommendation

- **Source of truth: Cursor named plugin** (`866f30d4…`, mtime 2026-08-05). It is newer and more complete (keyless tier, endpoint feedback, web monitors, developer search).
- Keep Claude Firecrawl installed only if you want Claude Code sessions to use the older, stricter auth story — otherwise update/reinstall Claude’s Firecrawl from marketplace so it matches Cursor, or disable one harness’s copy after that sync.
- Do **not** treat numeric `789/` as the Cursor SoT; it is a different, older skill shape.

---

## 2. Mintlify

### Paths, sizes, mtimes

| Side | Path | Size (`du`) | Newest content mtime | Status |
|------|------|-------------|----------------------|--------|
| Claude (audit) | `~/.claude/plugins/cache/claude-plugins-official/mintlify/acd6d2e0128c` | 40.9 KiB (audit) | 2026-07-24 (audit) | **ABSENT at execution** |
| Cursor (named) | `/Users/ruttanshbhatelia/.cursor/plugins/cache/cursor-public/mintlify-cursor-plugin/a22550306ff6b704649a8f09faf393e007cbcc1e` | 256 KiB | 2026-07-02 10:41:58 | Present |
| Cursor (numeric alias) | `~/.cursor/plugins/cache/cursor-public/25808295/a22550306ff6b704649a8f09faf393e007cbcc1e` | same content hash | — | Alias of named; not real duplication |

Claude Mintlify was already gone from cache before this cleanup (also absent from `installed_plugins.json` / `enabledPlugins` in the orphan audit). Marketplace trees under `~/.claude/plugins/marketplaces/` also have no Mintlify plugin copy to diff against.

### Live unified diff

**Not available.** Cannot produce Claude↔Cursor file diffs without the Claude tree.

### Audit-era drift (for decision context)

From `skills-audit.md` (2026-08-09):

- Cursor `mintlify` SKILL.md hash `09cc4ae1e5e41a95`
- Claude `mintlify` SKILL.md hash `d0a01483e35bd596` (description phrasing differed: “Mintlify sites” vs “Mintlify documentation sites”)

Cursor package also includes `rules/mintlify.mdc`, `mcp.json`, and a fuller `skills/mintlify/reference/` set (api-docs, cli, components, navigation, configuration).

### Behavioral summary

With Claude’s copy already gone, there is currently **no dual-harness Mintlify skill conflict** — only Cursor still has the skill. Reinstalling Claude Mintlify from an older cache/marketplace revision would reintroduce hash drift.

### Recommendation

- **Source of truth: Cursor `mintlify-cursor-plugin`.** It is the only live copy and includes reference docs + rule + MCP wiring.
- If Claude Mintlify is needed again, install fresh from the official marketplace and immediately diff against Cursor before enabling — do not restore an unknown older cache blob without that check.
- **Unexpected-behavior flag:** None active today (Claude copy absent). Future reinstall without sync would silently revive divergent docs guidance.

---

## 3. Playwright

### Paths, sizes, mtimes

| Side | Path | Size (`du`) | Newest content mtime | Shape |
|------|------|-------------|----------------------|-------|
| Claude plugin | `/Users/ruttanshbhatelia/.claude/plugins/cache/claude-plugins-official/playwright/unknown` | 52 KiB (mostly `.in_use` markers; payload is tiny) | plugin.json / `.mcp.json` from install era (~2026-04-11 content files) | MCP launcher plugin |
| Cursor plugin cache | *(no `playwright` entry under `~/.cursor/plugins/cache/cursor-public/`)* | — | — | Not packaged as a Cursor plugin-cache skill tree |
| Cursor MCP (this workspace) | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/mcps/plugin-playwright-playwright` | descriptor + 24 tool JSON files | 2026-08-09 02:07 | Fixed MCP tool surface |

Claude plugin files (entire behavioral payload):

```json
// .claude-plugin/plugin.json
{ "name": "playwright", "description": "Browser automation and end-to-end testing MCP server by Microsoft. ..." }

// .mcp.json
{ "playwright": { "command": "npx", "args": ["@playwright/mcp@latest"] } }
```

Cursor MCP metadata:

```json
{ "serverIdentifier": "plugin-playwright-playwright", "serverName": "playwright" }
```

Cursor exposes these tools (24):  
`browser_click`, `browser_close`, `browser_console_messages`, `browser_drag`, `browser_drop`, `browser_evaluate`, `browser_file_upload`, `browser_fill_form`, `browser_find`, `browser_handle_dialog`, `browser_hover`, `browser_navigate`, `browser_navigate_back`, `browser_network_request`, `browser_network_requests`, `browser_press_key`, `browser_resize`, `browser_run_code_unsafe`, `browser_select_option`, `browser_snapshot`, `browser_tabs`, `browser_take_screenshot`, `browser_type`, `browser_wait_for`.

### Unified diff

There is no parallel skill-tree pair to `diff -u`. The meaningful comparison is **integration shape**:

| Dimension | Claude | Cursor |
|-----------|--------|--------|
| Delivery | Official Claude plugin → spawns MCP via shell | Cursor MCP plugin channel (`plugin-playwright-playwright`) |
| Version pin | `@playwright/mcp@latest` (floating) | Tool schemas materialized as local JSON descriptors |
| Skill markdown | None | None in plugin cache (automation is MCP tools, not SKILL.md) |
| Dangerous ops | Whatever `@latest` exposes on run day | Explicit `browser_run_code_unsafe` tool present in descriptor set |

### Behavioral summary

- A Claude session loads Playwright as “run `npx @playwright/mcp@latest`,” so **tool names, arguments, and safety posture can change whenever npm resolves a new latest**.
- A Cursor session loads a **fixed catalog of `browser_*` tools** from the MCP plugin descriptors already on disk for the project.
- Same product name, different contract stability. This is not a line-edit drift inside SKILL.md; it is harness packaging drift.

### Unexpected-behavior flags

1. **Floating `@latest` on Claude** — silent tool-surface changes across days without owner action.  
2. **`browser_run_code_unsafe` on Cursor** — allows arbitrary JS in the Playwright server process; Claude’s surface depends on whatever `@latest` ships that day (may or may not expose an equivalent).  
3. **No Cursor plugin-cache Playwright** — disabling “Playwright” in Claude’s plugin UI does not remove Cursor’s MCP Playwright, and vice versa.

### Recommendation

- Treat them as **two independent installs of related MCP servers**, not as syncable skill trees.
- **Prefer Cursor’s fixed MCP descriptors as the behavioral SoT for day-to-day agent work** (predictable tool list).
- For Claude: consider pinning `@playwright/mcp` to a specific version instead of `@latest` if you keep the Claude plugin enabled (change not made here).
- Codex also has `~/.codex/skills/playwright` (skill + scripts); left untouched per scope.

---

## Decision checklist (no changes made)

| Tool | Live dual install? | Recommended SoT | Action left for owner |
|------|--------------------|-----------------|------------------------|
| Firecrawl | Yes (skills diverge in 3/10 files) | Cursor named plugin | Update or disable Claude copy after review |
| Mintlify | No (Claude already absent) | Cursor plugin | Only reinstall on Claude if synced to Cursor |
| Playwright | Yes, but different packaging | Cursor MCP descriptors for stability; pin Claude npm version if kept | Decide whether both MCP entry points stay enabled |

---

## Appendix — Task 2 note (repo skills; no quarantine)

`ai-challenge-loan-ref` `.claude/skills` vs `.agents/skills` for `next-best-practices`, `vercel-react-best-practices`, and `web-design-guidelines` are **not equivalent**. Diffs: [`repo-*-SKILL.md.diff`](./tool-drift-diff-files/). See execution record in `cleanup-queue.md`.
