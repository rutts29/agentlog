# agentlog Research Council Synthesis

**Date:** 2026-08-09  
**Inputs:** [Hermes Agent audit](./hermes-agent-audit.md) and [Awesome Agent Orchestrators audit](./awesome-orchestrators-audit.md)

## Executive decision

agentlog should be a **local observability and learning plane for coding agents**, not an agent runtime.

The coding harnesses remain the execution plane: they own processes, permissions, tools, worktrees, and live conversation state. agentlog reads their durable on-disk state, normalizes it, derives evidence-backed insights, and makes useful knowledge available to later sessions. A session must appear in agentlog even if it was launched directly from a terminal or IDE.

This resolves the apparent conflict between the two audits:

- The orchestrators audit is right that agentlog must stay a lens over provider state. Owning execution would undermine its cross-harness coverage.
- The Hermes audit is right that SQLite state machines, append-only events, bounded context, and explicit handoffs are strong patterns.
- Hermes's **dispatcher-backed Kanban is not the right product surface for agentlog**. Borrow its durable-state mechanics for an Attention Inbox and a learning-review queue, but do not add task claiming, heartbeats, worker spawning, worktree management, or model overrides to v1.

The practical product promise is:

> See what every coding agent did, understand what worked, and carry verified learnings into the next relevant session.

## 1. What to adopt

### 1.1 On-disk state as the source of truth

This is the strongest common finding. Claude Command Center, codecast, and agent-console discover sessions created outside their own UI. Hermes also keeps durable session and board state in SQLite.

**Adopt**

- Read provider-owned files and databases without requiring users to launch agents through agentlog.
- Treat raw artifacts as immutable evidence. Reprocessing may change normalized or derived records, but must not alter source transcripts.
- Publish a harness capability matrix: transcript coverage, lineage, live status, tools, skills, tokens, cost, and resume-link support. Unknown must remain unknown rather than being inferred as false.
- Go deep on Codex, Claude Code, and Cursor before expanding breadth.

**Research references**

- Orchestrators audit §3.1: CCC's engine support matrix and Cursor metadata-only honesty.
- Orchestrators audit §3.3: agent-console's transcript discovery and explicit distinction between discovery and live attachment.
- Orchestrators audit §4.1: provider capability matrix.

**Mapping to agentlog**

- `src/agentlog/ingest/base.py` already defines the correct boundary with `TranscriptAdapter.discover()` and `parse_chunk()`.
- `src/agentlog/ingest/pipeline.py` already supports incremental, failure-isolated ingestion.
- `artifacts.parsed_offset`, `content_hash`, and `parser_version` in `src/agentlog/db/schema.py` are the right checkpoint primitives.
- The current adapter registry is hard-coded in `ingest/pipeline.py`, and `Harness` is a closed enum in `normalize/models.py`. Replace those two constraints with a registry plus capability manifests before adding Warp or Hermes.

### 1.2 A normalized evidence ledger, then derived claims

The dashboard design correctly separates factual views from the Insights page. Preserve that split in the database:

1. **Evidence layer:** artifacts, sessions, messages, tool events, skill exposures, lineage.
2. **Derived layer:** statuses, corrections, frustration, failures, handoffs, skill-effectiveness observations, and proposed learnings.
3. **Publication layer:** user-approved knowledge exported to agent-readable surfaces.

Every derived claim should store:

- source session and message/window IDs;
- extractor name, schema version, prompt version, provider, and model;
- input hash and extraction timestamp;
- confidence and structured evidence;
- lifecycle status such as `candidate`, `approved`, `rejected`, `published`, or `superseded`.

This extends the existing `exchange_windows` design rather than replacing it. `analysis/windows.py` already creates stable user/assistant units with `input_hash`; those windows should be the unit of semantic extraction and cache invalidation.

### 1.3 FTS-first search with real-message windows

Hermes's `tools/session_search_tool.py` returns real messages, uses lineage-aware deduplication, and avoids LLM summarization in the retrieval path. Guild and codecast add semantic retrieval, but both audits recommend starting with keyword search.

**Adopt now**

- Keep SQLite FTS5 as the primary search path.
- Return matching message plus bounded preceding/following messages.
- Group duplicate hits from parent/child sessions and expose lineage.
- Load long transcripts in windows rather than returning an entire JSONL-derived session.

**Adopt later**

- Add embeddings only after FTS quality and corpus size justify them.
- Combine BM25/FTS and vector results with reciprocal-rank fusion, following Guild's documented hybrid pattern.

**Mapping to agentlog**

- `messages_fts` and `Repository.search_messages()` already provide the foundation.
- The API should add cursor-based transcript windows and bookends, not an LLM-generated search summary.

### 1.4 Session lineage and handoff objects

codecast treats parent/child hierarchy and handoffs as first-class. Guild distinguishes journal, durable lore, and a brief for the next session. Hermes's `build_worker_context()` orders parent results, role history, comments, and attachments into a bounded handoff.

