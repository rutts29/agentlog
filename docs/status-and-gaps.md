# agentlog — Status and Gaps

> Historical audit snapshot from before the 2026-08-09 API fallback recovery.
> Several gaps below were subsequently addressed; verify current source and tests
> before treating any row as present state.

**Date:** 2026-08-09
**Method:** read-only reconciliation of the planning docs (`docs/research/council-synthesis.md`, `docs/research/eval-architecture.md`, the Hermes and orchestrator audits, `docs/dashboard-design.md`, `docs/dashboard-redesign-v2.md`, `docs/data-inventory.md`, `docs/cleanup-queue.md`) against the actual code in `src/agentlog/` and `web/`. Every status claim below cites a file (and line where useful). Design-doc prose is never counted as implementation evidence.

## 1. Framing

agentlog is a **harness assistant and observatory layer**, not a harness. It sits beside whatever coding agent the owner is running — Claude Code, Codex, Cursor, Warp, Hermes — reads their durable on-disk transcripts, databases, and configs locally, normalizes them into one evidence ledger, measures behavior over time, and *proposes* changes for the owner to approve. It never drives an agent, never spawns or supervises a process, and never applies a change on its own: writes are limited to `~/.agentlog/`, and any write outside it must pass through an explicitly approved, dry-run-by-default proposal (`src/agentlog/analysis/claims/apply.py`, `src/agentlog/cli.py:349-459`). Everything works offline against `~/.agentlog/agentlog.db`; there is no cloud dependency in the codebase.

## 1.1 Forward-only source-backed transcript architecture

Transcript retention is an explicit, forward-only choice. Existing sessions
remain `legacy_materialized`: their message text stays in SQLite and remains
available to SQLite FTS. Every newly created session identity is
`source_backed`, including a new identity discovered inside an artifact that
also contains older materialized sessions. The storage mode is immutable on
artifacts and sessions, preventing later ingest from silently changing the
retention contract.

For source-backed sessions, SQLite retains session and artifact metadata,
message identity fields and content hashes, tool events, token usage, and
derived exchange windows. Message `text` is blank and source-backed messages
are not inserted into FTS. Detail endpoints, search, read-only MCP tools, and
coach preprocessing hydrate text transiently from the canonical artifact
through the deterministic harness adapter. No LLM is needed to parse or
retrieve transcript text.

The source reader validates the artifact path and harness, checks that the
persisted checkpoint prefix still matches, reads a stable source snapshot, and
requires the parsed session to match the persisted identity and message
metadata prefix. A missing source, changed prefix, unstable read, identity
disappearance, or metadata divergence fails closed with no transcript text;
callers receive an unavailable/changed result rather than stale or guessed
content. Complete lines appended after the checkpoint become visible on the
next read, subject to the same stability and prefix checks.

This is deliberately a bounded first cut. Source search scans only a bounded
number of candidate sessions, and each selected JSONL source read currently
parses the full artifact to reconstruct the requested session; SQLite sources
likewise use their adapter's current full read. Large multi-session artifacts
therefore pay a scan/parse cost on detail, search, MCP, and coach paths. The
durable metadata and hashes make that cost safe and observable, but do not yet
provide random-access transcript offsets.

Cutover is additive: migration defaults existing rows to `legacy_materialized`,
installs blank-text-aware FTS triggers, and adds storage guards. Rollback means
restoring the database backup and compatible code version; there is no in-place
conversion that repopulates source-backed text or silently downgrades a
session. Future improvements can add indexed source offsets or a deliberate
backfill, but must preserve the immutable mode and fail-closed behavior.

## 2. Council backlog matrix

Legend: **Integrated** = built and reachable from the UI or CLI · **Built, not surfaced** = code and API exist but no UI/CLI consumer · **Scaffold** = partial or unvalidated · **Missing**.

