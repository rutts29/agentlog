# Python backend defect review

Review snapshot: 2026-08-09, approximately 21:10 IST.

Scope: `src/agentlog/` Python backend and `tests/`, with `web/` excluded. This was a read-only source review except for this requested report. I inspected the schema and all migrations, ingest adapters and pipeline, normalization, analysis/extraction, API queries and dependencies, watcher/service code, registry, MCP server, CLI/config/pricing, and tests. I also ran the full test suite and targeted in-memory/temp-directory reproductions. I did not access or write `~/.agentlog/agentlog.db`, restart services, or edit source.

The tree changed while the review was running. Findings touching `analysis/extractors/*` are marked provisional as requested. The claims/proposals API and migration also landed during the review, so the network-safety finding involving those endpoints is provisional to that concurrent landing.

## Executive summary

I found two critical confirmed defects:

1. migration v015 leaves foreign-key enforcement disabled on every initialized/migrated connection; the existing durable-label regression test fails as a direct consequence;
2. the headline redirect/brake metric reads a field that does not contain redirect/brake labels and can report zero when every labeled window is a redirect.

The next most serious defects are semantic aggregates that mix failed audit runs and reruns while claiming full availability, malformed JSONL tails that are permanently checkpointed past, an unauthenticated API that can expose transcripts and now apply edits to agent configuration when bound beyond loopback, incomplete root-cluster handling, model/token aggregation that assigns message-level data to the wrong model, and durable label identity that omits context actually sent to the labeler.

The full unrestricted suite ran 246 tests in 8.553 seconds and failed with one error:

`tests.test_durable_labels.LabelSurvivalTests.test_orphan_marking_instead_of_deletion` raises `sqlite3.IntegrityError: UNIQUE constraint failed: exchange_windows.session_id, exchange_windows.request_message_id, exchange_windows.response_message_id`.

The attribution tests pass when run outside the restricted command sandbox (8 tests, OK). Seven `git init` errors from the first sandboxed full-suite run were environmental and are not product findings.

## Confirmed defects

### Critical

#### C1. Migration v015 leaves SQLite foreign keys disabled

- Confidence: 100/100
- References: `src/agentlog/db/migrations/v015_derive_watermarks.py:89-95`; `src/agentlog/db/schema.py:171-177`; `src/agentlog/db/repository.py:467-495`; `tests/test_durable_labels.py:235-303`
- What is wrong: v015 executes schema SQL, sets `PRAGMA foreign_keys = OFF`, performs DML that opens a transaction, and then executes `PRAGMA foreign_keys = ON` inside that transaction. SQLite ignores changes to `foreign_keys` while a transaction is active. `init_db()` commits afterward, but never re-enables or verifies the pragma.
- Concrete consequence: the same connection proceeds with foreign keys off. Deletes no longer cascade from messages to `exchange_windows`; orphan rows survive and collide with replacement windows. More generally, every declared relationship can silently admit or retain invalid rows for the lifetime of the connection. This defeats the integrity model on first ingest and migration.
- Reproduction:
  1. Run `conn = connect(temp_db); init_db(conn)`.
  2. Run `conn.execute("PRAGMA foreign_keys").fetchone()[0]`.
  3. Observed result: `0`.
  4. Running `.venv/bin/python -m unittest tests/test_durable_labels.py -q` reproduces the uniqueness error during re-ingest.
  5. A migration-by-migration trace showed v015 changing the pragma from `1` to `0`; v016 then ran with it off.
- Recommended fix: run v015's table rebuild in an explicit migration boundary where `foreign_keys` is disabled before any transaction, commit the rebuild, then re-enable it outside the transaction. Assert `PRAGMA foreign_keys = 1` after every migration batch and run `PRAGMA foreign_key_check`; make `init_db()` fail loudly if either check fails. Add a regression test that checks the pragma and foreign-key behavior after both fresh initialization and upgrading an older database.

#### C2. Redirect/brake metrics inspect the wrong UX field