**Adopt**

- Render session lineage as a graph and as simple parent/child navigation.
- Introduce a structured `handoff` insight: summary, unresolved questions, changed assumptions, affected components, and source sessions.
- Keep handoffs evidence-linked and bounded. They are derived context packages, not replacement transcripts.

**Mapping to agentlog**

- `sessions.parent_session_id` already exists and all three current adapters populate it.
- Codex `state_5.sqlite` spawn edges should enrich, not replace, JSONL lineage.
- Claude and Cursor subagent paths already map to parents in their adapters.

### 1.5 An Attention Inbox, not a task board

CCC, codecast, diri, and agent-console converge on a high-value triage surface. Use the codecast ordering as the starting point:

1. pinned;
2. needs input;
3. working;
4. error;
5. idle;
6. complete/deferred.

Each status must carry `status_source` and `status_confidence`. Transcript-derived status, process-derived status, and user-set state are not equivalent.

Borrow from Hermes:

- append-only event history, analogous to `task_events`;
- typed reasons, analogous to `block_kind`;
- comments/notes as a human-readable audit trail;
- optimistic UI updates with server-side transition validation.

Do **not** borrow:

- `gateway/kanban_watchers.py` dispatch;
- CAS worker claims and heartbeats;
- task DAG promotion;
- worker process spawning;
- worktree assignment or per-task model/provider overrides.

The Inbox answers “what deserves my attention?” It does not answer “which worker should agentlog dispatch?”

### 1.6 Live refresh without owning the agent

CCC uses SSE at `/api/sessions/events`; Better Agent uses a WebSocket architecture. agentlog should stream **ingest and derived-state changes**, not terminal output.

Recommended shape:

- A filesystem watcher or short debounce loop marks artifacts dirty.
- The existing incremental pipeline reparses only changed prefixes.
- The backend emits `session_added`, `session_updated`, `insight_ready`, and `ingest_failed`.
- React invalidates focused TanStack Query keys.
- Nightly ingestion remains the correctness backstop.

SSE is sufficient for v1 because the dashboard mostly receives one-way updates. WebSockets are unnecessary until agentlog has substantial bidirectional live controls.

### 1.7 UI concepts worth taking

Adopt these directly into `docs/dashboard-design.md`:

- **Needs-you Inbox on Overview** from CCC/codecast/agent-console.
- **Windowed transcript viewer** from CCC.
- **Pinned → Working → Needs Input → Idle → Deferred ordering** from codecast.
- **Separate current state from historical outcome**: `needs_input` or `working` describes what a session needs now; `corrected` or `abandoned` describes what happened.
- **Parent/child session navigation** from codecast and GraphCode.
- **Cost as cost-per-quality**, combining agent-deck's cost surface with agentlog's correction and retry rates.
- **Cmd+K over sessions, projects, skills, and learnings** from codecast.
- **Stable session title from the first user prompt** from agent-console; do not replace a user's recall key with a later LLM summary.
- **Session peek drawer** inspired by Hermes's `drawer.tsx`, preserving table/filter context while keeping full session detail as the deep-reading route.
- **AI blame/attribution** from codecast, joined with Cursor's `ai-code-tracking.db` and Git history.
- **Capability matrix** in Settings/About and beside unavailable filters.

Keep the existing dashboard principles: dense, read-only by default, URL-addressable filters, drill-down to evidence, and explicit confidence.

### 1.8 A small, declarative model-provider resolver

Hermes separates declarative `ProviderProfile` (`providers/base.py`), registry loading (`providers/__init__.py`), runtime resolution (`hermes_cli/runtime_provider.py`), and API adapters. agentlog needs a much smaller version for semantic extraction.

Adopt:

- provider profile: API mode, base URL, credential environment variable, model capabilities, and local/remote privacy class;
- one resolver from `(provider, model)` to an analysis client;
- a narrow adapter interface for structured extraction and embeddings;
- provider/model/prompt/schema recorded on every extraction run;
- deterministic-only mode as a fully supported configuration;
- optional local-model mode and bring-your-own-key remote mode.

Do not import Hermes's OAuth, credential-pool, fallback, or gateway surface.

Critically, keep **two abstractions separate**:

- a **harness adapter** reads Codex, Claude Code, Cursor, Warp, or Hermes state;
- an **LLM provider adapter** performs semantic analysis with Anthropic, OpenAI, Ollama, or another model API.

Conflating those axes will make parser capabilities, privacy, and model routing difficult to reason about.

## 2. What to avoid

### 2.1 Owning execution

Do not build a PTY daemon, terminal multiplexer, worktree launcher, permission proxy, agent spawner, merge queue, or resume protocol. Those features:

