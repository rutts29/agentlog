# Hermes Agent — Deep Architecture Audit

**Repo:** [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)  
**Local clone (read-only):** `.research/hermes-agent/`  
**Audit date:** 2026-08-09  
**Method:** Static analysis only (clone + file reads). No installs, no script execution, no running repo code.  
**Relevance:** Patterns for **agentlog** — a local-first command center for AI coding agents.

---

## Executive summary

Hermes Agent is a full local-first agent runtime (not just a chat UI): SQLite session store with FTS5, file-backed curated memory, agentskills-style procedural memory, multi-provider LLM routing, a messaging gateway, and a **durable Kanban board** for multi-profile agent collaboration.

For agentlog, the highest-value ideas are:

1. **Kanban as durable work queue** (SQLite + CAS claims + comments as protocol) — not in-process swarms.
2. **Layered memory** (bounded curated files + FTS session search + optional external providers).
3. **Clear split:** `delegate_task` (RPC/fork-join) vs Kanban (persistent peer queue).
4. **Frozen system-prompt snapshots** to protect prefix cache while still writing memory mid-session.
5. **Tool registry + toolsets** with gating by context (worker vs orchestrator).

Avoid: cloning Hermes’ mega-files (`run_agent.py` ~8k LOC, `cli.py` ~18k, `gateway/run.py` ~27k, `kanban_db.py` ~10k) or treating Hermes as a library to import wholesale.

---

## Architecture diagram

```text
+-----------------------------------------------------------------------------+
|                         ENTRY SURFACES                                       |
|  CLI (cli.py)  |  TUI (ui-tui/)  |  Desktop (apps/desktop)  |  Web dash      |
|  Gateway (gateway/run.py)  |  ACP  |  Batch / Cron  |  MCP serve             |
+---------------+---------------------------+---------------------------------+
                |                           |
                v                           v
+-------------------------------+  +------------------------------------------+
|     AIAgent (run_agent.py)    |  |     Kanban subsystem                      |
|  prompt_builder / system tiers|  |  hermes_cli/kanban_db.py  (~/.hermes/     |
|  runtime_provider resolution  |  |    kanban.db or boards/<slug>/kanban.db) |
|  tool dispatch (model_tools)  |  |  tools/kanban_tools.py (model surface)  |
|  context_compressor           |  |  gateway/kanban_watchers.py (dispatcher)|
|  memory_manager + MemoryStore |  |  plugins/kanban/dashboard + desktop UI  |
+-------+-----------+-----------+  +---------------+--------------------------+
        |           |                              |
        v           v                              v
+-------------+ +--------------+        +---------------------+
| SessionDB   | | Tool backends|        | Worker OS processes |
| state.db    | | terminal x7  |        | hermes -p <profile> |
| FTS5 search | | browser, MCP |        | HERMES_KANBAN_* env |
| memories/   | | file, web    |        | claim + heartbeat   |
| skills/     | |              |        +---------------------+
+-------------+ +--------------+
```

**Core design patterns**

| Pattern | Where | Role |
|--------|--------|------|
| Synchronous agent loop | `run_agent.py` (`AIAgent`) | One turn → LLM → tools → loop until done |
| Declarative provider profiles | `providers/base.py`, `plugins/model-providers/` | Auth/API quirks without scattering flags |
| Shared runtime resolver | `hermes_cli/runtime_provider.py` | `(provider, model)` → `(api_mode, key, base_url)` |
| Self-registering tools | `tools/registry.py` + each `tools/*.py` | Import-time `registry.register()` |
| Toolsets | `toolsets.py` | Named bundles; platform/profile presets |
| Frozen memory snapshot | `tools/memory_tool.py` | Prompt stable for cache; disk live |
| Pluggable memory (single-select) | `agent/memory_manager.py` | One external provider at a time |
| Pluggable context engine | `agent/context_engine.py`, `plugins/context_engine/` | Default: lossy compressor |
| Durable kanban queue | `hermes_cli/kanban_db.py` | WAL + `BEGIN IMMEDIATE` + CAS claims |
| Profiles as identity | `hermes_cli/profiles.py` | Separate `HERMES_HOME` / memory / skills |
| Plugin hooks | `hermes_cli/plugins.py` `VALID_HOOKS` | Observer + some control hooks |
| Progressive skills | `skills/`, `agent/learn_prompt.py` | agentskills.io SKILL.md on demand |