### P0 — must have for v1

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1a | Codex / Claude / Cursor transcript ingestion, incremental checkpoints, per-artifact failure isolation | Integrated | `ingest/codex.py`, `ingest/claude.py`, `ingest/cursor.py`; registered `ingest/pipeline.py:41-48`; checkpointing `ingest/checkpoint.py` + `pipeline.py:130-201`; failure isolation `pipeline.py:85-91` |
| 1b | Codex **archived** sessions | Missing | `CodexAdapter.discover()` globs `~/.codex/sessions` only (`ingest/codex.py:161-165`); `~/.codex/archived_sessions/` is declared in `registry/harnesses.py:62` but never read (14 MB / 3 files per `data-inventory.md`) |
| 1c | Cursor `state.vscdb` ingestion + dedupe vs agent-transcripts | Scaffold | vscdb is read for **metadata only** (model/effort/timestamps by composer UUID): `ingest/cursor.py:260-338`, `lookup_composer_meta`. Discovery is transcript-files-only (`ingest/cursor.py:415-424`), so the 84 composers / 23.7k bubbles in vscdb are not a session source. Composer-UUID dedupe of transcript duplicates does exist (`ingest/cursor_merge.py`) |
| 1d | Codex `state_5.sqlite` spawn-edge enrichment | Missing | No reference to `state_5.sqlite` or `thread_spawn_edges` anywhere in `src/agentlog/`; lineage comes from JSONL `parent_thread_id` only |
| 2 | Harness registry replacing the closed enum + capability matrix | Scaffold | Declared capability matrix exists (`registry/harnesses.py`, 5 harnesses × 9 capabilities) and is served live-joined at `GET /api/harnesses` (`api/harnesses.py`, `api/app.py:102-106`). But `Harness` is **still a closed enum** (`normalize/models.py:10-15`) and `adapters()` is **still a hard-coded list** (`ingest/pipeline.py:41-48`) — the two things the council specifically said to replace. No UI consumer (see §4) |
| 3 | Schema migrations and provenance | Integrated, with two known holes | Runner + 15 numbered migrations on top of the base schema (`db/migrations/__init__.py:24-41`); `derivation_runs` provenance table in `v002_extraction.py:6`. **Hole A:** `exchange_windows` still has no `window_hash`/`builder_version`/`start_seq`/`end_seq` (`db/schema.py:81-88`); `v012_durable_labels.py` upgraded identity to a request+response `content_hash` (`:14-23`) but tool context and builder version are still excluded, contrary to `eval-architecture.md` §8.1. **Hole B:** `tool_events` still lacks `tool_call_id`, `arguments_hash`, `canonical_tool`, `result_code`, `success_source`, `timestamp` (`db/schema.py:61-71`), so exact retry fingerprinting remains impossible |
| 4 | Deterministic analysis baseline (durations, counts, lineage, freshness, deterministic attention) | Integrated | `analysis/deterministic.py`, `analysis/derive.py`, `analysis/attention.py` (804 lines, 7 states) surfaced at `GET /api/attention` and consumed by `web/src/views/Overview.tsx`; freshness at `GET /api/meta` |
| 5 | Reproducible semantic extraction with caching and provenance | Scaffold — data exists, gate does not | Full extractor stack present: triage (`analysis/extractors/triage.py`), packet emit/ingest with hard-reject validation (`extractors/packets.py`, 778 lines), storage with extractor/prompt/model provenance (`extractors/storage.py`), CLI `agentlog extract packets-*` (`cli.py:602-729`). **1,837 `ux_observations` rows exist** (`docs/verification/db-integrity-report.md:229`) from run `.research/extraction-run-001` (232/232 packets complete). **But** the audit gate that authorizes those labels was run in `synthetic_labeled_audit` mode against fixture gold and a scripted model, not the live corpus (`.research/extraction-verification/audit_fixture_metrics.json`), and `.research/extraction-run-002` (138 packets / 1,089 windows) is 100% `pending`. Meanwhile `api/queries.py:734-797` computes and publishes the lead metric whenever `ux_observations` is non-empty — it applies the §4.7 statistical precision gate but **not** the §3.4 label-quality gate (precision ≥0.90 / recall ≥0.80 on adjudicated gold) |
| 6 | Search and transcript APIs (FTS5, windowed loading, lineage grouping) | Integrated (search) / Missing (windowing) | FTS5 table `db/schema.py:101`, `GET /api/search` with harness/model/project filters and cursor (`api/app.py:302-323`); lineage via `GET /api/sessions/{id}/tree`. Cursor-paginated **transcript windowing** is not implemented — `session_detail_v2` returns the session detail in one shot (`api/descriptive.py`), and `web/src/components/Transcript.tsx` renders from that |
| 7 | Core dashboard (Overview + Attention, Sessions, Cmd+K, Session detail, Models & Cost, Skills, Insights) | Integrated except Insights | 10 routes in `web/src/App.tsx:29-40`; `CommandPalette.tsx`; `Models.tsx`, `Skills.tsx`, `Sessions.tsx`, `SessionDetail.tsx` all fetch real endpoints. **`Insights.tsx` is a 69-line placeholder** that renders one static policy card and an empty state |
| 8 | Privacy and secret handling (never ingest auth files, redact before remote extraction, show what leaves the machine) | Scaffold | Auth files are never in any discovery path (`config.py`, adapters). Read-only SQLite opening is enforced (`ingest/sqlite_ro.py`). **No redaction module exists anywhere in `src/agentlog/`** (grep for `redact` returns nothing), yet a remote path exists: `analysis/extractors/llm_client.py` posts window text to `https://api.x.ai/v1` when `XAI_API_KEY` is set. The packet workflow (the path actually used) keeps everything local, which is why this has not bitten yet |
| 9 | Reliable incremental operation (manual + scheduled ingest, watermarks, visible partial failures) | Integrated, but currently lagging in practice | Watcher daemon with debounce, incremental `ingest_harness`, and post-ingest derive (`watch/daemon.py:142-185`); launchd install/status (`service/launchd.py`, `cli.py:1039-1135`); derive watermarks (`v015_derive_watermarks.py`). **Operationally**, `db-integrity-report.md:12` found 15 Cursor transcripts never ingested and 9 stale — the watcher is not keeping Cursor current |