- make hand-launched sessions invisible;
- duplicate native harness behavior;
- create a large security and recovery surface;
- move agentlog into the crowded orchestrator category;
- distract from transcript analytics, which is the differentiated product.

Native resume links or commands are acceptable. Reimplementing native chat is not.

### 2.2 Kanban as the product

Hermes's Kanban is good engineering for Hermes because Hermes executes durable work. It is not evidence that every command center needs a board. The orchestrators audit also notes that the category-defining vibe-kanban is sunsetting amid many similar products.

Do not add generic `todo → ready → running → review → done` cards to v1. If a later board appears, it should be a projection over sessions, attention events, handoffs, and learning proposals—not a second source of task truth.

### 2.3 Automatic truth promotion

Oxy's feature request is valuable, but automatic propagation must not mean “an LLM silently rewrites every AGENTS.md.”

Avoid:

- publishing unsupported claims from one conversation;
- appending forever to a global Markdown file;
- losing provenance when summarizing;
- leaving contradictory facts active simultaneously;
- concurrently editing one shared memory file from multiple agents;
- silently dropping old entries to fit a prompt budget.

The correct rule is **automatic detection and routing, controlled promotion**. High-confidence policies can later support auto-approval, but the system must always preserve evidence, supersession, and rollback.

### 2.4 Pretending active chats inherit changed files

A file update reliably affects future sessions only if the harness reads it at startup. Existing sessions will not necessarily re-read `AGENTS.md`, `CLAUDE.md`, or a skill file.

For v1, promise propagation to future sessions and on-demand retrieval. Active-session propagation requires a harness hook, MCP call, or explicit refresh command and should be presented as a later capability.

### 2.5 Prompt and memory bloat

Do not inject the full learning corpus into every session. Hermes's bounded `MEMORY.md`/`USER.md`, frozen session snapshot, progressive skills, and capped worker context are the right constraints.

Use:

- short indexes and descriptions;
- topic/dependency-scoped retrieval;
- bounded handoff briefs;
- fail-loud size limits;
- supersession and consolidation instead of silent truncation.

### 2.6 LLM-derived operational status

Do not spend model calls deciding whether a session process is alive or whether a transcript changed. Use files, timestamps, process metadata when available, and deterministic state rules. Reserve LLM calls for semantic questions such as correction intent, frustration, learning extraction, and handoff synthesis.

### 2.7 Breadth before depth

Do not claim equal support for ten harnesses. Current code deeply supports only transcript JSONL from Codex, Claude Code, and Cursor agent transcripts. Cursor `state.vscdb`, Codex state/memory databases, Warp SQLite, and Hermes databases need distinct adapters and deduplication rules.

### 2.8 Monoliths and copied architecture

Avoid Hermes's gravity-well modules such as `run_agent.py`, `cli.py`, `gateway/run.py`, and `hermes_cli/kanban_db.py`. Reimplement small patterns behind agentlog-owned interfaces. Do not take a runtime dependency on Hermes.

Also avoid copying source-available or AGPL implementation code. CCC and several relevant orchestrators are useful design references, not safe code donors without a license review.

### 2.9 Cloud-required core value

`~/.agentlog/agentlog.db` must be sufficient for ingestion, search, analysis, and knowledge retrieval. Cloud sync, teams, mobile access, and remote notifications are optional later layers.

## 3. Core positioning decision

### Identity

agentlog is:

1. **An ingestion engine** over heterogeneous local agent state.
2. **A normalized evidence ledger** for sessions, messages, tools, skills, models, cost, and lineage.
3. **An observability cockpit** for search, attention, quality, and attribution.
4. **A learning relay** that turns verified findings from one session into relevant context for later sessions.

agentlog is not:

- the process supervisor;
- the task source of truth;
- the agent-to-agent scheduler;
- the terminal or chat UI;
- the worktree owner;
- a general personal-assistant operating system.

### Reconciliation with Hermes Kanban

Hermes Kanban contains two separable ideas:

1. **Durable state and audit mechanics**: SQLite, append-only events, explicit transitions, bounded context, typed reasons, human-readable comments.
2. **Execution orchestration**: claims, heartbeats, dispatch, retries, task DAGs, worker identities, workspaces, and provider overrides.

agentlog should adopt the first and reject the second.

The agentlog equivalents are:

| Hermes concept | agentlog equivalent |
|---|---|
| task card | observed session, attention item, or learning proposal |
| task event | immutable status/extraction/publication event |
| comments | user annotation or review rationale |
| blocked reason | typed attention reason |
| worker context | bounded handoff or learning context package |
| claim/heartbeat/dispatcher | **no equivalent** |
| model/provider override per task | extraction policy, not execution routing |

If execution orchestration is ever added, it must be an optional plugin or separate service with its own database and threat model. Core ingestion must remain useful and complete when that service is absent.

## 4. Concrete feature backlog

### P0 — Must have for v1