---

## Multi-provider / multi-model

**Layers**

1. **`ProviderProfile`** (`providers/base.py`) — declarative: env vars, base URL, `api_mode` (`chat_completions` / Responses / Anthropic), vision flags, aux model, message/request hooks.
2. **Registry** (`providers/__init__.py`) — loads `plugins/model-providers/<name>/` (bundled + `$HERMES_HOME`), user overrides win; also legacy `providers/*.py`.
3. **Auth** (`hermes_cli/auth.py` `PROVIDER_REGISTRY`) — credentials, OAuth (Nous, Codex, etc.), pools.
4. **Runtime resolution** (`hermes_cli/runtime_provider.py`) — used by CLI, gateway, cron, ACP, aux LLM; URL→api_mode heuristics; credential pools (`agent/credential_pool.py`); fallback providers (docs: `fallback-providers.md`).
5. **API adapters** — `agent/anthropic_adapter.py`, `codex_responses_adapter.py`, `gemini_native_adapter.py`, Bedrock, etc., selected by `api_mode`.

**Kanban-specific:** tasks can set `model_override`, `provider_override`, `reasoning_effort` (`kanban_db` schema); dispatcher passes `-m` / `--provider` / `--reasoning` to workers.

**Adoption note for agentlog:** Prefer a small declarative provider table + one resolver. Do not fork Hermes’ full auth/OAuth surface unless needed.

---

## Memory & cross-session learnings

Hermes does **not** rely on a single vector DB. It stacks complementary stores:

### 1. Built-in curated memory (primary)

- Files: `~/.hermes/memories/MEMORY.md` and `USER.md` (`tools/memory_tool.py`).
- Delimiter: `§` between entries.
- Soft caps (~2200 / ~1375 chars) — overflow errors; agent must consolidate (no silent drop).
- **Frozen snapshot:** injected at session start into system prompt; mid-session writes hit disk immediately but do not refresh the prompt (prefix-cache preservation).
- Threat scanning on write (`tools/threat_patterns.py`, strict scope).
- Drift guard: refuses writes if on-disk content would not round-trip through the parser.

### 2. Session history + FTS5

- DB: `~/.hermes/state.db` (`hermes_state.py` / `hermes_state_common.py` SCHEMA_VERSION 25).
- Tables: `sessions`, `messages`, `session_model_usage`, FTS indexes, compression locks, `async_delegations`, etc.
- Tool: `tools/session_search_tool.py` — discover (FTS + lineage dedupe + bookends), scroll, browse. **No LLM summarization in the search path** (returns real messages).
- Hides sources: `kanban`, `subagent`, `tool`; demotes `cron` in ranking.

### 3. Skills (procedural memory)

- `SKILL.md` under `~/.hermes/skills/` (agentskills.io).
- Progressive disclosure; `/learn` → `agent/learn_prompt.py` → agent authors via `skill_manage`.
- **Curator** (`agent/curator.py`) — idle-triggered aux agent archives/consolidates agent-created skills (no auto-delete).

### 4. External memory providers (optional, single-select)

- ABC: `agent/memory_provider.py`; orchestration: `agent/memory_manager.py`.
- Plugins: `plugins/memory/{honcho,mem0,hindsight,supermemory,byterover,...}`.
- Lifecycle: `initialize` → `system_prompt_block` → `prefetch` / `queue_prefetch` → `sync_turn` → tools; hooks for compress / session end / delegation.

### 5. Learning graph (UI)

- `agent/learning_graph.py` — skills + MEMORY/USER chunks as nodes; lexical edges for desktop “learning made visible” view — not a runtime coordinator.

**How learnings propagate across agents/sessions**