### P1 — important for usefulness

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Learnings propagation: atomic claims, scope routing, review queue, approved publication | Built, not surfaced in UI | Full claim lifecycle: `analysis/claims/{extract,scope,store,proposals,apply}.py`, tables in `v016_claims_proposals.py`. Review + apply + rollback via CLI (`cli.py:283-459`) and a just-added API router (`api/proposals.py`, 8 routes). Publication targets `~/.agentlog/context/` rather than `AGENTS.md` for usage-mix notes (`claims/proposals.py:332-335`, `:404`). **No web view consumes `/api/claims` or `/api/proposals`** (`web/src/lib/api.ts` references neither) |
| 2 | Structured handoffs / session briefs | Built, not surfaced in UI | Deterministic brief builder (`analysis/briefs.py`, 774 lines), CLI `agentlog brief` (`cli.py:255-280`), API `GET /api/sessions/{id}/brief` and `/brief.md` (`api/briefs.py`). No web consumer |
| 3 | Live local updates (watch → debounced ingest → SSE → derived states) | Integrated | `watch/daemon.py`, `watch/presence.py` (513 lines), `GET /api/events/stream` (`api/events.py:154`), `GET /api/live`; consumed by `web/src/lib/useIngestStream.ts` and `useLivePresence.ts`; live orbs in `web/src/components/LiveOrb.tsx` |
| 4 | Skill effectiveness with sample-size thresholds and non-causal language | Integrated | `analysis/skills.py` (883 lines): skill indexing from disk, exposure matching, outcome joins, rate payloads with gating (`_rate_payload`, `:320`); `GET /api/skills` → `web/src/views/Skills.tsx` |
| 5 | Hybrid retrieval (embeddings + RRF behind the search interface) | Missing | No embedding code anywhere in `src/agentlog/` (grep for `embed` returns nothing). This was explicitly "adopt later," so it is a deliberate deferral rather than a slip |
| 6 | AI code attribution ("which session changed this line?") | Scaffold | Git-side attribution is real: read-only `git` via argv lists, commit↔session joining, `session_commits` (480 rows) and `authored_by_agent` (`analysis/attribution.py`, 691 lines; `v009`, `v010`); `GET /api/attribution*`. **Cursor's `ai-code-tracking.db` (13.7k code hashes, 25 scored commits) is not read**, and no web view consumes the attribution API |
| 7 | Warp and Hermes ingestion, read-only, tasks as observed external state | Integrated | `ingest/warp.py` (246 lines), `ingest/hermes.py` (444 lines, reads `state.db` + kanban boards), both registered in `pipeline.py:41-48`, declared in `registry/harnesses.py:113-178`, tested in `tests/test_warp_hermes_ingest.py`. Note the Hermes adapter is validated only against synthetic fixtures — no `~/.hermes` install exists on this machine (`registry/harnesses.py:167-171`) |
| 8 | Read-only agentlog MCP server | Integrated, narrower than specced | `mcp_server/server.py` exposes 6 read-only tools (`search_sessions`, `get_session`, `usage_stats`, `attention_inbox`, `skill_inventory`, `agreement_and_extraction_status` — `mcp_server/tools.py`). No spawn/edit/shell tools. **Missing the two retrieval tools the council named**: "retrieve relevant approved learnings" and "get handoff briefs," both of which already exist as library code |

