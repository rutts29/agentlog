# AI Coding Agent Data Inventory

Inventory date: **2026-08-09**  
Machine: macOS (`darwin`), user `ruttanshbhatelia`  
Purpose: enumerate every persistent location of AI coding-agent transcripts, memory, config, skills, and rules so **agentlog** can parse all of it.

---

## Executive summary

| Category | Approx. volume | Parse priority |
|---|---|---|
| **Parse-worthy conversational / memory data** | **~1.05 GB** | see table below |
| Total agent-related footprint (incl. caches, plugins, VMs, extensions) | **~20+ GB** | mostly skip |
| Primary harnesses with real history | Codex, Cursor, Claude Code | high |
| Secondary | Warp AI, ChatGPT desktop, VS Code chat, Claude Desktop IndexedDB | medium / low |
| Absent on this machine | `~/.anthropic`, `~/.openai`, `~/.gemini`, `~/.hermes`, `~/.kimi`, `~/.cursor/rules` | n/a |

**Recommended parse order for agentlog:**

1. **Codex session JSONL** (`~/.codex/sessions/`) — largest clean transcript corpus  
2. **Cursor `state.vscdb` composer/bubble data** — full IDE chat history (not only agent-transcripts)  
3. **Claude Code project JSONL** (`~/.claude/projects/`)  
4. **Cursor agent-transcripts** (`~/.cursor/projects/*/agent-transcripts/`) — already human-readable JSONL  
5. **Codex `state_5.sqlite` threads + memories** — index / memory layer  
6. **Rules / AGENTS.md / MEMORY.md / skills** — metadata enrichment, not transcripts  
7. Warp / ChatGPT desktop / Claude Desktop / VS Code — optional secondary sources  

---

## Master table of data sources