| Mechanism | Scope | Propagation |
|-----------|--------|-------------|
| MEMORY.md / USER.md | Per profile (`HERMES_HOME`) | Next session system prompt |
| Skills + curator | Per profile skills dir | On-demand load / slash commands |
| Session FTS | Shared state.db for that home | Explicit `session_search` tool |
| External provider | Provider-scoped identity | Prefetch/sync per turn |
| Kanban comments / parent results | Shared `kanban.db` across profiles | Injected into worker context |
| Profiles | Isolation by design | Docs warn: don’t share one home across two writers |

---

## Context / memory limits

| Concern | Approach | Key files |
|---------|----------|-----------|
| Context window | `ContextCompressor` — protect head/tail, summarize middle with aux model; tool-output prune pre-pass | `agent/context_compressor.py` |
| Engine plug-in | `ContextEngine` ABC | `agent/context_engine.py` |
| Prompt tiers | stable → context → volatile | `agent/prompt_builder.py`, `system_prompt.py` |
| Prompt caching | Anthropic breakpoints | `agent/prompt_caching.py` |
| Memory size | Char budgets; fail on overflow | `tools/memory_tool.py` |
| Tool errors | Cap ~2048 chars at registry | `tools/registry.py` |
| Kanban worker prompt | Caps on body, comments, prior runs, field bytes | `kanban_db.build_worker_context` |
| Trivial turns | Skip memory prefetch on greetings/slash | `memory_provider.is_trivial_prompt` |
| Compaction in search | Exclude compaction marker prefixes from bookends | `session_search_tool.py` |

---

## Task management & Kanban

### Concept

Kanban is a **durable message queue + state machine**, shared across Hermes profiles, not an in-process swarm.

**Statuses:** `triage | todo | scheduled | ready | running | blocked | review | done | archived`  
**Links:** parent→child in `task_links`; dispatcher promotes `todo→ready` when parents are done/archived.  
**Comments:** inter-agent protocol; full thread (capped) enters worker context.  
**Workspaces:** `scratch` (ephemeral), `dir:<abs>`, `worktree` / `worktree:<path>`.  
**Boards:** isolation via separate DBs under `kanban/boards/<slug>/`; default board keeps `~/.hermes/kanban.db`.  
**Tenants:** soft namespace filter within a board.

### Two surfaces, one DB layer

1. **Agents:** `kanban_*` tools (`tools/kanban_tools.py`) — portable across terminal backends (tools run in-process, not shell-inside-sandbox).
2. **Humans/automation:** `hermes kanban …`, slash `/kanban`, dashboard/desktop UI.

Gating:

- Lifecycle tools when `HERMES_KANBAN_TASK` set (dispatcher worker) or profile has `kanban` toolset.
- List/unblock orchestrator-only (`_check_kanban_orchestrator_mode`).
- **`delegate_task` children cannot mutate Kanban** (tool + DB guards).

### Dispatcher

- Embedded in gateway by default (`gateway/kanban_watchers.py`): reclaim stale/crashed claims, promote ready, CAS claim, spawn `hermes -p <assignee>` with env pins.
- Claim TTL (default 15m) + heartbeat; failure circuit breaker; typed `block_kind` (`dependency|needs_input|capability|transient`); unblock-loop breaker → `triage`.
- Concurrency: WAL + `BEGIN IMMEDIATE` + CAS on `status`/`claim_lock` — losers see 0 rows, no distributed lock.

### Worker context assembly

`build_worker_context()` order: title → body → prior attempts → done parents’ summaries → assignee role history → comments → attachments (absolute paths).

### Kanban vs `delegate_task`

| | `delegate_task` | Kanban |
|--|-----------------|--------|
| Shape | In-process / thread-pool RPC | Durable SQLite queue |
| Identity | Anonymous child | Named profile |
| Resume | No | Block/unblock/reclaim |
| HITL | Weak | Comments, triage, notify subs |
| Audit | Lost on compress | Rows forever |
| Coordination | Hierarchical | Peer |

Code: `tools/delegate_tool.py` (blocks recursive delegate, memory, clarify, cron for children).