1. **Deep canonical ingestion for Codex, Claude Code, and Cursor**
   - Parse canonical transcript sources, archived sessions, and subagents.
   - Add Cursor `state.vscdb` ingestion and dedupe it against agent-transcript exports.
   - Enrich Codex sessions from `state_5.sqlite` spawn edges.
   - Preserve incremental checkpoints, parser-version invalidation, and per-artifact failures.

2. **Harness registry and capability matrix**
   - Replace the closed `Harness` enum and hard-coded `adapters()` list with registered harness definitions.
   - Store declared and observed capabilities.
   - Surface partial coverage honestly in the UI.

3. **Schema migrations and provenance**
   - Add explicit database schema versioning and migrations.
   - Record raw artifact identity, normalization version, and derivation lineage.
   - Preserve evidence links for every insight.

4. **Deterministic analysis baseline**
   - Session duration, model/tool/skill counts, failures, retries, lineage, and ingest freshness.
   - Deterministic attention states where evidence permits.
   - No LLM dependency for core value.

5. **Reproducible semantic extraction**
   - Corrections, frustration, failure patterns, handoff candidates, and learning candidates over `exchange_windows`.
   - Extraction-run caching by input hash plus extractor/prompt/schema/provider/model versions.
   - Deterministic-only, local-model, and bring-your-own-key modes.

6. **Search and transcript APIs**
   - FTS5 across messages.
   - Windowed transcript loading with cursor pagination.
   - Search results with real context, harness/project filters, and lineage-aware grouping.

7. **Core dashboard**
   - Overview with Attention Inbox and ingest freshness.
   - Sessions ledger and Cmd+K.
   - Session detail with transcript, tool calls, lineage, and evidence-linked corrections.
   - Models & Cost, Skills, and Insights views with explicit estimates and confidence.

8. **Privacy and secret handling**
   - Never ingest auth files.
   - Redact likely secrets before remote semantic extraction.
   - Show what content will leave the machine.
   - Keep all raw evidence and deterministic analysis local.

9. **Reliable incremental operation**
   - Manual refresh plus nightly scheduled ingest.
   - Idempotent extraction watermarks.
   - Visible partial failures and last-success timestamps.

### P1 — Important for usefulness

1. **Learnings propagation**
   - Extract atomic claims with subject, value, scope, timestamp, evidence, and supersession links.
   - Resolve affected projects using repository, dependency, path, and topic metadata.
   - Queue candidates for review; automatically distribute approved learnings.
   - Publish to an agentlog-managed Markdown context file and expose a read-only MCP query surface.
   - Support future sessions first; label active-session refresh as unsupported until a harness-specific integration exists.

2. **Structured handoffs**
   - Generate bounded session briefs with unresolved work, changed assumptions, and affected components.
   - Link parent and child sessions and show handoff quality.

3. **Live local updates**
   - Watch source artifacts, run debounced incremental ingest, and stream change events to React over SSE.
   - Add derived `needs_input`, `working`, `error`, `idle`, and `complete` states with source/confidence.

4. **Skill effectiveness**
   - Index skill definitions and versions.
   - Join exposures to outcomes.
   - Enforce sample-size thresholds and avoid causal language for observational comparisons.

5. **Hybrid retrieval**
   - Add embeddings behind the same search interface.
   - Use FTS/vector reciprocal-rank fusion and retain exact evidence windows.

6. **AI code attribution**
   - Join Cursor AI tracking, session commits, and Git history for codecast-style “which session changed this line?”
   - Present confidence and gaps; do not overclaim authorship.

7. **Warp and Hermes ingestion**
   - Read Warp's SQLite conversation tables.
   - Read Hermes `state.db` and optional `kanban.db` read-only.
   - Treat Hermes tasks as observed external state, not agentlog-owned tasks.

8. **Read-only agentlog MCP server**
   - Search sessions, fetch evidence, retrieve relevant approved learnings, and get handoff briefs.
   - No spawn, claim, edit, or shell tools.

### P2 — Nice to have later

1. **Active-session learning refresh**
   - Harness hooks or explicit commands that ask agentlog for updates since a session snapshot.
   - Never assume all harnesses can accept mid-session context.

2. **Opt-in publication adapters**
   - Generate scoped `updates.md`, SKILL.md, AGENTS.md/CLAUDE.md references, or harness-specific memory exports.
   - Use content hashes, dry runs, backups, and rollback.

3. **Learning graph**
   - Visualize claims, dependencies, projects, sessions, and supersession.
   - Keep it an explanatory UI, like Hermes's `agent/learning_graph.py`, not a runtime coordinator.

4. **Remote and team features**
   - Encrypted sync, mobile lens, and shared learning review only after local single-user workflows are strong.

5. **Additional harnesses and run receipts**
   - Ingest Gemini, OpenCode, autonomous runners, and CI agent receipts according to demonstrated demand.