| # | Path | Harness | Formats | Size / count | Contents | Date range | Priority | Format notes |
|---|---|---|---|---|---|---|---|---|
| 1 | `~/.codex/sessions/` | Codex CLI / Desktop | JSONL (`rollout-*.jsonl`) | **292 MB**, **401** files | Full session rollouts: meta, messages, tool calls | 2026-04-25 → 2026-08-01 | **HIGH** | Event stream with `type` + `payload`; nested by `YYYY/MM/DD/` |
| 2 | `~/.codex/archived_sessions/` | Codex | JSONL / mixed | **14 MB**, 3 files | Archived sessions | 2026-05-13 → 2026-07-01 | **HIGH** | Same family as sessions |
| 3 | `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` | Cursor IDE | SQLite (`ItemTable`, `cursorDiskKV`, `composerHeaders`) | **237 MB**; 84 composers; ~23.7k bubbles; ~15.9k agentKv blobs | Full composer/chat message store | 2026-07-02 → 2026-08-09 | **HIGH** | Complex: join `composerData:*` + `bubbleId:*` + `agentKv:blob:*` |
| 4 | `~/.cursor/projects/*/agent-transcripts/` | Cursor Agent | JSONL | **5.1 MB**, **55** files, 6 project dirs | Agent chat transcripts (`role`/`message`) | 2026-07-02 → 2026-08-09 | **HIGH** | Simplest Cursor format; may be subset of vscdb |
| 5 | `~/.claude/projects/` | Claude Code | JSONL + JSON + MD + txt | **41 MB** total; **86** JSONL (~39 MB) | Session transcripts, subagents, tool-results, project memory | 2026-04-12 → 2026-07-26 | **HIGH** | Types: `user`, `assistant`, `attachment`, `system`, etc. |
| 6 | `~/.codex/state_5.sqlite` | Codex | SQLite | **6.7 MB**; 404 threads, 360 spawn edges | Thread index / graph | through 2026-08-07 | **HIGH** | Use as session catalog; join to JSONL by thread id |
| 7 | `~/.codex/memories/` + `memories_1.sqlite` | Codex | MD + SQLite + git | **236 KB** MD; **144 KB** DB | `MEMORY.md`, `raw_memories.md`, summaries | through 2026-08-01 | **HIGH** | Small but high-signal durable memory |
| 8 | `~/.claude/projects/*/memory/` | Claude Code | Markdown | ~4 project memory dirs | `MEMORY.md`, feedback_*, project_* notes | 2026-05 → 2026-07 | **HIGH** | Structured auto-memory |
| 9 | `~/.claude/history.jsonl` | Claude Code | JSONL | **420 KB** | Prompt history (display text, project, sessionId) | through 2026-07-27 | **MEDIUM** | Command/prompt index, not full turns |
| 10 | `~/.codex/history.jsonl` + `session_index.jsonl` | Codex | JSONL | **251 KB** + **4 KB**; 155 + 34 lines | Prompt history + session index | through 2026-07-12 / 2026-08-01 | **MEDIUM** | Lightweight index |
| 11 | `~/…/Cursor/…/conversation-search.db` | Cursor | SQLite + FTS | **2.6 MB**; 39 conversations | Search index over chats | recent | **MEDIUM** | Titles/metadata; content likely in state.vscdb |
| 12 | `~/.cursor/ai-tracking/ai-code-tracking.db` | Cursor | SQLite | **3 MB**; 13.7k code hashes, 25 scored commits | AI code attribution / tracking | through 2026-08-08 | **MEDIUM** | Not transcripts; useful for “what AI wrote” |
| 13 | `~/.codex/logs_2.sqlite` | Codex | SQLite | **415 MB**; 22,420 log rows | App/runtime logs | through 2026-08-07 | **LOW** | Huge; noisy; parse only if needing telemetry |
| 14 | `~/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite` | Warp | SQLite | **7.3 MB**; 368 `ai_queries`, 15 `agent_conversations` | Terminal AI queries / agent convos | active | **MEDIUM** | Separate product; easy SQL |
| 15 | `~/Library/Application Support/com.openai.chat/conversations-v3-*` | ChatGPT macOS | `.data` blobs | **~4.6 MB** app data; several multi‑100KB–2MB files | Cached ChatGPT conversations | through 2026-07 | **MEDIUM** | Opaque `.data` format; may need reverse-engineering |
| 16 | `~/Library/Application Support/Code/User/workspaceStorage/*/chatSessions/` | VS Code | JSON | **12 KB**, 1 session | VS Code chat sessions | sparse | **LOW** | Almost empty on this machine |
| 17 | `~/Library/Application Support/Claude/` (IndexedDB, local-agent) | Claude Desktop | LevelDB / plugins | IndexedDB **1.7 MB**; local-agent **6.7 MB**; total app **12 GB** (mostly VM/cache) | Desktop chats (IndexedDB), cowork skills | through 2026-06 | **LOW–MEDIUM** | LevelDB hard; VM bundles (**9.8 GB**) skip |
| 18 | `~/.claude/settings.json`, `~/.claude.json`, `~/.codex/config.toml`, `~/.cursor/mcp.json`, Claude `claude_desktop_config.json` | Multi | JSON / TOML | small | Config, hooks, MCP server defs | current | **MEDIUM** | Config/metadata only; **redact secrets** |
| 19 | Skills / plugins caches (`~/.claude/plugins`, `~/.codex/plugins`, `~/.cursor/skills-cursor`, `~/.agents/skills`) | Multi | MD / packages | Claude plugins **1.9 GB**; Codex plugins **317 MB**; Cursor skills **296 KB** | Skill definitions (`SKILL.md`) | ongoing | **MEDIUM** (defs) / **SKIP** (caches) | Parse skill *names/paths*; skip node_modules blobs |
| 20 | Repo `AGENTS.md` / `CLAUDE.md` / `.claude` / `.agents` / `.codex` | Project rules | Markdown | see §Repo files | Per-repo agent instructions | various | **MEDIUM** | Enrichment, not session logs |
| 21 | Obsidian vault: `~/side_projects/research-papers` | Obsidian | MD + `.obsidian` | vault open; app support **42 MB** | Research notes; many agent-topic briefs | active | **LOW** | Content is research corpus, not agent transcripts |
| 22 | `~/.local/share/claude/` | Claude Code | App binaries / versions | **1.0 GB** | Runtime installs, not user data | n/a | **SKIP** | |
| 23 | `~/.cursor/extensions`, Cursor App Support caches | Cursor | mixed | extensions **941 MB**; App Support **1.9 GB** | IDE extension/cache | n/a | **SKIP** | |
| 24 | Missing: `~/.anthropic`, `~/.openai`, `~/.gemini`, `~/.hermes`, `~/.kimi`, `~/.cursor/rules` | — | — | — | Not present | — | **SKIP** | |