### Novel coordination patterns

- Comments as the API between agents/humans.
- Typed blocks to avoid cron↔worker thrash.
- Goal mode (`goal_mode` + aux judge loop) on a card.
- Notify subscriptions (`kanban_notify_subs`) → gateway delivery when tasks complete/block.
- Multi-board hard isolation via env `HERMES_KANBAN_BOARD`.
- Decomposition helpers: `hermes_cli/kanban_decompose.py`, `kanban_swarm.py`, `kanban_specify.py`.

---

## Plugin / tool system

**Tools**

- Central registry: `tools/registry.py` — schema, handler, toolset, `check_fn`, discovery cache.
- Grouping: `toolsets.py` (~28 toolsets, 70+ tools).
- MCP: `tools/mcp_tool.py` dynamic tools.
- Approval: `tools/approval.py` + hooks `pre_approval_request` / `post_approval_response`.

**Plugins** (`hermes_cli/plugins.py`)

Sources: bundled `plugins/`, `~/.hermes/plugins/`, project `.hermes/plugins/`, pip entry points.  
Manifest `plugin.yaml` + `register(ctx)`.  
Hooks include: `pre/post_tool_call`, `pre/post_llm_call`, `pre_verify`, session lifecycle, `subagent_*`, `pre_gateway_dispatch`, kanban lifecycle (`kanban_task_claimed|completed|blocked`), skill lifecycle, etc.  
Special single-select kinds: memory providers, context engines, model-provider plugins.

**Desktop kanban plugin:** `apps/desktop/src/plugins/kanban/` — SDK contributions (routes, sidebar, statusbar); REST via `plugins/kanban/dashboard/plugin_api.py` (`/api/plugins/kanban`); WS tails `task_events`.

---

## UI architecture

| Surface | Stack | Role |
|---------|-------|------|
| CLI | `cli.py` (very large) | Interactive chat, slash commands |
| TUI | `ui-tui/` (TS) + `tui_gateway/` | Structured gateway protocol |
| Web dashboard | `web/` (Vite/React) + `hermes_cli/web_server.py` | Admin: sessions, config, cron, skills, chat PTY, kanban |
| Desktop | `apps/desktop/` Electron + plugin SDK | Command center; kanban board plugin |
| Docs site | `website/` Docusaurus | Product docs |

**Kanban UI flow**

1. Board query → columns by status (`board.tsx`).
2. Optimistic drag-and-drop with workflow checks.
3. Drawer for comments/attachments/overrides (`drawer.tsx`).
4. Board switcher persists in `localStorage` (does not force CLI `current` board).
5. Orchestration panel (`orchestration.tsx`) for fleet/pipeline views.
6. Writes go through shared `kanban_db` (same as CLI).

---

## Code deep-dive: key files

| Path | Purpose | Approx size |
|------|---------|-------------|
| `run_agent.py` | `AIAgent` conversation loop | ~8.3k LOC |
| `cli.py` | HermesCLI | ~18.7k |
| `gateway/run.py` | Messaging gateway | ~27.8k |
| `hermes_state.py` | Session DB API | ~10.5k |
| `hermes_state_common.py` | SCHEMA_SQL v25 | — |
| `hermes_cli/kanban_db.py` | Kanban schema + CAS + worker context | ~10.4k |
| `tools/kanban_tools.py` | Model tool surface | ~2.3k |
| `gateway/kanban_watchers.py` | Dispatcher + notifier | ~1.5k |
| `agent/context_compressor.py` | Context limits | ~7.4k |
| `tools/memory_tool.py` | MEMORY/USER stores | — |
| `agent/memory_manager.py` | External memory orchestration | — |
| `tools/registry.py` | Tool registry | — |
| `tools/delegate_tool.py` | Subagent RPC | — |
| `hermes_cli/runtime_provider.py` | Provider resolution | — |
| `providers/base.py` | ProviderProfile | — |
| `agent/curator.py` | Skill maintenance | — |
| `tools/session_search_tool.py` | Cross-session recall | — |
| `apps/desktop/src/plugins/kanban/*` | Board UI | — |
| `plugins/kanban/dashboard/plugin_api.py` | Kanban REST/WS | — |