### P2 — later

| # | Requirement | Status | Evidence |
|---|---|---|---|
| 1 | Active-session learning refresh | Missing | No harness hook or refresh command; consistent with the council's own "later capability" framing |
| 2 | Opt-in publication adapters with hashes, dry runs, backups, rollback | Integrated (CLI) | `analysis/claims/apply.py` implements dry-run default, content-hash checks, backup, and rollback; `cli.py:363-421` requires `--approve` then `--apply --write` |
| 3 | Learning graph visualization | Missing (session graph exists instead) | `web/src/components/ConstellationGraph.tsx` (1,618 lines) + `GET /api/graph` visualize **sessions and repos**, not claims/supersession |
| 4 | Remote / team features | Missing, intentionally | Nothing in the codebase; loopback CORS only (`api/app.py:86-91`) |
| 5 | Additional harnesses and CI run receipts | Missing, intentionally | — |

### Evaluation architecture (Type A / Type B)

| Requirement | Status | Evidence |
|---|---|---|
| Type B statistical engine: Wilson intervals, cluster bootstrap, precision gates, abstention, confounder flags, language contract | Integrated as a library | `analysis/performance/{stats,gates,outcomes,eligibility,analysis}.py`; used by `api/queries.py` (`evaluate_binary_rate`, `evaluate_continuous_rate`) and surfaced through `/api/summary` and `/api/aggregates/binary`; language contract echoed at `GET /api/meta` (`api/app.py:132-146`) |
| Type B `performance profile` / `performance compare` commands | Missing | `cli.py` has `ingest`, `derive`, `stats`, `sessions`, `session show`, `brief`, `claims`, `propose`, `search`, `extract *`, `experiment *`, `serve`, `service *` — no `performance` group |
| Type B task taxonomy + first-message pre-treatment classification + owner overrides | Missing | `task_clusters` and `outcome_observations` tables exist (`v003_experiments.py:6,15`) but there is no `taxonomy.py`/`cohorts.py`/`comparisons.py` under `analysis/performance/`, and no classifier writing `task_label_observations` (that table is not created by any migration) |
| §6 prospective coin-flip experiment (pre-registration, eligibility, ITT analysis, compliance from transcripts) | Integrated | `analysis/performance/experiments.py` (681 lines), `compliance.py`, `eligibility.py`; full CLI `agentlog experiment register/flip/link/sync-compliance/status/analyze` (`cli.py:812-997`); `tests/test_experiments.py` |
| Type A `src/agentlog/quality/` package and `agentlog quality run` | Missing | No `quality/` directory; no `tests/quality/` (tests are 36 flat files under `tests/`); no fixture manifests, gold files, incremental-equivalence test, adversarial/prompt-injection suite, or CI exit-code runner |
| Adjudication surface for building gold labels | Integrated | `api/adjudication.py` (41 KB, queue/report/taxonomy/save), `web/src/views/Adjudicate.tsx` (908 lines), `v008_adjudications.py`, `v012` rebuild. This is the machinery that would produce the gold the extraction gate is missing |

## 3. Hermes and orchestrator audits — adopted vs not

### Adopted