**Parse-worthy conversational volume (sum of high-value sources): ~1.05 GB**  
**Gross disk used by agent-related trees: ~20+ GB** (dominated by Claude VM bundles, plugin caches, Cursor extensions).

---

## 1. Codex (`~/.codex/`) — 1.3 GB total

### 1.1 Sessions (primary transcript store) — HIGH

- **Path:** `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`
- **Count / size:** 401 JSONL files, ~304 MB on disk (du 292 MB)
- **Also:** `~/.codex/archived_sessions/` (14 MB, 3 files)
- **Date range:** 2026-04-25 → 2026-08-01
- **Sample event types:** `session_meta`, plus message/tool events in `type`/`payload` form
- **Originators observed:** Codex Desktop, CLI; includes subagent spawn metadata (`agent_nickname`, `agent_role`, `parent_thread_id`)
- **Parse complexity:** Medium — line-oriented JSONL, but many event types; need a type switch
- **Worth parsing:** **Yes — #1 source**

### 1.2 SQLite state / memory / logs

| DB | Size | Tables (notable) | Priority |
|---|---|---|---|
| `state_5.sqlite` | 6.7 MB | `threads` (404), `thread_spawn_edges` (360), `thread_sections`, `thread_dynamic_tools` | **HIGH** (catalog) |
| `memories_1.sqlite` | 144 KB | `stage1_outputs`, `jobs` | **MEDIUM** |
| `logs_2.sqlite` | 415 MB | `logs` (22,420) | **LOW** |
| `goals_1.sqlite` | 32 KB | goals | **LOW** |
| `sqlite/codex-dev.db` | 248 KB | older/dev mirror | **LOW** |
| `sqlite/*` older copies | ~48 MB | stale mirrors of above | **SKIP** unless backfill |

### 1.3 Markdown memories — HIGH

```
~/.codex/memories/
  MEMORY.md
  memory_summary.md
  raw_memories.md
  rollout_summaries/
  extensions/
  .git/          # versioned memory repo
```

### 1.4 Config / skills / other

| Path | Role | Priority |
|---|---|---|
| `config.toml` | Main config; includes `[mcp_servers]` (`node_repl`, `computer-use`, …) | MEDIUM |
| `auth.json` | Credentials | **SKIP** (secrets) |
| `AGENTS.md` | Global Codex agent instructions | MEDIUM |
| `hooks.json` | Hooks | MEDIUM |
| `skills/` | Local skills (~10 `SKILL.md`) | MEDIUM |
| `plugins/` (317 MB) | Cached plugin bundles + skills | MEDIUM defs / SKIP blobs |
| `history.jsonl` | Prompt history (155 lines) | MEDIUM |
| `session_index.jsonl` | Session index (34 lines) | MEDIUM |
| `.codex-global-state.json` | UI/global state (~165 KB) | LOW |
| `attachments/`, `shell_snapshots/`, `computer-use/`, `cache/` | Aux data | LOW / SKIP |
| `transcription-history.jsonl` | Voice transcripts | LOW–MEDIUM |

### 1.5 Codex Desktop Electron shell

- `~/Library/Application Support/Codex/` (~205 MB) — Chromium profile (Cache, GPU, etc.)
- Actual conversation data lives in `~/.codex/`, not here
- Priority: **SKIP** for agentlog (browser chrome only)

---

## 2. Cursor (`~/.cursor/` + Application Support) — ~3 GB combined

### 2.1 Agent transcripts — HIGH (easy)

- **Path:** `~/.cursor/projects/<project-slug>/agent-transcripts/<uuid>/<uuid>.jsonl`
- **Also:** `…/subagents/*.jsonl` under some chats
- **Size:** 5.1 MB, 55 JSONL files across ~6 projects with transcripts
- **Projects with transcripts (sizes):**
  - `…-research-papers` 1.6 MB
  - `…-ai-sec` 1.6 MB
  - `empty-window` 972 KB
  - `…-Plugin` 656 KB
  - `…-jito-mcp` 224 KB
  - `…-Documents-local-sec` 212 KB