### Explicitly not on the backlog

- PTY ownership;
- worktree spawning;
- agent task dispatch;
- permission proxying;
- merge queues;
- generic chat;
- autonomous retry loops;
- cloud-required storage.

## 5. Architecture recommendations

### 5.1 Target architecture

```text
Provider-owned local state
    │
    ▼
Harness registry + read-only adapters
    │
    ▼
Normalized evidence ledger (SQLite/WAL/FTS5)
    │
    ├── deterministic analyzers
    ├── semantic extraction jobs
    └── lineage / dependency enrichment
    │
    ▼
Derived insights + learning candidates + event log
    │
    ├── FastAPI query API + SSE
    ├── React dashboard
    ├── read-only MCP retrieval
    └── opt-in publication adapters
```

Keep each arrow one-way by default. Publication is the only path that writes agent-consumable state, and it must never mutate source transcripts.

### 5.2 Memory and state propagation

Use three lifetimes, adapted from Guild:

| Lifetime | agentlog object | Purpose |
|---|---|---|
| session | evidence window / observation | Exact local event with provenance |
| handoff | session brief | Bounded context for the next related session |
| durable | approved learning | Reusable fact, constraint, correction, or practice |

Recommended learning record:

```text
learning
  id
  kind
  subject
  predicate
  value
  scope_type / scope_id
  source_session_id / source_message_id
  observed_at
  confidence
  status
  supersedes_id
  valid_from / valid_until
  extractor_run_id
```

Recommended flow:

1. **Detect:** semantic extractor emits a candidate from a stable exchange window.
2. **Ground:** attach exact source text and deterministic repository/dependency metadata.
3. **Route:** calculate affected project/topic scopes. Prefer explicit dependency evidence over embedding similarity.
4. **Reconcile:** search active learnings for the same subject/predicate; mark contradiction or supersession.
5. **Review:** user approves, edits, rejects, or later configures a narrow auto-approval policy.
6. **Publish:** materialize approved, relevant, non-superseded learnings into a bounded context view.
7. **Consume:** future agents read the generated file or query MCP. Record which learning version was exposed.
8. **Evaluate:** connect exposure to later corrections and outcomes without claiming causation.

This satisfies Oxy's core request—one agent's discovery can keep related agents current—without turning an unverified chat statement into global truth.

Use a **frozen snapshot per session**. At session start or first retrieval, record the IDs/versions of exposed learnings. New writes become immediately available to new retrievals, but the system should not pretend an already-running prompt was retroactively changed.

### 5.3 Multi-provider abstraction

#### Harness side

Define a `HarnessDefinition` separate from parser code:

```text
id
display_name
source_patterns
capabilities
canonical_source_priority
parser_factory
resume_template
```

Capabilities should include:

```text
transcript, lineage, tool_events, skills, token_usage, cost,
live_status, resume, memory, code_attribution
```

Adapters continue to implement discovery and incremental parsing. A harness may have multiple source adapters—such as Cursor agent transcripts plus `state.vscdb`—with explicit canonical priority and dedupe keys.

#### Semantic-analysis side

Define a narrow `AnalysisProvider`:

```text
structured_extract(request, output_schema)
embed(texts)
capabilities()
privacy_class
```

Resolve it from declarative provider/model configuration. The extraction scheduler, not business logic, chooses deterministic, local, or remote execution. Store the resolved provider and model on every run.

Do not put harness-specific parsing into model-provider adapters or provider authentication into transcript adapters.

### 5.4 Learnings propagation surfaces

Use two consumption surfaces:

1. **MCP retrieval as the canonical interface**
   - Query relevant approved learnings for project, dependencies, topic, and “since version.”
   - Return bounded text plus source citations.
   - Works across harnesses that support MCP without rewriting their native memory files.

2. **Generated Markdown as the compatibility interface**
   - Materialize a bounded `~/.agentlog/context/<scope>.md` or opt-in project `.agentlog/updates.md`.
   - A stable, manually installed rule tells the harness to read it.
   - agentlog owns the generated file and rewrites it atomically from the database; agents do not append to it directly.

The database remains canonical. Markdown is a projection, not the source of truth. This avoids concurrent-writer corruption and makes supersession, size limits, and rollback tractable.

### 5.5 Database and job boundaries

Add focused modules rather than expanding `repository.py` into a monolith:

- `db/migrations/` — schema evolution;
- `ingest/registry.py` — harness definitions and capabilities;
- `analysis/extractors/` — versioned deterministic and semantic extractors;
- `analysis/jobs.py` — resumable extraction queue;
- `knowledge/repository.py` — learning lifecycle and supersession;
- `knowledge/routing.py` — project/dependency/topic relevance;
- `knowledge/publishers/` — MCP views and generated Markdown;
- `api/` — FastAPI queries and SSE events.