---

## Database schemas

### Session store (`~/.hermes/state.db`)

Defined in `hermes_state_common.py` `SCHEMA_SQL`:

- `sessions` — lineage (`parent_session_id`), model/billing, compression failure fields, profile, archive/pin
- `messages` — roles, tool_calls, reasoning fields, `active`/`compacted`, `api_content`
- `session_model_usage` — per-model token/cost rollups
- `system_prompts` — hash-deduped prompts
- `gateway_routing`, `compression_locks`, `async_delegations`
- FTS5 over messages (external-content layout + trigram; CJK optional)

### Kanban (`~/.hermes/kanban.db` or board path)

Defined in `hermes_cli/kanban_db.py` `SCHEMA_SQL`:

- `tasks` — status, assignee, workspace, claims, failures, overrides, goal_mode, block_kind, …
- `task_links` — DAG dependencies
- `task_comments` — protocol log
- `task_events` — append-only audit (WS tail source)
- `task_runs` — per-attempt PID/heartbeat/outcome
- `task_attachments` — metadata + on-disk blobs
- `kanban_notify_subs` — gateway notification routing

### Built-in memory

Not SQL — markdown files under `memories/`.

### Comparison to agentlog today

agentlog (`src/agentlog/db/schema.py`) already uses WAL SQLite + FTS5 on messages — aligned with Hermes’ session store spirit. Hermes adds richer session lineage, kanban DB, and file-backed curated memory that agentlog does not have (and may not need as an analyzer).

---

## Feature analysis (answers)

### Learnings across agents/sessions

Curated files + skills + FTS search + optional providers + kanban handoffs. Profiles isolate by default; kanban is the intentional cross-profile bus.

### Context/memory limits

Compressor + char-capped memory + tool error caps + bounded worker context. Fail-loud memory overflow forces agent consolidation.

### Orchestration approach

Three tiers:

1. Single-agent loop + tools  
2. `delegate_task` for short parallel fork-join  
3. Kanban dispatcher for durable multi-profile pipelines  

Cron is a fourth: scheduled fresh agents with delivery (`cron/`).

### Task state tracking

Kanban rows + runs + events; CAS claims; heartbeats; circuit breakers. Session todos are separate (`tools/todo_tool.py`) — ephemeral in-conversation, not the multi-agent board.

### Novel coordination

Typed blocks, comment protocol, board isolation, goal-mode judge loop, notify subs, forbid delegated children from mutating the board, scratch artifact promotion on complete.

---

## What agentlog should adopt

| Pattern | Why | Hermes references | Suggested agentlog shape |
|---------|-----|-------------------|--------------------------|
| Durable task board (SQLite) | Survives restarts; HITL; audit | `hermes_cli/kanban_db.py`, status machine | Optional `tasks` / `task_events` for “work across harnesses” |
| Comments as handoff protocol | Model-readable, human-editable | `task_comments`, `build_worker_context` | Thread on each task, not hidden chat |
| CAS claim + heartbeat | Safe multi-worker without Redis | claim_lock / claim_expires | Same if agentlog ever dispatches |
| Typed block reasons | Stop retry storms | `VALID_BLOCK_KINDS`, block_recurrences | `blocked_reason` enum |
| delegate vs board split | Prevents wrong primitive | Kanban docs + `delegate_tool.py` | Document: “subagent” ≠ “board task” |
| FTS5 + lineage-aware recall | agentlog already close | `session_search_tool.py`, FTS schema | Keep FTS; add lineage/parent session UX |
| Frozen snapshot for UI state | Cache-friendly mental model | `memory_tool.py` | If showing “user profile”, snapshot per view |
| Tool/capability gating by role | Worker ≠ orchestrator | `_check_kanban_*` | Role-scoped actions in command center |
| Plugin contribution UI | Desktop kanban as SDK plugin | `apps/desktop/src/plugins/kanban/` | Board as a plugin to agentlog UI, not core |
| Progressive skills index | Low token tax | skills docs, ≤60-char descriptions | If ingesting skills: store short descriptions separately |