- **Format:** `{ "role": "user"|"assistant", "message": { "content": [...] } }`
- **Date range:** 2026-07-02 → 2026-08-09
- **Also present:** `terminals/*.txt` (agent shell session mirrors) — useful context, MEDIUM
- **Note:** `~/.cursor/rules/` **does not exist** globally

### 2.2 Composer / chat SQLite — HIGH (harder, more complete)

- **Path:** `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (237 MB)
- **Tables:**
  - `composerHeaders` — **84** composer sessions (2026-07-02 → 2026-08-09)
  - `cursorDiskKV` — **41,051** keys, including:
    - `bubbleId:*` — 23,686 keys / **75.8 MB** (message bubbles)
    - `agentKv:blob:*` — 15,874 keys / **112.8 MB** (agent blobs)
    - `composerData:*` — 90 keys / 3.5 MB
  - `ItemTable` — 464 UI/settings keys
- **Parse complexity:** **High** — reconstruct threads by composer id; values are JSON blobs
- **Worth parsing:** **Yes — richest Cursor history**; agent-transcripts alone are incomplete

### 2.3 Other Cursor stores

| Path | Size | Contents | Priority |
|---|---|---|---|
| `…/conversation-search.db` | 2.6 MB | FTS over 39 conversations | MEDIUM |
| `~/.cursor/ai-tracking/ai-code-tracking.db` | 3 MB | AI code hashes / scored commits | MEDIUM |
| `~/.cursor/mcp.json` | 4 KB | MCP servers (e.g. `aikido`) — **contains secrets** | MEDIUM (names only) |
| `~/.cursor/cli-config.json`, `agent-cli-state.json` | small | CLI state | LOW |
| `~/.cursor/plans/*.plan.md` | 24 KB | Plan files | MEDIUM |
| `~/.cursor/skills-cursor/` | 296 KB | **20** built-in skills (`SKILL.md`) | MEDIUM |
| `~/.cursor/agents/` | empty | — | SKIP |
| `~/.cursor/extensions/` | 941 MB | Extensions | SKIP |
| `~/.cursor/plugins/` | 184 MB | Plugins | SKIP |
| Workspace `state.vscdb` (7 workspaces) | 1.8 MB | Per-workspace composer pane state | LOW–MEDIUM |

---

## 3. Claude Code (`~/.claude/` + `~/.claude.json`) — 2.2 GB home dir

### 3.1 Project transcripts — HIGH

- **Path pattern:** `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`
- **Sidecar dirs:** `<session-uuid>/subagents/*.jsonl`, `tool-results/`, `memory/`
- **Count / size:** 86 JSONL (~39 MB); project tree 41 MB
- **Projects (notable):**
  - `-Users-…-ai-sec` — 32 MB (largest)
  - `-Users-…-Documents-local-sec` — 8.4 MB
  - several `side_projects` / worktree stubs (small)
- **Date range:** 2026-04-12 → 2026-07-26
- **Event types (sample largest file):** `attachment`, `assistant`, `user`, `last-prompt`, `mode`, `permission-mode`, `ai-title`, `system`, `file-history-snapshot`, …
- **Also:** some JSONL are plugin telemetry (e.g. `skill-injection` events), not full chats — filter by `type`/`role`
- **Parse complexity:** Medium–high (rich event model + subagents)

### 3.2 Project memory — HIGH

Memory dirs under:

- `…/ai-sec/memory/` — MEMORY.md + feedback_* + project_ai_sec.md
- `…/side-projects/memory/`
- `…/side-projects-prime-intellect-jd/memory/` (and underscore variant)

### 3.3 Global Claude Code files

| Path | Size | Contents | Priority |
|---|---|---|---|
| `history.jsonl` (+ `.bak`) | ~420 KB | Prompt history with project + sessionId | MEDIUM |
| `settings.json` | 3 KB | hooks, permissions, plugins, model | MEDIUM |
| `CLAUDE.md` | 5 KB | Global instructions (also `~/AGENTS.md` → symlink) | MEDIUM |
| `~/.claude.json` | 56 KB | Client state / caches / onboarding | LOW |
| `commands/` | small | Custom slash commands | MEDIUM |
| `plugins/` | **1.9 GB** | Plugin cache; **~369** `SKILL.md` under cache | MEDIUM defs / SKIP blobs |
| `file-history/` | 484 KB | Edit history snapshots | LOW–MEDIUM |
| `tasks/`, `jobs/`, `teams/`, `daemon/` | small–28 MB | Task/daemon state | LOW |
| `security/` | **277 MB** | Includes venv / SDK tooling | SKIP |
| `stats-cache.json`, `backups/`, `cache/` | small | Caches | SKIP |
| `sessions/` | empty | — | — |

### 3.4 Claude Desktop app — mostly SKIP

`~/Library/Application Support/Claude/` (**12 GB**):

| Subpath | Size | Notes | Priority |
|---|---|---|---|
| `vm_bundles/` | 9.8 GB | VM images | **SKIP** |
| `Cache` / `Code Cache` | ~1.5 GB | Chromium caches | **SKIP** |
| `claude-code/` / `claude-code-vm/` | ~445 MB | Bundled runtime | **SKIP** |
| `IndexedDB/https_claude.ai_0.indexeddb.leveldb/` | 1.7 MB | Desktop web chat local DB | LOW (hard) |
| `local-agent-mode-sessions/` | 6.7 MB | Cowork skills plugin tree | LOW–MEDIUM |
| `claude_desktop_config.json` | small | MCP: `MCP_DOCKER`; preferences | MEDIUM |
| `claude-code-sessions/` | 272 KB | Session stubs | LOW |

### 3.5 `~/.local/share/claude/` — SKIP

1.0 GB of `ClaudeCode.app` + versioned binaries. Not user transcript data.

---

## 4. Shared / other harnesses

### 4.1 `~/.agents/` — MEDIUM

- `skills/source-command-{continual-learning,dream,learn,recap}/SKILL.md` (4 skills, 28 KB)
- Shared skill pack (Codex migration era)

### 4.2 `~/AGENTS.md` — MEDIUM

- Symlink → `~/.claude/CLAUDE.md`
- Separate Codex global: `~/.codex/AGENTS.md`

### 4.3 Warp — MEDIUM

`~/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite` (7.3 MB)

| Table | Rows |
|---|---|
| `ai_queries` | 368 |
| `agent_conversations` | 15 |
| `agent_tasks` | 23 |
| `project_rules` | present |
| `ai_blocks` / `last_ai_conversations` | 0 |

### 4.4 ChatGPT macOS app — MEDIUM

`~/Library/Application Support/com.openai.chat/`

- `conversations-v3-<userId>/*.data` — several conversation caches (190 KB–2.1 MB each)
- Also Codex-related pairing/task folders (often empty stubs)
- Format opaque; worth attempting if agentlog wants ChatGPT coverage

### 4.5 VS Code — LOW

- One chat session JSON under workspaceStorage (`12 KB`)
- Not a meaningful history store on this machine

### 4.6 cagent — SKIP

- `~/.config/cagent/` — first-run + uuid only
- `~/.cagent/` — present, minimal

### 4.7 CodexBar — SKIP (config only)

- `~/.codexbar/config.json`
- `~/Library/Application Support/CodexBar/` (108 KB)

### 4.8 MCP config locations (no secrets in this doc)

| File | Servers (names only) |
|---|---|
| `~/.cursor/mcp.json` | `aikido` |
| `~/Library/Application Support/Claude/claude_desktop_config.json` | `MCP_DOCKER` |
| `~/.codex/config.toml` `[mcp_servers]` | `node_repl`, `computer-use`, … |
| `~/Library/Application Support/fastmcp/` | version cache only |
| `~/Library/Application Support/chrome-devtools-mcp/` | telemetry state only |

**Security note for agentlog:** never index raw API keys/tokens from MCP configs or `auth.json`.

### 4.9 Not found

`~/.anthropic`, `~/.openai`, `~/.gemini`, `~/.hermes`, `~/.kimi`, `~/.continue`, `~/.aider*`, `~/.windsurf`, `~/.opencode`, `~/.cursor/rules`

---

## 5. Repository-level files

### 5.1 `AGENTS.md` (first-party; excluding `node_modules`)

| Path |
|---|
| `~/AGENTS.md` → `~/.claude/CLAUDE.md` |
| `~/.codex/AGENTS.md` |
| `~/ai_sec/AGENTS.md` |
| `~/side_projects/research-papers/AGENTS.md` |
| `~/side_projects/marketing-agent/marketing-agent-data-source/AGENTS.md` |
| `~/side_projects/ai-challenge-loan-ref/AGENTS.md` |
| `~/side_projects/solprobe/AGENTS.md` |
| (skill-bundled copies under `.claude/skills/.../AGENTS.md`, `.agents/skills/...` — treat as skill docs) |

**None under `~/Documents`.**

### 5.2 `CLAUDE.md` (first-party)

| Path |
|---|
| `~/.claude/CLAUDE.md` |
| `~/side_projects/research-papers/CLAUDE.md` |
| `~/side_projects/marketing-agent/marketing-agent-data-source/CLAUDE.md` |
| `~/side_projects/ai-challenge-loan-ref/CLAUDE.md` |
| `~/side_projects/solprobe/CLAUDE.md` |

### 5.3 Project agent directories (excluding venv/node_modules)

| Path | Notes |
|---|---|
| `~/side_projects/.claude/` | parent |
| `~/side_projects/adversarial-traffic-shaping/.claude/` | |
| `~/side_projects/research-papers/.claude/` (+ `skills/`) | also Obsidian vault |
| `~/side_projects/marketing-agent/…/.agents`, `.claude`, `.codex` | |
| `~/side_projects/ai-challenge-loan-ref/.agents`, `.claude`, `.codex` | |
| `~/side_projects/solprobe/.claude`, `.codex` (+ worktree copies) | |
| `~/side_projects/nanochat-solprobe/.agents`, `.claude`, `.codex` | |
| `~/side_projects/solShare/.cursor`, `.agents`, `.claude`, `.codex` | plans under `.cursor/plans/` |
| `~/ai_sec/.claude`, `.codex` | |

### 5.4 Cursor rules (`.mdc` / `.cursor/rules`)

- Global `~/.cursor/rules`: **missing**
- Repo `.cursor/rules`: **none found** under `side_projects` / Documents / ai_sec
- `solShare/.cursor/` has `CLOUD.md` + plan markdown (not ruleset)

### 5.5 Memory files (durable agent memory)

| Location | Files |
|---|---|
| `~/.codex/memories/` | MEMORY.md, raw_memories.md, memory_summary.md, rollout_summaries/ |
| `~/.claude/projects/*/memory/` | MEMORY.md + feedback_* + project_* |
| Repo trees | No standalone `MEMORY.md` at repo roots observed (Claude stores under `~/.claude/projects/…`) |

---

## 6. Obsidian

- App config: `~/Library/Application Support/obsidian/obsidian.json`
- **Single vault:** `~/side_projects/research-papers` (open)
- Contains `AGENTS.md`, `CLAUDE.md`, `.claude/`, and many newsletter briefs about agents/memory — **research content**, not harness transcripts
- Priority for agentlog: **LOW** (optional tag/search); do not treat as session store

---

## 7. Skills inventory (definitions worth indexing)

| Root | Approx. `SKILL.md` count | Priority |
|---|---|---|
| `~/.cursor/skills-cursor/` | 20 | MEDIUM |
| `~/.agents/skills/` | 4 | MEDIUM |
| `~/.codex/skills/` | ~10 | MEDIUM |
| `~/.claude/plugins/**` | ~369 (mostly cached plugins) | MEDIUM (index names) / SKIP vendor trees |
| `~/.codex/plugins/**` + `.tmp/plugins` | many curated/bundled | same |
| Per-repo `.claude/skills`, `.agents/skills` | project-specific | MEDIUM |

---

## 8. Recommended agentlog parsing plan

### Tier A — must parse (session truth)

1. `~/.codex/sessions/**/*.jsonl` (+ archived)
2. Cursor `state.vscdb` → reconstruct composers from `composerHeaders` + `bubbleId` + `composerData`
3. `~/.claude/projects/**/*.jsonl` (filter non-chat event types)
4. `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` (dedupe against vscdb when possible)

### Tier B — enrich / index

5. Codex `state_5.sqlite` threads graph  
6. Claude + Codex `MEMORY.md` trees  
7. `history.jsonl` / `session_index.jsonl` for timeline indexes  
8. Global + repo `AGENTS.md` / `CLAUDE.md` / settings / MCP server *names*  
9. Skill manifests (`SKILL.md` frontmatter)  
10. Warp `ai_queries` / `agent_conversations`  
11. Cursor `conversation-search.db` + `ai-code-tracking.db`

### Tier C — optional / hard

12. ChatGPT `.data` conversation caches  
13. Claude Desktop IndexedDB (LevelDB)  
14. Codex `logs_2.sqlite` (only if debugging harness itself)  
15. VS Code chatSessions  

### Tier D — skip

- Plugin/extension/VM/binary caches (`vm_bundles`, `~/.local/share/claude`, Cursor extensions, Claude `security/` venv, Chromium GPU/Cache dirs)
- Auth/token files
- `node_modules` / `.venv` AGENTS.md copies

### Deduping guidance

- Cursor agent-transcripts likely **overlap** composer bubbles in `state.vscdb` — prefer vscdb as canonical, keep transcripts as easy fallback / export
- Codex JSONL is canonical; `state_5.sqlite` is the index
- Claude Desktop IndexedDB may overlap Claude.ai cloud history more than Claude Code project JSONL — treat separately

### Format complexity cheat-sheet

| Source | Complexity | Strategy |
|---|---|---|
| Cursor agent-transcripts JSONL | Low | Stream JSON lines → normalize roles |
| Codex rollout JSONL | Medium | Map `type` → unified message/tool schema |
| Claude Code JSONL | Medium–high | Filter chat types; attach subagent files |
| Cursor `state.vscdb` | High | SQL extract + JSON parse + join by composerId |
| Warp SQLite | Low–medium | Direct SQL |
| ChatGPT `.data` | High / unknown | Probe magic/headers; may be protobuf/encrypted |
| Claude IndexedDB LevelDB | High | Optional later |

---

## 9. Total data volume (approx.)

| Bucket | Size |
|---|---|
| Codex home `~/.codex` | 1.3 GB |
| Claude Code home `~/.claude` | 2.2 GB |
| Cursor home `~/.cursor` | 1.1 GB |
| Cursor App Support | 1.9 GB |
| Claude Desktop App Support | 12 GB (≈10 GB VM/cache skip) |
| Claude local share | 1.0 GB (skip) |
| Codex Desktop App Support | 205 MB (mostly skip) |
| Warp + ChatGPT + Obsidian app | ~54 MB |
| **Parse-worthy conversational/memory subset** | **~1.05 GB** |
| **Gross agent-related footprint** | **~20 GB** |

---

## 10. Quick path checklist for implementers

```text
# Transcripts
~/.codex/sessions/
~/.codex/archived_sessions/
~/.claude/projects/
~/.cursor/projects/*/agent-transcripts/
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
~/Library/Application Support/Cursor/User/globalStorage/conversation-search.db

# Memory
~/.codex/memories/
~/.codex/memories_1.sqlite
~/.claude/projects/*/memory/

# Config / rules
~/.codex/config.toml
~/.codex/AGENTS.md
~/.claude/settings.json
~/.claude/CLAUDE.md
~/AGENTS.md
~/.cursor/mcp.json
~/Library/Application Support/Claude/claude_desktop_config.json
~/side_projects/**/AGENTS.md
~/side_projects/**/CLAUDE.md

# Skills
~/.cursor/skills-cursor/
~/.agents/skills/
~/.codex/skills/
~/.claude/plugins/          # index only
~/side_projects/**/.claude/skills/
~/side_projects/**/.agents/skills/

# Secondary
~/Library/Application Support/dev.warp.Warp-Stable/warp.sqlite
~/Library/Application Support/com.openai.chat/conversations-v3-*
```

---

*Generated by filesystem inventory on 2026-08-09. Re-run when new harnesses are installed or after large session growth.*