- Confidence: 100/100
- References: `src/agentlog/api/queries.py:681-731`, especially `:717-724`; `src/agentlog/api/queries.py:734-798`, especially `:781-791`; `src/agentlog/analysis/extractors/storage.py:109-139`; `docs/research/eval-architecture.md:367-373`
- What is wrong: UX observations store semantic labels such as `user_redirect` in `turn_kinds_json`. Both redirect metric implementations ignore that column and instead look for `redirect_brake` or `had_redirect_brake` in `flags_json`. `flags_json` is the reliability/quality flags model, not the turn-kind output.
- Concrete consequence: the headline metric emits plausible but wrong values. A fully labeled redirect corpus can be reported as zero redirects. This is the product's worst failure mode: a trusted number with the opposite meaning of the evidence.
- Reproduction: seed ten linked `ux_observations` with `turn_kinds_json='["user_redirect"]'` and ordinary empty flags, then call `semantic_lead_metric(conn, all_time_range)`. The targeted reproduction returned `{"labeled_redirects": 10, "status": "ok", "estimate": 0.0, "n_clusters": 10}`.
- Recommended fix: define the exact turn kinds that enter the numerator and query `turn_kinds_json` (prefer a normalized observation-label table or `json_each`, not ad hoc string matching). Restrict the denominator to eligible human-supervisor substantive windows as required by the evaluation contract. Add behavior tests where true positives, abstentions, worker briefs, and ordinary turns are inserted through `write_ux_observations`, then assert numerator, denominator, estimate, and support status.

### High

#### H1. Semantic aggregates mix audit rows, reruns, and partial coverage while reporting availability 1.0

- Confidence: 98/100
- References: `src/agentlog/analysis/extractors/pipeline.py:73-137`; `src/agentlog/analysis/extractors/storage.py:87-142`; `src/agentlog/api/queries.py:63-75`, `:687-731`, `:763-798`
- What is wrong:
  - `run_audit_phase()` writes predictions to the production `ux_observations` table before the gate result is known and retains them when the gate fails.
  - observations are unique per `(window_id, run_id)`, so rerunning an audit or a full extraction adds another live row for the same window.
  - the API does not select a canonical completed/authorized run, deduplicate windows, check derivation-run status, or distinguish audit from full-corpus output.
  - it sets `availability=1.0` unconditionally and uses only observed rows as the denominator, even if observations cover a small sample of eligible windows.
- Concrete consequence: failed evaluator output can become a user-facing metric; reruns double-weight windows; a 100-window audit can be presented as complete corpus coverage; and estimates can change merely because extraction was rerun. This violates the binding denominator and partial-coverage rules.
- Reproduction: run the audit phase twice against the same linked windows, or insert two completed run IDs for each window, then query `semantic_lead_metric`. `COUNT(*)` and per-root `n` double although the evidence corpus did not. Insert observations for only a small subset and note that the returned cell still has availability 1.0.
- Recommended fix: add an explicit run purpose/status/authorization contract and a single published run pointer. Aggregate only one published, gate-passing row per eligible window. Compute coverage as `distinct observed eligible windows / all eligible windows`, return both counts, and abstain below the documented coverage gate. Keep audit predictions isolated from production aggregates.

#### H2. Malformed or partially written JSONL tails are permanently checkpointed past

- Confidence: 98/100
- References: `src/agentlog/ingest/base.py:196-220`; `src/agentlog/ingest/pipeline.py:181-199`; adapter loops such as `src/agentlog/ingest/codex.py:167+`
- What is wrong: `iter_jsonl_bytes()` treats a final unterminated byte slice as a complete line and yields a JSON error through `next_offset == len(data)`. Adapters advance `bytes_consumed` to that end offset even for the error. The pipeline persists the reported offset. If the writer later completes the line, append ingestion starts after the malformed prefix and sees only the suffix, so the completed record can never be reconstructed.
- Concrete consequence: normal concurrent observation of an in-progress JSONL write causes silent, permanent message loss until a full reparse is forced. Derived windows, token totals, tool linkage, and labels then operate on an incomplete transcript.
- Reproduction:
  1. Create a transcript containing one valid line followed by an unterminated `{"type":`.
  2. Ingest it and record `parsed_offset`; it equals the full file size.
  3. Append the remainder of the JSON object and a newline, then ingest incrementally.
  4. The targeted reproduction retained only the first message, emitted another warning, and produced no recovered window.
- Recommended fix: if there is no terminating newline, retain the final line by reporting its start offset as the safe checkpoint. More generally, advance the checkpoint only through successfully framed records; distinguish malformed complete lines from an incomplete tail. Add append tests that stop at every byte position of a multibyte UTF-8 JSONL record and then complete it.

#### H3. The unauthenticated “read-only” server can disclose transcripts and modify agent configuration