---

## What to avoid

1. **Monolithic modules** — `run_agent.py` / `cli.py` / `gateway/run.py` / `kanban_db.py` as gravity wells. Keep agentlog packages small.
2. **Importing Hermes as a dependency** — deep coupling, huge surface, provider/auth complexity, telemetry risk (treat as untrusted; patterns only).
3. **Unbounded auto-memory** — Hermes’ char limits and fail-on-overflow are the right lesson; don’t auto-compact by silently dropping.
4. **Shelling out for board mutations from sandboxed workers** — Hermes moved to in-process tools for a reason.
5. **Shared home for concurrent agents** — documented corruption/compounding risk for MEMORY.md.
6. **In-process-only multi-agent for long work** — use durable queue when work must survive restarts.
7. **LLM summarization inside search** — Hermes removed it from session_search; prefer raw windows for analyzers like agentlog.
8. **Rebuilding full messaging gateway** — out of scope for a coding-agent command center.

---

## Integration possibilities

| Option | Feasibility | Notes |
|--------|-------------|-------|
| **A. Read Hermes DBs read-only** | High | Ingest `~/.hermes/state.db` and `kanban.db` as another harness in agentlog (like Claude/Codex/Cursor). Schema is SQLite; map sessions/messages/tasks. |
| **B. Adopt Kanban schema ideas** | High | Reimplement a slim subset in agentlog DB; do not copy 10k-line module. |
| **C. Embed Hermes UI/plugin** | Low | Desktop plugin SDK is Hermes-specific (`@hermes/plugin-sdk`). |
| **D. Call Hermes CLI/API** | Medium | Possible for “open in Hermes” but coupling + security (session tokens, local agent). Prefer DB read or export. |
| **E. Shared agentskills format** | High | Both can speak SKILL.md; agentlog already has `skill_exposures`. |
| **F. Memory provider protocol** | Low–medium | Interesting if agentlog becomes an agent; overkill for analytics-only. |

**Recommended path for agentlog**

1. Add an optional **Hermes ingest adapter** (static parsers over SQLite files — no executing Hermes).  
2. Design agentlog’s own **command-center board** inspired by Kanban (statuses, links, comments, events) for cross-harness work items.  
3. Surface **session FTS + lineage** (parent_session / compaction) already present in Hermes state.db.  
4. Keep orchestration out of agentlog’s core until there’s a clear dispatcher need.

---

## Risk / trust notes (static)

- Large install surface (Node + Python + many optional deps). Treat as untrusted; do not run installers or repo scripts for research.
- Dashboard auth is local session-token / OAuth oriented; plugin routes document LAN exposure constraints.
- Memory/context scanners exist (`threat_patterns`) — useful idea; still not a substitute for sandboxing agents.

---

## Appendix: directory map (high level)

```text
hermes-agent/
├── run_agent.py, cli.py, model_tools.py, toolsets.py
├── hermes_state*.py          # session SQLite
├── agent/                    # loop helpers, memory, compression, learning
├── tools/                    # self-registering tools + environments/
├── hermes_cli/               # CLI, kanban_db, web_server, plugins, auth
├── gateway/                  # messaging + kanban dispatcher
├── providers/, plugins/      # model providers, memory, kanban, platforms
├── skills/, optional-skills/
├── ui-tui/, tui_gateway/
├── web/, apps/desktop/
├── cron/, acp_adapter/
└── website/docs/             # architecture + kanban feature docs
```

Primary docs used (also in clone):

- `website/docs/developer-guide/architecture.md`
- `website/docs/user-guide/features/kanban.md`
- `website/docs/user-guide/features/memory.md`
- `website/docs/user-guide/features/skills.md`
- `website/docs/user-guide/features/delegation.md`
- `website/docs/user-guide/features/web-dashboard.md`

---

*End of audit. All conclusions from static reads of the cloned tree under `.research/hermes-agent/`.*