For concurrency, SQLite WAL is sufficient. Use short transactions, idempotency keys, and compare-and-set transitions for extraction/publication jobs. Do not introduce Redis or a distributed queue for a single-machine v1.

### 5.6 Security and trust boundary

- Open third-party SQLite databases read-only where possible.
- Copy or snapshot databases before reading if their owner does not tolerate concurrent access.
- Never load or execute provider plugins during ingestion.
- Never index credential files.
- Redact secrets before remote model calls and publication.
- Store publication history and content hashes.
- Require explicit opt-in before writing any project file.
- Treat transcript instructions as untrusted data, not commands to the extractor or publisher.

## Build guidance

Build in this order:

1. complete deep canonical ingestion and migrations;
2. ship factual Sessions/Search/Lineage views;
3. add deterministic metrics and the Attention Inbox;
4. add reproducible semantic extraction and evidence-linked Insights;
5. add learning review and future-session propagation;
6. add live refresh, MCP retrieval, attribution, and additional harnesses.

The critical sequencing rule is that propagation depends on trustworthy ingestion, provenance, and supersession. A global updates file built before those foundations would amplify mistakes rather than compound useful knowledge.

## Final recommendation

Do not build another orchestrator. Build the best local record of what coding agents did and the safest mechanism for carrying validated knowledge forward.

Hermes demonstrates how durable state can coordinate execution. agentlog should use the same rigor to coordinate **understanding**: immutable evidence, explicit state transitions, bounded context, provenance, and durable handoffs—while leaving execution to the tools that already own it.

## Appendix: Detailed UI/UX council notes

Council seat: dashboard design. Scope: what the research changes (and deliberately does not change) about `dashboard-design.md`.

### 0. Headline

The research **validates the existing design's core bet** — a read-only, dense, dark analytics cockpit over on-disk state — and independently confirms the stack (Better Agent chose FastAPI + React + WebSocket; CCC is a Python local server + web dashboard). The biggest gap the research exposes is that our design is purely *retrospective*: it has no concept of "what needs my attention right now." Every high-relevance tool (CCC, codecast, agent-console, diri) leads with live attention states. That is the one structural addition worth making. Everything else is refinement.

### 1. UI patterns worth stealing

#### 1.1 Attention/inbox taxonomy — steal from codecast, agent-console, CCC

The single most convergent pattern across the list. codecast's inbox ordering is the best articulation:

```
Pinned → Working → Needs Input → Idle → Deferred
```