- Confidence: 96/100
- Provisional: the proposals endpoints landed during this review
- References: `src/agentlog/cli.py:817-853`; `src/agentlog/api/app.py:72-93`, `:415-418`; `src/agentlog/api/queries.py:429-499`; `src/agentlog/api/descriptive.py:590-674`; `src/agentlog/api/proposals.py:82-148`; `src/agentlog/analysis/claims/apply.py:167-249`
- What is wrong: `agentlog serve` accepts any `--host`, has no authentication or origin/CSRF enforcement, and describes itself as a read-only dashboard. The API returns full message text and has mutating endpoints. The newly included proposals router can refresh, approve, apply with `dry_run=false`, and roll back changes to real files under the owner's home directory.
- Concrete consequence: binding to `0.0.0.0` exposes the owner's complete coding transcripts to the LAN and lets any reachable client mutate the database. With an available proposal, a remote client can approve and apply edits to `AGENTS.md`, rules, or skill files. CORS is not authentication and does not stop `curl`, non-browser clients, DNS rebinding, or all cross-origin side effects.
- Reproduction: on a disposable database/copy, run `agentlog --db <copy> serve --host 0.0.0.0`, then from another host request `/api/sessions/<id>` and `/api/proposals`. POST `/api/proposals/<id>/approve`, followed by `/api/proposals/<id>/apply?dry_run=false`, demonstrates the mutation path. Do not use the production database for this check.
- Recommended fix: refuse non-loopback binds unless an explicit unsafe/remote mode is supplied and authenticated. Add a random bearer token or authenticated local session for every endpoint, plus CSRF protection for browser mutations. Separate the read-only dashboard app from mutating administration/publication APIs. Correct the CLI description and print a prominent warning when any non-loopback bind is requested.

#### H4. Durable UX identity omits context that changes the label

- Confidence: 93/100
- Provisional: `analysis/extractors/*` is under active edit
- References: `src/agentlog/analysis/windows.py:15-35`, `:45-89`; `src/agentlog/analysis/extractors/window_context.py:191-206`; `src/agentlog/analysis/label_survival.py`
- What is wrong: a window's durable content hash includes only session ID, user text, and assistant text. The UX labeler is also given `next_user`, tool timeline, skill names/exposure types, harness, and model. Re-ingest can correct any of those contextual inputs without changing the hash, so the old observation remains linked as if it were valid for the new extractor input.
- Concrete consequence: expensive labels and hand adjudications can silently attach to semantically changed evidence. For example, correcting previously orphaned tool events or a next-user response can change whether an interaction is a successful correction, but content-hash relinking preserves the stale result.
- Reproduction: create a window, label it, then change only a linked tool event, `next_user_text`, skill exposure, or model and rebuild/relink. `compute_window_content_hash()` is unchanged, so the old label stays `linked`.
- Recommended fix: separate visible exchange identity from extractor-input identity. Preserve durable human labels by the visible identity, but record a versioned extraction-input hash covering every field consumed by that extractor. Mark machine observations stale when their input hash changes; require human adjudication rematch or explicit review when context that affects the adjudication changed.

#### H5. Root-cluster aggregation handles only one parent level and sometimes drops children entirely

- Confidence: 96/100
- References: `src/agentlog/api/queries.py:687-731`, `:763-798`; `docs/research/eval-architecture.md:356-372`
- What is wrong: global semantic aggregation uses `COALESCE(s.parent_session_id, s.id)` rather than resolving the full ancestry to a canonical root. A grandchild is grouped under its immediate child, not the root. Parent IDs may also be represented as an external or canonical ID. The model-conditioned query adds `s.parent_session_id IS NULL`, excluding every child window instead of rolling descendants into the root.
- Concrete consequence: orchestration-heavy tasks manufacture extra independent clusters globally, while model cells omit worker evidence entirely. Sample sizes, confidence gates, and rates depend on tree depth and ID representation rather than task count.
- Reproduction: seed root → child → grandchild sessions with one observation each. The global query produces two cluster keys (root and child), not one. `_model_redirect_cell` sees only the root session's windows.
- Recommended fix: materialize canonical root IDs during ingest or use a recursive CTE with cycle protection. Roll every descendant to exactly one root cluster and test mixed canonical/external parent IDs, grandchildren, and cross-harness handoffs.

#### H6. Message-level model usage is attributed to the session model in coverage and activity rollups