| Feature | Source | Rationale (one line) |
|---|---|---|
| On-disk state as source of truth | CCC / agent-console / codecast | The whole ingest layer reads provider files; sessions launched from a terminal still appear. |
| Per-harness capability matrix with honest unknowns | CCC, diri | `registry/harnesses.py` declares levels; `api/harnesses.py` joins live coverage — though nothing renders it yet. |
| Append-only durable state and typed reasons (Hermes mechanics, not its board) | Hermes `task_events`, `block_kind` | `ingest_events` (`v006`), derive watermarks (`v015`), and typed attention states in `analysis/attention.py`. |
| Attention inbox ordered by what needs the human | codecast / CCC / agent-console | 7 typed states with source and confidence in `analysis/attention.py`, surfaced on Overview. |
| SSE event stream for live refresh | CCC `/api/sessions/events` | `api/events.py` + `useIngestStream.ts`; no WebSocket, as recommended. |
| FTS-first search with real message windows, no LLM in the retrieval path | Hermes `session_search_tool` | `messages_fts` + `GET /api/search`; no summarization anywhere in search. |
| Session lineage as first-class | codecast, GraphCode | `sessions.parent_session_id` across all adapters; `/api/sessions/{id}/tree`; Orchestration view. |
| Cmd+K palette | codecast | `web/src/components/CommandPalette.tsx`. |
| Stable session title from the first user prompt | agent-console | `_first_user_preview` (`mcp_server/tools.py:75`) and the same rule in the sessions list; no LLM re-titling. |
| Read-only MCP as an integration surface | guild, swarm-protocol | `mcp_server/` with six read-only tools and `readOnlyHint` annotations. |
| Session graph (as a lens, not a canvas editor) | GraphCode | Built as `ConstellationGraph.tsx` — note this *exceeds* the council's advice, which said lineage should be lists, not a graph. The v2 redesign consciously overrode it. |
| Skill index with short descriptions | Hermes progressive skills, skillfold | `analysis/skills.py` indexes `SKILL.md` frontmatter from all harness roots. |
| Bounded handoff brief | guild, Hermes `build_worker_context` | `analysis/briefs.py` produces a size-bounded, evidence-linked Markdown brief. |

### Consciously rejected

| Feature | Source | Rationale |
|---|---|---|
| Kanban board as product surface | Hermes, vibe-kanban | Would pivot agentlog into dispatch; the category is sunsetting. No board code exists. |
| CAS claims, heartbeats, dispatcher, worker spawning, worktrees, per-task model overrides | Hermes gateway | Execution orchestration; agentlog observes rather than schedules. Nothing in the codebase does this. |
| PTY ownership / terminal monitor grid | octomux, diri, tlbx | Owning execution makes hand-launched sessions invisible. |
| Unified permission/approval inbox | octomux | Requires live attach. |
| Diff review workstation, embedded chat, spawn/steer controls | octomux, Garcon | Different product; the transcript viewer is for reading only. |
| Cloud sync / phone lens | several | Local-first is a hard constraint; CORS is loopback-only. |
| Strands Evals as a dependency | eval-architecture §7 | Patterns copied (versioned suites, structured case results, CI exit codes); the runtime would drag in OpenTelemetry and boto3. Nothing imports it. |
| Automatic truth promotion into `AGENTS.md` | Oxy request | Proposals default to dry-run, require approval, and route usage-mix learnings to `~/.agentlog/context/` instead of prompt files (`claims/proposals.py:332-335`). |

### Never picked up (not a deliberate rejection)

| Feature | Source | Note |
|---|---|---|
| Hermes drawer pattern → session peek drawer | Hermes `drawer.tsx` | Listed as adopt in council §1.7 and dashboard §2.6; no drawer component exists. |
| Windowed transcript loading | CCC | Listed as adopt; `Transcript.tsx` renders the whole payload. |
| Daily digest card | codecast | Listed as adopt; not built. |
| Render-time secret redaction in transcripts | codecast | Listed as adopt; no redaction code at all. |
| Hybrid FTS + vector retrieval with RRF | guild | Explicitly deferred, still deferred. |
| Line-level blame joined with Cursor AI-tracking DB | codecast | Git half built; Cursor AI-tracking half missing. |
| `a` = jump to first needs-input session | agent-console | Not in any keyboard handler. |
| Zero-token status discipline | bernstein | Followed in spirit (status is deterministic), never written down as a rule. |

## 4. Specced but unbuilt UI surfaces