agent-console uses `working / waiting / idle / failed`; CCC derives "needs you" **from the transcript itself, not process heuristics** — the right method for a lens that doesn't own PTYs. diri derives status from PTY output; we can't (and shouldn't) do that, so CCC's transcript-derived approach is our model.

**Adaptation for agentlog:** an "Attention" strip at the top of Overview, above the KPI cards — one row of state counts (`Needs input 2 · Working 3 · Idle 41 · Error 1`), each count clickable → Sessions pre-filtered. States render as our existing 6px status dots + text, never badge walls. This reuses the universal-drill-down mental model (§8.2 of the design doc) rather than adding a new one. Sessions with no recent activity fall out of the strip entirely — the strip shows *now*, the KPI cards show *the period*.

This also implies a `state` field distinct from the existing terminal `status` column (`ok/retried/corrected/abandoned`): status is what a session *was*, state is what it *is*. Keep both; don't conflate them.

#### 1.2 Hermes's kanban — study the taxonomy, do not adopt the board

Hermes's kanban is a **durable work queue for an agent runtime** (SQLite + CAS claims + comments-as-protocol). agentlog is an observer, not a dispatcher. The orchestrator audit is blunt: kanban-as-the-product is a crowded, sunsetting category (vibe-kanban, 27k stars, is winding down), and adopting a board would silently pivot agentlog into spawn-and-supervise. **No kanban board in v1 or v2.**

What *is* worth taking from Hermes's kanban, translated to read-only:

- **The status vocabulary** (`triage / todo / ready / running / blocked / review / done`) is a well-tested state machine. If agentlog ever ingests Hermes's `kanban.db` as another harness (integration option A in the Hermes audit), render those statuses faithfully as a *facet on the Sessions page*, not as columns.
- **Typed block reasons** (`dependency | needs_input | capability | transient`) — if we surface a "blocked" state anywhere, type it. An untyped "blocked" is noise; a typed one is an insight.
- **The drawer pattern** (`drawer.tsx`): Hermes opens task detail as a slide-over drawer rather than navigating away, preserving board context. Useful for us as a *session peek* — hover/press on a Sessions row opens a summary drawer (first prompt, anatomy stats, state) without leaving the filtered list. Full navigation to Session detail stays for deep reading.
- **WS tail of `task_events`** — the pattern of streaming an append-only event table to the UI is exactly how our live refresh should work (see §2.1).

#### 1.3 Session graphs — steal from GraphCode, codecast, Hermes's learning graph

Three independent signals: GraphCode renders sessions as a graph of live nodes; codecast models parent/child sub-session hierarchy; Codex's own `state_5.sqlite` already contains spawn edges (per our inventory); Hermes tracks `parent_session_id` lineage in its session store.

**Adaptation:** do *not* build a canvas/graph view (that's GraphCode's product). Instead:

- In **Session detail**, add a small "Lineage" block to the anatomy pane: parent session link + child/subagent session links, rendered as an indented list. A list is a graph you can actually read at our density.
- In the **Sessions table**, indent or badge child sessions under a disclosure on the parent row (codecast's hierarchy), with a `Flat / Grouped` toggle. Default flat — grouping is a lens, not the resting state.

#### 1.4 Cmd+K — already designed; extend it with codecast's verbs

Our design has `⌘K` for jump-to-session/project/skill. codecast validates it and adds action verbs (`ask`, `context`, `handoff`). We stay read-only, so no write verbs — but add **filter actions** to the palette: "Filter: sessions needing input", "Filter: corrections in ai-sec", "Go: Models & Cost". The palette becomes the keyboard route to any URL-addressable view, which our filters-are-URLs rule (§8.3) makes nearly free.

#### 1.5 Stable session identity — steal from agent-console

"Title = first user prompt, for life." Never re-title sessions from later content or LLM summaries; the user's recall key is what *they* typed first. Adopt as a hard rule in ingest and everywhere a session is named (table rows, palette results, drawer header). This also honors the Hermes lesson: no LLM summarization in the search/recall path.

### 2. Dashboard enhancements (what to ADD)

Ordered by value; the first two are structural, the rest are refinements.

1. **Attention strip on Overview** (§1.1). New API: `GET /api/attention` returning per-state session lists. Placement: above KPI strip; the strip is *now*, KPIs are *the period*.
2. **Live refresh via SSE** — CCC exposes `/api/sessions/events`; Better Agent uses WebSockets. Replace "press ⟳ and wait" with a file-watcher-driven ingest that pushes invalidations over one SSE endpoint; TanStack Query refetches affected keys. Keep the manual ⟳ as fallback. This is what makes an attention strip honest — a stale "needs input" is worse than none.
3. **Windowed transcript loading** in Session detail — CCC windows long conversations for a reason; long Codex JSONL sessions will hurt otherwise. Virtualize the transcript pane (we already virtualize the Sessions table), load message windows on demand, keep the anatomy pane computed server-side so it never depends on how much transcript is loaded.
4. **Harness capability matrix** — CCC/diri publish an honest per-engine support matrix (spawn/monitor/transcript/steer). Ours is read-only, so the columns are: `transcripts ✓ / live state ? / tokens ✓ / cost est ✓ / skills ✓` per harness. Surface as a small static page or an info popover next to harness filters. This is the UI form of "Cursor is metadata-only sync" honesty — matching our existing "honest data" principle (§8.7).
5. **Session lineage in detail + grouped Sessions toggle** (§1.3).
6. **Session peek drawer** on the Sessions table (§1.2).
7. **Daily digest card** on Overview — codecast's activity feed / daily digest, compressed to one card: "Yesterday: 14 sessions, 3 corrections, 1 new dead skill." Links into Sessions/Skills filtered to yesterday. Low effort; it reuses existing aggregates.
8. **Blame — as a roadmap item, not v1.** codecast's `cast blame` (line → session attribution) is the standout differentiator, and we hold the raw material (Cursor AI-tracking DB + git). But it is a new ingest pipeline and a new view; adding it now would delay the cockpit. Put it on the Insights roadmap as "attribution insights" and design the session detail's "Files touched" list to be link-ready for it.

### 3. What to explicitly NOT add (scope creep)

Straight from the research's own warnings:

- **Kanban board / task management** — vibe-kanban's sunset is the market's verdict; Hermes's board exists to *dispatch*, which we don't do.
- **Monitor grid of terminals / live PTY panes** (octomux, tlbx, diri) — we don't own PTYs; embedding terminals would force us to.
- **Unified permission/approval inbox** (octomux) — requires live attach; revisit only if agentlog ever gains a live mode.
- **Diff review workstation** (octomux, Garcon, parallel-code) — a different product with heavy UI surface.
- **Chat reimplementation** — agent-console's "resume the native UI" is the right instinct; our transcript viewer is for *reading*, and at most we add a copyable "resume in harness" command string, never an embedded chat.
- **Spawn/fork/steer controls** — the CCC manifesto's core warning: wrappers that own execution go blind. No launch buttons anywhere in the UI.
- **Phone/remote lens and cloud sync** — loopback-first stays; both are later-if-ever.
- **Graph canvas view** — lineage as lists/indentation (§1.3), not a pannable node editor.

### 4. Specific components to emulate

| Component | Source tool | What to take | Maps to (design doc) |
|---|---|---|---|
| Inbox strip with priority-ordered states | codecast | State ordering + count-chip → filtered list | New; sits above Overview KPI strip |
| Transcript viewer with tool-call collapse | codecast, CCC | Collapsed tool groups (already designed) + **windowed loading** | §4.4 Session detail |
| Detail drawer (slide-over, context preserved) | Hermes desktop kanban `drawer.tsx` | Session peek from table row | New; Sessions page |
| Cost dashboard as first-class page | agent-deck, CCC | Validates Models & Cost; add cost-per-quality framing (we already have CORR% next to $/SESS — keep it) | §4.5 |
| Skills manager as dedicated surface | agent-deck | Validates Skills page; their skills *pool* view suggests adding a `SOURCE` facet filter (we have the column; add the filter) | §4.6 |
| Jump-to-alert keybinding | agent-console (`a`) | Add `a` = jump to first needs-input session, alongside existing `j/k/n` | §8.5 keyboard map |
| Engine capability matrix | CCC, diri | Honest per-harness support table | New; info popover/static page |
| Event-stream-driven UI refresh | CCC (SSE), Hermes (WS `task_events` tail), Better Agent | One SSE endpoint invalidating query keys | §9 API sketch |
| Secret auto-redaction in transcripts | codecast | Redact known token patterns at *render* time (and ideally ingest time) in the transcript pane | §4.4 |

**Dark mode:** the research adds little here — most high-relevance tools are TUIs (density benchmarks, not palette sources), and none of the audited READMEs document a token system worth copying. Our palette (§5) is already the pattern the good desktop tools converge on: near-black neutrals, one text family, status color confined to small indicators. Keep it unchanged; the only note worth recording is that the attention strip must obey the existing rule — state encoded in ≤8px dots and text, never filled backgrounds, or the strip will visually outrank the KPIs it sits above.

**Information density:** the TUIs (agent-console, agent-deck) are the benchmark — every row earns its height. Two concrete takes: (a) the session peek drawer keeps the table dense by moving detail off-row; (b) windowed transcript loading keeps Session detail responsive at any length. Our existing 8px row padding and tabular numerals stand.

### 5. Visual hierarchy — "lens over state"

The best tools in the audit express *lens, not runtime* through concrete UI choices, all of which we should mirror:

1. **Provenance over control.** CCC and agent-console never show a control they can't honor; every element is traceable to on-disk state. For us: no buttons that imply execution, and every "live" claim carries freshness — the sidebar already shows "synced 2m ago"; the attention strip must inherit a per-harness `as of` tooltip, because Cursor state will always be staler than Claude JSONL.
2. **Derived, labeled, typed.** Status derived from transcripts (CCC), never from an LLM call in the loop (bernstein's zero-token discipline); blocks typed (Hermes); estimates labeled (our §8.7). The visual grammar: *facts in foreground text, derivations in muted text with tooltips, and confidence tags on anything computed* — our Insights cards already do this; the attention strip and lineage links should follow the same grammar.
3. **Status taxonomy visualization.** Convergent answer across codecast/agent-console/diri: a **priority-ordered horizontal strip of state counts**, not a board. Columns (kanban) imply you move things between them; a strip implies you *read* it. Ordering encodes priority (needs-input leftmost), count-chips encode volume, and click-through encodes drill-down. One glance answers "does anything need me," which is the entire job of the top of Overview.
4. **The terminal state of every glance is a transcript.** Our design principle §1.4 (everything drills down to a session) is exactly what CCC/codecast do and what the spawn-and-supervise tools *can't* do cleanly, because their terminal state is a live process. This is our structural advantage — protect it. Every new element added above (strip, digest, lineage, matrix) resolves to a Sessions URL or a Session detail; nothing introduced here breaks the single mental model.

### 6. Changes this implies for dashboard-design.md

Concrete edit list for the next design-doc revision:

1. §4.2 Overview: add Attention strip above KPI row; add Daily digest card (replaces one of the two chart-row slots or sits in the right column).
2. §4.3 Sessions: add peek drawer; add `Flat / Grouped` lineage toggle; add `Source` facet to skills filter.
3. §4.4 Session detail: windowed transcript loading; Lineage block in anatomy pane; render-time secret redaction.
4. §8.5 Keyboard: add `a` (jump to needs-input).
5. §9 API: add `GET /api/attention`, `GET /api/events` (SSE), and lineage fields on `GET /api/sessions/{id}`.
6. New appendix: harness capability matrix (transcripts / live state / tokens / cost / skills per harness).
7. Roadmap note: blame/attribution view (codecast-style) — post-v1, Insights-adjacent.

Nothing in the research argues against the existing five-view IA, the palette, the typography, or the React/FastAPI stack — all were independently converged on by the closest tools in the space.

---