- Confidence: 95/100
- References: `src/agentlog/api/tokens.py:546-578`, `:650-713`; `src/agentlog/api/activity.py:390-440`, `:503-523`
- What is wrong:
  - token contribution rows correctly prefer `u.model_canonical`, but the session denominator for `group_by="model"` uses only `s.model_canonical`;
  - the message denominator can use `m.model_canonical`, so contribution, session coverage, and message coverage are grouped by different identities;
  - activity sessions are grouped by session model, while message counts are grouped by message model and then left-joined to session-model rows. A switched-to model with no session row disappears from `by_model`.
- Concrete consequence: model coverage tuples can be internally contradictory, and model activity undercounts or drops switched-model messages. A two-message session whose second message uses another model reports a harness total of two but only one model-row message in the targeted reproduction.
- Reproduction: create one session canonicalized as model A with messages/usage on A and B. Query token `usage(..., group_by="model")` and `activity.rollup`. The targeted result included model A with session coverage and model B with message coverage but zero sessions; the activity model rows summed to one message while the harness row contained two.
- Recommended fix: define separate session-level and message-level model dimensions. For message/token metrics, group every numerator and denominator by the resolved message/usage model. For session counts, explicitly expose `session_start_model`, `dominant_model`, or `models_seen` rather than joining unlike grains. Add invariants that model rows reconcile to the harness total for additive message metrics.

#### H7. UX extraction sends unredacted transcript content to xAI

- Confidence: 100/100
- Provisional: `analysis/extractors/*` is under active edit
- References: `src/agentlog/analysis/extractors/window_context.py:191-206`; `src/agentlog/analysis/extractors/ux_extractor.py:250-263`; `src/agentlog/analysis/extractors/llm_client.py:15-55`
- What is wrong: the extraction payload contains truncated but otherwise unredacted user and assistant text, next-user text, tool timeline, model, and skill names. `XAIChatClient` posts it to `https://api.x.ai/v1`. The full-run gate and owner authorization concern evaluation quality; they do not implement redaction, secret detection, data classification, or a specific egress confirmation.
- Concrete consequence: source snippets, credentials pasted into prompts, customer data, private paths, and other coding-history content can leave the machine. Truncation bounds size, not sensitivity. This contradicts a strict local-only safety claim.
- Reproduction: pass a `WindowContext` containing a unique canary secret to `truncate_for_ux`; the canary appears verbatim in the payload passed to `complete_json`, which constructs the remote HTTP request body.
- Recommended fix: either use a local model by default or make remote extraction an explicit, separately named opt-in that states exactly what leaves the machine. Apply deterministic secret/path/content redaction before payload construction, show/export the exact outbound audit pack for owner review, record provider and redaction version, and add a no-network mode enforced below the CLI.

#### H8. Tool events linked by message ID disappear from UX window context

- Confidence: 100/100
- Provisional: `analysis/extractors/*` is under active edit
- References: `src/agentlog/analysis/extractors/window_context.py:110-138`; Codex linkage in `src/agentlog/ingest/codex.py`
- What is wrong: the context loader selects tools using `tool_events.seq > request_message.seq AND seq < next_user.seq`. Tool-event sequence numbers are not guaranteed to share the message sequence coordinate system. Codex can link a tool to response message sequence 11 while the tool's own sequence is 1, so the event is excluded despite a valid `message_id`.
- Concrete consequence: the UX extractor sees `tool_count=0` and no tool timeline for windows that did use tools. Triage, semantic labels, and any tool-conditioned observations are wrong precisely in sessions whose linkage was recently repaired.
- Reproduction: seed request message seq 10, response message seq 11, and a tool event with `message_id` equal to the response ID but tool `seq=1`. `load_window_contexts()` returned `context_tool_count: 0` in the targeted check.
- Recommended fix: select tools primarily through `message_id` for all assistant messages in the window. Use timestamps or an explicit event ordering field only for truly orphaned tools. Do not compare sequence columns from different source domains. Add fixtures where message and tool sequences diverge.

### Medium

#### M1. Attribution rebuild destroys valid prior results before discovering per-session failures