| Surface | Spec | Current state |
|---|---|---|
| **Insights feed** | `dashboard-design.md` §4.7; council P0 #7 | `web/src/views/Insights.tsx` is a 69-line placeholder. `GET /api/insights` exists and returns a feed shape; the view renders only the empty state and a static policy card. |
| **Harness capability matrix** | council §1.7, dashboard-design appendix item 6 | `GET /api/harnesses` is fully implemented and **has no web consumer** (absent from `web/src/lib/api.ts`). |
| **Session peek drawer** | council §1.7 / §4 component table | Not built. |
| **Flat / Grouped lineage toggle in Sessions** | council §1.3 | Not built; `Sessions.tsx` is flat only. |
| **Windowed transcript loading** | council §2 item 3 | Not built. |
| **Daily digest card** | council §2 item 7 | Not built. |
| **Claims / proposals review surface** | council P1 #1 | API and CLI exist; no view. This is the single largest "advisory layer with no face" gap. |
| **Session brief surface** | council P1 #2 | API exists (`/brief`, `/brief.md`); no view. |
| **Attribution surface** | council P1 #6 | API exists (`/api/attribution*`); no view. |
| **Token / cost detail endpoints** | dashboard §4.5 | `/api/tokens/by-harness`, `/by-model`, `/timeseries`, `/sessions/{id}/tokens` all exist; only the aggregate inside `/api/summary` is rendered. |
| **`a` keybinding, render-time redaction, resume-in-harness command string** | council §1.7, §4 | None present. |

The v2 "Observatory" redesign (`dashboard-redesign-v2.md`) is, by contrast, substantially built: token swap, constellation graph with SSE spawn/pulse, live orbs, and the three-column Overview all exist (`ConstellationGraph.tsx`, `LiveOrb.tsx`, `Overview.tsx`, screenshots in `docs/ui-screenshots-v2/`).

## 5. Data sources on disk but not ingested

Reconciling `data-inventory.md` against `config.py` and the adapters:

| Source | Volume (inventory) | Status |
|---|---|---|
| Cursor `state.vscdb` composers as **transcripts** | 84 composers, 23.7k bubbles, 237 MB | Read for model/effort/timestamp only (`ingest/cursor.py:260-338`); never a discovery source. Inventory calls it the "richest Cursor history." |
| Codex `~/.codex/archived_sessions/` | 14 MB, 3 files | Not discovered (`ingest/codex.py:161-165`). |
| Codex `state_5.sqlite` | 404 threads, 360 spawn edges | Not read at all. |
| Codex `memories/` + `memories_1.sqlite` | 236 KB MD + 144 KB DB | Not read. |
| Claude `~/.claude/projects/*/memory/` | ~4 memory dirs | Not read. |
| Claude `history.jsonl`, Codex `history.jsonl` / `session_index.jsonl` | 420 KB + 255 KB | Not read. |
| Cursor `conversation-search.db` | 2.6 MB, 39 conversations | Not read. |
| Cursor `ai-tracking/ai-code-tracking.db` | 3 MB, 13.7k hashes, 25 scored commits | Not read; `analysis/attribution.py` uses git only. |
| Repo/global `AGENTS.md`, `CLAUDE.md`, harness `settings.json`, MCP server names | small | Not ingested as evidence. `analysis/claims/scope.py` resolves scopes and `proposals.py` can *write* to such files, but nothing reads their current content into the ledger. |
| Warp `ai_blocks` (assistant text) | 0 rows locally | Genuinely empty; correctly recorded as unavailable (`registry/harnesses.py:132-143`). |
| ChatGPT desktop `.data`, Claude Desktop IndexedDB, VS Code chatSessions, Codex `logs_2.sqlite` | Tier C / low | Not read — matches the inventory's own "optional / hard" tier. |

Separately, and more urgent than any new source: **15 Cursor transcript files already inside the supported discovery path have never been ingested and 9 more are stale** (`docs/verification/db-integrity-report.md:12,47-67,151-163`). Roughly +580 messages and +1,159 tool events are missing from the ledger today.

## 6. Cleanup queue status

`docs/cleanup-queue.md` is **substantially executed**, not outstanding. Two recorded execution passes on 2026-08-09 quarantined (never deleted) ~880 MiB of orphaned Claude plugin cache and unreferenced plugin versions, made Cursor the Superpowers primary and disabled the Claude dual-install, resolved Firecrawl and Mintlify drift, and pinned the Claude Playwright MCP launcher to `@playwright/mcp@0.0.78`. All operations used `mv` into `~/.agentlog-quarantine/<timestamp>/` with `RESTORE.md` and config backups.

Outstanding, by explicit decision:

- `ai-challenge-loan-ref` `.claude/skills` vs `.agents/skills` — content genuinely diverged, so no merge or symlink; diffs parked in `docs/tool-drift-diff-files/repo-*.diff`.
- Oversized skill splitting — deferred pending review.
- Cursor named `firecrawl` / `postman` trees — enablement state lives in `state.vscdb` and could not be confirmed shallowly; left in place.
- Cursor numeric plugin aliases and Codex `.system` skills — out of scope by design.
- **The stated reason for the whole queue — "revisit removal decisions once agentlog can measure what actually fires" — is now partly satisfiable**: `analysis/skills.py` indexes definitions and joins exposures to outcomes, and `skill_exposures` has 336 rows. That evidence has not yet been fed back into a cleanup decision.
- **Unused-skill archive proposals are disabled** until exposure telemetry covers Cursor/Codex skill invocations (zero `skill_exposures` is not non-use). See `docs/review/insights-consult.md` §Follow-up.

## 7. Still worth building — prioritized

Ordered by value to a single-user observatory.

| # | Item | Size | Why |
|---|---|---|---|
| 1 | **Fix Cursor ingest lag** (reparse the 9 drifted artifacts, pick up the 15 missing files, and add a size-drift check to the watcher so `parsed_offset == size` at a stale size cannot look complete) | S | The ledger is silently wrong today. Every downstream metric inherits the error, and this is the cheapest possible fix. |
| 2 | **Close the semantic-label gate loop**: finish `.research/extraction-run-002` (138 pending packets), run the adjudication queue to a real gold set, compute live precision/recall, and make `api/queries.py:734` refuse to publish the lead metric until the §3.4 bars pass | M | The Overview currently publishes a redirect/brake rate derived from labels whose only validation was a scripted fixture run. This is exactly the honesty failure the eval doc was written to prevent. |
| 3 | **Build the Insights view against claims and proposals** — one review surface consuming `/api/claims` and `/api/proposals` with approve / reject / dry-run diff | M | This is the observatory's entire advisory purpose, and it exists as library + CLI + API with no face. Highest value-per-line remaining. |
| 4 | **Surface the harness capability matrix and per-metric availability in the UI** | S | Already fully implemented server-side; renders the product's honesty visible instead of implicit. |
| 5 | **Cursor `state.vscdb` as a transcript source**, with composer-UUID dedupe against agent-transcripts | L | The inventory's own verdict is that transcripts are a subset; 84 composers vs 55 transcript files. Cursor is the harness with the worst coverage and the most missing model/effort. |
| 6 | **Type A quality suite** (`src/agentlog/quality/`, `tests/quality/`, fixture tiers 1 and 4, full-vs-incremental equivalence, repeated-run determinism, `agentlog quality run` with CI exit codes) | L | Item 1 happened because nothing tests ingest equivalence. Sized L, but tiers 1 and 4 plus the equivalence test alone (M) would have caught it. |
| 7 | **Session brief + attribution surfaces in the UI**, and add `get_brief` / `relevant_learnings` tools to the MCP server | M | Two built subsystems with zero consumers, plus the two MCP retrieval tools the council named. Turns agentlog from a dashboard into something later sessions can actually query. |
| 8 | **Complete the evidence fields**: `tool_events.tool_call_id` / `arguments_hash` / `canonical_tool` / `result_code` / `success_source`, and a true `window_hash` over request + response + tool context + builder version | M | Blocks exact retry and tool-failure metrics permanently; also the correct cache-invalidation key for extraction. Cheaper now than after another 2,000 labeled windows. |
| 9 | **Type B descriptive profile**: task taxonomy, first-message pre-treatment classifier with owner override, and `agentlog performance profile` | L | The statistical engine and the schema are already there; what is missing is the taxonomy and the surface. Do it after item 2 — an unvalidated classifier feeding a gated profile is worse than no profile. |
| 10 | **Feed skill-activation evidence back into the cleanup queue** — a "definitions on disk that never fired" report with the token-overhead estimate | S | Closes the loop the cleanup doc explicitly deferred to agentlog, and is a concrete example of the harness-assistant promise. |
| 11 | **Codex `state_5.sqlite` spawn-edge and archived-session ingest** | S | Small, closes two named P0 sub-items, and enriches lineage for the largest corpus. |
| 12 | **Redaction module** before any remote extraction path is used again | S | Only matters if `XAI_API_KEY` extraction returns; the packet flow is local. Do it before, not after. |