- Confidence: 92/100
- References: `src/agentlog/analysis/attribution.py:424-481`
- What is wrong: `rebuild_attribution()` starts with an unconditional `DELETE FROM session_commits`, then catches broad exceptions per session and continues. A transient Git error, temporarily unavailable repo, lock, or parser defect therefore converts a previously attributed session into no attribution and still commits the partial rebuild.
- Concrete consequence: a maintenance command can silently degrade attribution coverage and permanently discard valid explicit/inferred joins. The returned error list does not restore prior rows.
- Reproduction: first populate `session_commits`; then make one repository unresolvable or make the Git command fail and run rebuild. Existing rows for that session are deleted and not restored, while successful sessions commit.
- Recommended fix: build into a staging table/run ID, retain errors, and atomically publish only successful per-session replacements. Preserve old rows for failed sessions, or abort the whole publish when failures exceed an explicit policy. Catch expected Git/SQLite exceptions rather than every `Exception`.

#### M2. Session-detail alias resolution returns metadata with an empty transcript

- Confidence: 100/100
- References: `src/agentlog/api/descriptive.py:590-663`
- What is wrong: `_resolve_session()` can resolve an external/alias ID to the canonical session row, but every child query still binds the original `session_id` argument rather than `resolved["id"]`.
- Concrete consequence: `/api/sessions/abc` can return session metadata for `codex:abc` while reporting zero messages, tools, windows, skills, and children. The response is internally inconsistent and looks like data loss.
- Reproduction: seed session ID `codex:abc`, external ID `abc`, and one message. `session_detail_v2(conn, "abc")` returned `resolved_id="codex:abc"` with `messages=0` and anatomy message count 0.
- Recommended fix: bind one canonical `resolved_id` to every dependent query and use aliases only at the lookup boundary. Add API tests for canonical IDs, external IDs, Cursor IDs containing slashes, and legacy aliases.

#### M3. Equal-length Cursor duplicate imports can replace richer data with poorer data

- Confidence: 86/100
- References: `src/agentlog/ingest/pipeline.py:163-199`, `:203+`; `src/agentlog/db/repository.py` duplicate/session replacement logic; `src/agentlog/ingest/cursor_merge.py`
- What is wrong: the duplicate-session safeguard compares prior and new message counts. A poorer duplicate is skipped only when it has fewer messages; equal message counts proceed through full replacement. Equal-length copies can still differ in tool events, per-message model/effort, timestamps, parent linkage, or metadata richness.
- Concrete consequence: discovery order among duplicate Cursor storage copies can erase richer facts without changing message count. This recreates the same class of path-derived duplicate/data-quality problem in a subtler form.
- Reproduction: ingest a canonical Cursor session with N messages plus linked tools/model metadata, then ingest another artifact resolving to the same external ID with N messages but missing those fields. Observe the replacement of session-owned rows.
- Recommended fix: merge by a deterministic source-quality score and field-level provenance, not message count. At minimum compare message IDs/content hashes and counts of tools, usage, skills, timestamps, and non-null identity fields; never destructively replace richer rows with an equal-length poorer copy. Add order-invariance tests.

### Low

#### L1. Superseded API implementations remain live as unused, behaviorally inconsistent code

- Confidence: 100/100
- References: `src/agentlog/api/queries.py:336-426` and `:429-499`; active replacements in `src/agentlog/api/descriptive.py`; routing in `src/agentlog/api/app.py:330-354`
- What is wrong: `queries.list_sessions()` and `queries.session_detail()` have no Python call sites; the app uses `descriptive.list_sessions_v2()` and `session_detail_v2()`. The dead `list_sessions()` also applies project filtering after SQL pagination while returning the unfiltered total, so reviving it would produce empty/short pages and wrong cursors. Project-label derivation is duplicated across queries, descriptive/tokens, and the new claims scope code.
- Concrete consequence: maintainers can fix or test the wrong implementation, and future route changes can accidentally reactivate known-bad pagination semantics. Project labels can diverge across endpoints.
- Reproduction: repository-wide Python search finds no call to `queries.list_sessions` or `queries.session_detail`; app routing points to the v2 functions.
- Recommended fix: delete the superseded functions and consolidate project identity/label derivation in one normalized module. If compatibility requires them, make them thin delegates to the canonical implementation and test pagination behavior.

## Suspicions needing investigation

These are not stated as confirmed production failures.

### S1. Generic CLI writers retain SQLite's short default lock timeout

- Confidence: 84/100
- References: `src/agentlog/db/schema.py:123-127`; contrast `src/agentlog/api/deps.py:13-17`, `:61-99` and watcher-specific connection setup
- Concern: the common `connect()` enables foreign keys but does not set the 30-second busy timeout or WAL. API and watcher paths compensate independently; CLI commands and future callers using the common connector can fail after SQLite's default timeout while the daemon holds a write lock.
- Investigation: hold `BEGIN IMMEDIATE` from one process while running each mutating CLI command against a disposable database. Record wait duration and whether each command retries, fails cleanly, or leaves a partial transaction.
- Suggested resolution if confirmed: put the common connection policy in one factory with explicit read-only/read-write modes, WAL, foreign keys, busy timeout, and bounded retry semantics.

## Highest-value missing tests

1. Fresh and upgraded databases must finish with `PRAGMA foreign_keys=1` and an empty `PRAGMA foreign_key_check`.
2. Append ingestion must recover every possible partial final JSONL line, including split UTF-8, malformed complete lines, truncation, and rewrite.
3. Semantic metrics must be seeded through the real storage layer and assert label-field mapping, distinct-window semantics, published-run selection, eligible denominator, coverage, abstention, and root clustering.
4. Repeated audit/full runs and failed audit gates must not change published aggregates.
5. Root → child → grandchild and cross-harness parent-ID variants must resolve to exactly one analytical cluster.
6. Model-switch sessions must reconcile message and token totals across harness/model dimensions; current tests do not expose the unlike-grain join.
7. Window-label staleness must be tested when next-user text, tools, skills, harness, model, or linkage changes without visible exchange text changing.
8. Cursor duplicate ingestion must be order-invariant for equal-message-count sources with unequal richness.
9. Alias-based session-detail requests must return the same transcript as canonical IDs.
10. Server tests should assert non-loopback binding policy, authentication, CSRF protection, and separation of read-only from mutating/publication routes.
11. Attribution rebuild tests should preserve old rows for sessions whose refresh fails.

The existing durable-label test is valuable and currently catches C1. The current aggregation tests can pass while C2/H1 remain wrong because they exercise synthetic flag shapes rather than storing real `turn_kinds_json` through the extractor storage contract. Token tests cover cumulative-vs-additive logic well but do not reconcile switched-model message-level grouping against session-level grouping.

## Calibrated positives

- External Cursor SQLite databases are opened with `mode=ro`; I found no write path into audited third-party transcript databases.
- Git attribution uses argument-vector subprocess calls with `shell=False`; I found no shell-injection path.
- Codex cumulative token snapshots are intentionally reduced to the final snapshot rather than summed, while additive message usage is handled separately.
- The model identity resolver preserves raw provenance and returns unknown for providers/placeholders/profiles rather than inventing a base model.
- Full-corpus UX extraction has a quality gate and explicit owner authorization. That is a sound statistical control, though it does not replace privacy consent/redaction.
- The durable-label redesign correctly recognizes that expensive labels must not use cascading foreign keys. The soft-link/orphan/relink direction is good; the migration-state defect currently prevents it from working reliably.
- API read dependencies use SQLite URI `mode=ro`, close per-request connections, and set a busy timeout. Write dependencies centralize rollback and retry better than the generic connector.
- Proposal publication uses a content-hash conflict check, backup, temporary file, and atomic replacement. Those local file-safety mechanics are thoughtful; the missing API trust boundary is the problem.
- The evaluation architecture explicitly forbids causal overclaiming, requires denominators, and treats orchestration/root clustering as first-class. Several API implementations need to catch up to that contract, but the contract itself is unusually clear.
- Test coverage is broad for a one-day codebase, and the surviving durable-label test demonstrates useful behavior-level intent rather than merely asserting implementation.

## Verification record

- `.venv/bin/python -m unittest discover -s tests -q` outside the restricted sandbox: **failed**, 246 tests run, 1 error (C1 regression).
- `.venv/bin/python -m unittest discover -s tests -p 'test_attribution.py' -q` outside the restricted sandbox: **passed**, 8 tests.
- Targeted temporary/in-memory checks confirmed:
  - `PRAGMA foreign_keys` is `0` after `init_db`;
  - migration tracing changes it from 1 to 0 in v015;
  - ten stored redirect turn kinds can produce estimate 0.0;
  - malformed tail checkpointing loses the subsequently completed record;
  - a linked Codex tool can yield UX `tool_count=0`;
  - switched-model activity rows do not reconcile to harness message totals;
  - alias session detail resolves metadata but returns no messages.