### Not worth building

- **Open harness plugin registry replacing the `Harness` enum.** The council wanted it to support "adding Warp or Hermes" — both have since been added by editing the enum and the adapter list, at trivial cost. A single-user observatory watching five harnesses on one machine gains nothing from dynamic registration; the declared capability matrix in `registry/harnesses.py` already delivers the honesty benefit that motivated it. Revisit only if third parties will ship adapters.
- **Embeddings / hybrid RRF retrieval.** ~33k messages with FTS5 on a local disk. Vector search is a mass-market scale answer to a problem this corpus does not have.
- **Learning graph visualization (P2 #3).** The claim corpus is small enough to read as a list; the session constellation already absorbed the "graph" appetite.
- **Remote / team / mobile / encrypted sync (P2 #4).** Pure mass-market framing. Nothing in a single-user local observatory needs it.
- **Active-session learning refresh (P2 #1).** Requires harness-specific hooks agentlog does not control; the assistant framing is satisfied by "next session picks it up" plus MCP query on demand.
- **Any kanban, dispatch, or task-board surface.** Already correctly rejected; do not relitigate.
- **Strands Evals adapter.** Only makes sense with active benchmark execution, which is out of frame.

## 8. Packaging agentlog as an agent plugin

**Recommendation: yes, but only after items 1-4 above, and only as a thin distribution wrapper.**

The reasoning is that agentlog already has the two things a plugin manifest needs to point at, and neither would have to be built for this purpose. The MCP server is real, read-only, and annotated (`mcp_server/server.py`, six tools with `readOnlyHint`), and the package installs cleanly with a console entry point (`pyproject.toml`: `agentlog = "agentlog.cli:app"`). From the plugin shapes visible in the cleanup and skills audits on this machine — `.claude-plugin/plugin.json`, `.cursor-plugin/plugin.json`, `.codex-plugin/plugin.json`, an `.mcp.json` launcher, and one or more `SKILL.md` files — packaging is a manifest, a launcher entry pointing at `python -m agentlog.mcp_server`, and a short skill that tells the agent when to query agentlog. That is roughly a day of work, not a project.

The value is real and specific to the observatory framing: today agentlog can only be consulted by a human looking at a dashboard, and the harnesses it observes cannot ask it anything. A plugin makes "what did I already learn about this repo?" and "give me the brief from the session that touched this file" answerable mid-task by the agent itself, which is the learning-relay half of the product that currently has no consumer.

Three caveats that set the timing. First, do not ship it while the semantic labels are ungated (item 2) — a plugin that hands an agent unvalidated derived claims propagates the error into future work rather than merely displaying it. Second, the plugin must expose retrieval only; the write path (`proposals apply`) stays human-approved on the CLI or the review UI, or the advisory-only guarantee dies the moment an agent can call it. Third, this is a distribution decision, not an architecture one — nothing about the core should change to accommodate it, and if the manifest starts pulling requirements back into `src/agentlog/`, that is the signal to stop.

I could not verify anything about agent-plugins.org itself (no network fetch was performed and the repo contains no reference to it), so this recommendation rests on the plugin layouts observed on disk and described in `docs/skills-audit.md` and `docs/cleanup-queue.md`, not on that registry's actual submission requirements.

## 9. What could not be verified

- **Live database contents.** This audit did not open `~/.agentlog/agentlog.db`. All row counts quoted (605 sessions, 33,196 messages, 1,837 `ux_observations`, 3,268 exchange windows, 480 session commits) come from `docs/verification/db-integrity-report.md`, generated 2026-08-09 13:27 UTC. Extraction run 002 was emitted at 15:39 UTC, after that report, so current `ux_observations` may differ if any of its 138 packets were ingested since.
- **Whether the API and web app run correctly end to end.** `create_app()` imports and registers 65 routes, but no server was started and no view was rendered. UI claims are based on source reading plus the checked-in screenshots.
- **Adjudication progress.** `api/adjudication.py` and `Adjudicate.tsx` are complete, but how many windows have actually been adjudicated is a database question that was out of scope.
- **agent-plugins.org requirements**, as noted in §8.
- The repository was being modified during this audit (`api/proposals.py` appeared at 21:07 while it was in progress), so the proposals API surface may have moved since.
