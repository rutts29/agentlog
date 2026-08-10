# agentlog Evaluation Architecture

**Status:** implementation-driving design  
**Date:** 2026-08-09  
**Scope:** evaluation of agentlog's pipeline and evaluation of coding-agent performance from local historical data

## Executive decision

agentlog needs two evaluation systems with a one-way relationship:

- **Type A — evals OF agentlog** are software-quality checks. They ask whether ingestion, normalization, windowing, extraction, citations, and incremental processing are correct. They run against fixtures and gold labels in isolated databases and can fail a build.
- **Type B — evals BY agentlog** are product analytics. Their resting surface is a descriptive model-usage and interaction-style profile of this owner's history—not a performance ranking. They may surface matched-cohort associations only when the §5.2 procedure fully passes. They run against the production evidence ledger and must preserve uncertainty, missingness, and confounding.

Do not put both behind a generic `evals` package, generic `eval_results` table, or shared “score.” Type A must never appear in the dashboard as user evidence. Type B must never become a CI gate for the parser.

The Strands Agents Evals SDK should **not be an agentlog dependency**. Its `Case → Experiment → Evaluator → EvaluationReport` shape, evaluation levels, custom-provider boundary, result caching, and CI exit-code conventions are worth copying as patterns. Its concrete runtime is centered on executing agents or converting OpenTelemetry traces into a Strands `Session`; it has no SQLite-backed cohort discovery, observational comparison model, or confounding controls. Pulling it in would add the Strands runtime, tools, OpenTelemetry, boto3, and related dependencies to a deliberately small local reader.

## Reconciliation notes

Two independent reviews designed the Type B evaluation layer. This document is Reviewer A's architecture with Reviewer B's statistical constraints merged in. Audit trail:

**Agreed**

- Observational language only; no causal “best model” claims from historical selection.
- Root-task clustering for independence; windows for redirect/brake, correction, and other semantic evidence.
- Pre-treatment task labeling; outcomes must not define cohorts.
- Wilson intervals, missingness/capability enforcement, and hard abstention.
- Type A / Type B separation; Strands as patterns only, not a dependency.

**Differed, and how resolved**

| Topic | Reviewer A | Reviewer B | Resolution |
|---|---|---|---|
| Default product surface | Task-conditioned configuration matrix that can rank when gates pass | Descriptive model-usage & experience profile; 4-way ranked cross-tab not estimable | Descriptive profile is the resting state. Ranking only if §5.2 fully passes, labeled as matched-cohort association. With current data, almost no cell is expected to qualify. |
| Cell estimability | `model × harness × effort × task` as the comparison cell | Structural zeros (effort Codex-only; model near-nested in harness/era) plus confounding by indication | Keep the cell for descriptive stratification. Do not treat the full ranked cross-tab as estimable. Surface the owner's model-selection pattern as the differentiating signal. |
| Display / abstention gates | Round-number n tiers (5 / 15 / 30) | Wilson half-width precision gates (e.g. ≤10pp) plus cluster-adjusted event minima | Precision gates are binding (Wilson ≤10pp for binary rates; versioned cluster-bootstrap half-width for the continuous lead rate). Retain tier language for readability only when it does not loosen the precision gate. |
| Lead metric | Corrections among several peer outcomes; duration usable for efficiency comparisons | Corrections per 10 exchange windows as lead, framed as collaboration experience; duration contextual only; retry/tool-failure within-harness only | Initially adopted B's corrections lead. **Superseded by empirical revision + gap-closure:** redirect/brake rate is the lead *descriptive interaction-style* metric—not a quality-defect rate. |
| Per-model support | Exact variants kept separate; Cursor blocked until model/effort recovered | Only gpt-5.5 (303) supports a `model × task` grid; gpt-5.6-sol (47) and grok-4.5-build (69) support 2–3 strata; else abstain | Record those counts in §4.4. Most configurations abstain. **Superseded for Cursor:** model/effort now recovered for most transcripts (§4.4); cells without a resolved model still abstain. |
| Classifier validation n | ≥250 adjudicated root sessions before published task-conditioned comparisons | ~120 suggested | Staged: ~120 to unblock development; ≥250 required before any task-conditioned comparison is published (§4.3). |
| Task-label evidence | First request plus other pre-treatment signals (constraints, repo, file types) | First user message only, gated on a hand-labeled gold set | Task primary labels from the first substantive user message only, gated on gold. Other pre-treatment signals remain for difficulty features, not primary-task assignment. |
| Path to causal rigor | Absent | Opt-in coin-flip randomization for one pre-registered comparison | Added as §6. Upgrades exactly one comparison; UI must state that scope limit. |

**Not weakened:** wherever the reviews differed in strictness, the stricter bar is kept (publish still requires 250; ranking still requires all ten §5.2 gates; precision gates do not replace A's minimums with looser counts).

### Empirical revision (2026-08-09)

After the theoretical reconciliation above, a deep read of 40 stratified exchange windows from the real corpus (documented in `docs/research/extraction-test-run.md`) contradicted several design choices. This pass revises the document to match that evidence. Changes that conflicted with prior theory keep the stricter statistical bar and record attainability honestly.

| Topic | Prior design | Empirical finding | Revision |
|---|---|---|---|
| Lead metric | `corrections_per_10_exchange_windows` | Crisp corrections uncommon and hard to separate from ordinary follow-ups; keyword “correctionish” ~17% is false-positive-dominated. Mid-task redirects / premature-action brakes are real and common; agent pushback is measurable. | Lead metric → `redirects_brakes_per_10_exchange_windows` as a **descriptive interaction-style measure** (frequency aids detection power, not a quality verdict). Corrections demoted to secondary with mandatory abstention. Agent pushback added as a tracked metric. Framing corrected in gap-closure pass below. |
| Effective corpus | Thresholds sized against 4,942 windows | ~64% of windows are harness glue / empty / auto-review / stubs; human-substantive triage keeps ~1,782 (≈1,800). | All sample-size and labeling thresholds re-justified against ~1,800 eligible windows. Unattainable gates abstain rather than quietly loosen. |
| Population | Exclude auto-review and subagent independence | Also: Cursor synthetic subagent-followup messages, skill-body dumps, image-only turns. | Expand mandatory exclusions before any metric (§4.5). |
| Claude Code text | Implicitly usable after semantic validation | `extraction-test-run.md` reported ~90% empty Claude requests as a blocker. That emptiness was an **adapter bug** (tool results under the Anthropic `user` role recorded as empty human messages). **Fixed and verified:** the adapter yields exactly **331 human turns** and **~1,630,098 characters**, matching the raw files (2,871 Claude user rows → 2,540 tool plumbing + 331 genuine human turns). | Claude is **not** blocked. The 331-turn Claude corpus is modest and constrains Claude cell sizes; do not treat it as Codex-scale. Do not rewrite the dated evidence note; cite this correction when that finding is referenced. |
| Tool failure | Treatable from `tool_events.success` / source-native flags | `success` is mostly NULL (53,542 NULL · 1,208 success=1 · 133 success=0); failure needs exit codes in payloads, repeated identical calls, API-error text, or harness-specific parsing. | Weaken availability; mark `estimated`/`unknown` when only the sparse success column is present. |

### Gap-closure revision (2026-08-09)

A follow-up pass closed five gaps left after the empirical revision. Statistical bars are unchanged; framing and metric supportability are corrected to match `extraction-test-run.md`.

| Gap | Prior residual | Resolution |
|---|---|---|
| Lead-metric framing | Redirect/brake promoted for frequency/detectability, but UI and comparison copy still treated higher rates as worse | Reframe throughout as a **descriptive interaction-style measure**, not a quality-defect rate. Higher is not automatically worse; exploratory steering must not read as underperformance. §5.5 forbids defect/quality phrasing for this metric. |
| `clean_completion` | Required no redirect/brake; correction abstention → frequent `unknown` | **Retired.** Redirects are normal collaboration, so disqualifying them conflates steering with failure; with redirects common and correction often abstained, the composite rarely supports a claim. No replacement success proxy until terminal-outcome evidence exists. |
| Multi-agent orchestration | Type B modeled mostly user↔assistant; evidence shows supervisor–worker orchestration is the dominant style | First-class Type B representation (§4.1): orchestration labels, supervisor vs worker turns, root-cluster attribution (consistent with existing non-independence), supervisor- vs worker-level metrics. |
| Soft approval & frustration | Rules only implied outside Type B outcomes | Explicit Type B rules (§4.5): soft approval never terminal task success; frustration defaults to abstain unless affect is explicit. |
| Cost / truncation | Eval reprocessing had no input-size or budget bound | Import evidence caps: triage first; truncate fields (typically 2–4k chars); budget is binding (§4.5, Phase 3). |
| Cursor model/effort | Blocked pending recovery | Recovered from `state.vscdb` (`composerData.modelConfig`, join by composer UUID): **15/18** transcripts resolve a real model, **14/18** an effort; leave null where genuinely absent. Cells without a resolved model still abstain. |

## 1. Grounding in the current repository

The design extends the Week-1 architecture rather than replacing it:

- `src/agentlog/ingest/base.py` defines the right read-only adapter boundary.
- `src/agentlog/ingest/pipeline.py` provides per-artifact failure isolation and incremental/full decisions.
- `src/agentlog/ingest/checkpoint.py` contains the prefix-hash checkpoint logic that Type A must prove equivalent to full ingestion.
- `src/agentlog/db/schema.py` contains the evidence ledger: artifacts, sessions, messages, tools, skills, and exchange windows.
- `src/agentlog/db/repository.py` writes normalized records and reconstructs exchange windows after each ingest.
- `src/agentlog/analysis/windows.py` defines the current semantic unit as a user message paired with the next assistant message. Empirical reconstruction must use the `seq` range through the next user turn: **2,695 / 4,942** windows contain more than one assistant message before the next user message, so request+response IDs alone miss narration, wait loops, and pushback evidence.
- `src/agentlog/normalize/models.py` defines the current common shape and exposes where source capabilities are absent.
- `src/agentlog/ingest/codex.py`, `claude.py`, and `cursor.py` show that outcome observability differs by harness.
- `docs/dashboard-design.md` correctly reserves Insights for derived claims, requires explicit confidence, and makes every aggregate drill down to sessions.
- `docs/research/council-synthesis.md` requires immutable evidence, versioned derivations, local-first operation, and observational rather than causal language for effectiveness claims.

There is one correctness issue to fix before semantic extraction: `exchange_windows.input_hash` is currently only the request message's `content_hash` (`analysis/windows.py`). A changed assistant response or included tool result would not invalidate an extraction. Replace it with a `window_hash` over the canonical request, response, included tool events, and a `window_builder_version`.

The current schema is also insufficient for exact retry and cost metrics. `tool_events` discards call identifiers and argument fingerprints, and there is no token-usage table. Type B must not infer these missing fields from absence.

## 2. Separation of concerns

### 2.1 Storage boundary

Use two physical SQLite databases:

1. `~/.agentlog/agentlog.db` remains the evidence, derivation, and Type B analytics database.
2. `.agentlog/evals/pipeline-evals.db` in a checkout or an explicitly supplied temporary path stores Type A run results. CI should create it in a temporary directory and discard it.

Checked-in Type A fixture manifests and sanitized fixture data live under `tests/quality/`. Private fixtures sampled from the owner's corpus live outside Git under `~/.agentlog/fixtures/`. Gold files refer to stable fixture IDs and hashes, not absolute source paths.

This physical split prevents accidental joins between a parser's test score and a user's model-performance result. It also allows destructive fixture setup without touching the owner's database.

### 2.2 Code boundary

Use names that state the purpose:

```text
src/agentlog/
  quality/                    # Type A only
    cases.py
    runner.py
    metrics.py
    citations.py
    adversarial.py
    reports.py
  analysis/
    derivations/              # production extractors used by the product
    performance/              # Type B only
      taxonomy.py
      outcomes.py
      cohorts.py
      comparisons.py
      claims.py
tests/
  quality/
    fixtures/public/
    manifests/
    gold/
    test_ingest_contracts.py
    test_incremental_equivalence.py
    test_extractor_regressions.py
```

The commands should be distinct:

```text
agentlog quality run [--suite fast|full|semantic|adversarial]
agentlog performance refresh [--since ...]
agentlog performance profile [--since ...]
agentlog performance compare --task debug --metric redirects_brakes_per_10_exchange_windows
```

`profile` is the default product command. `compare` remains available but must abstain unless §5.2 fully passes.

`quality` may import production parsers and extractors. `analysis.performance` must not import `quality`, read Type A result databases, or vary behavior based on test outcomes. Both may use small shared value types such as evidence references, but not a shared repository class or result table.

### 2.3 Data-flow boundary

```text
raw local artifacts
  -> normalized evidence ledger
  -> versioned production derivations
  -> task labels + outcome observations
  -> Type B cells/comparisons/claims

sanitized/private fixtures + gold labels
  -> isolated pipeline under test
  -> Type A case results and gates
```

The only permitted arrow between the systems is from production incidents to new fixtures: when Type B or dashboard use reveals a pipeline mistake, minimize the source example and add it to Type A. No Type A score is product evidence.

## 3. Type A — evals OF agentlog

### 3.1 Test three contracts separately

Do not hide all correctness behind one end-to-end score.

1. **Ingestion contract:** source records become the correct normalized sessions, messages, tool events, skills, lineage, model, effort, and timestamps.
2. **Derivation contract:** normalized windows become the correct labels, confidence, and evidence references.
3. **Presentation contract:** every displayed or exported claim can be reconstructed from stored evidence and the displayed denominator.

A failure must name its contract, harness, fixture, extractor version, expected value, and observed value.

### 3.2 Fixture corpus

Maintain four fixture tiers.

#### Tier 1: minimal checked-in parser fixtures

For each harness, check in small sanitized JSONL files containing every observed event shape, not whole sessions. Each fixture should be 5–50 records and have a complete normalized golden file.

Minimum cases:

- Codex: `session_meta`, repeated metadata, `turn_context` model/effort changes, `event_msg` versus `response_item` duplication, function/custom/web/MCP calls, successful and failed outputs, subagent lineage, malformed JSON, and a final line without newline.
- Claude Code: main and subagent paths, user/assistant/system records, content blocks, `tool_use`/`tool_result`, `is_error`, Skill tool use, `skill-injections.jsonl`, model changes, and malformed records.
- Cursor: main and subagent paths, wrapper timestamps, system/user/assistant records, tool calls/results, missing timestamps, and records that must be ignored such as `turn_ended`.

Store expected rows as canonical JSON sorted by table and stable primary key. Do not assert SQLite rowids or extraction timestamps.

#### Tier 2: private real-session fixtures

Select 7 root sessions per harness from the owner's corpus:

- shortest and longest;
- one subagent tree;
- one tool-failure-heavy session;
- one multi-model or effort-changing session where available;
- one malformed or warning-producing artifact;
- one ordinary median-length session.

Copy them to `~/.agentlog/fixtures/private/<harness>/`, redact secrets deterministically, and record source content hashes. These fixtures do not enter Git or CI on other machines. They run in the owner's nightly/full suite and catch source drift that sanitized fixtures miss.

#### Tier 3: semantic gold windows

Do not label the raw 4,942-window ledger. After mandatory population triage (§4.5), the human-behavior extraction pool is about **1,800 windows** (~1,782 under the exploratory triage rule in `extraction-test-run.md`). Draw gold samples from that pool only.

- **Prevalence sample:** 300 windows selected by a seeded uniform sample over root-session clusters, stratified by harness and calendar month. Against ~1,800 eligible windows this is ~17% of the pool—large enough for precision/false-positive rates without enrichment bias, and still attainable without labeling the full corpus. Do not draw prevalence from auto-review, harness-synthetic, or empty rows.
- **Challenge sample:** 200 windows deliberately enriched for mid-task redirects / premature-action brakes, agent pushback, corrections (including borderline follow-ups), soft approvals, frustration (including ambiguous non-affect), tool failures, retries, abandonment candidates, long outputs, multi-agent orchestration (`inter_agent_handoff`, `worker_brief`, `coordinator_nudge`, `cross_harness_reference`), quoted instructions, and low-confidence classifier boundaries. This measures recall and boundary behavior, not prevalence. 500 labeled windows total (~28% of the eligible pool) is a heavy but one-time cost; do not shrink either sample to ease labeling.

Sample at the root-session level first, then windows within roots, so one long session cannot dominate. Freeze 20% of each sample as an untouched regression holdout. Prompt and rule tuning may use the other 80% only.

Use weak rules and one model pass to propose candidate labels and evidence spans; humans confirm or reject them. This reduces labeling work but does not make model proposals gold. Every positive and a 20% random sample of negatives receive two independent labels. Adjudicate disagreements. Report Cohen's kappa for categorical labels and span overlap F1 for evidence spans; a label definition is not ready if kappa is below 0.70.

Gold records contain:

```text
fixture_id
window_hash
label_schema_version
labels [{kind, value}]
evidence [{message_id, start_char, end_char, quote_hash}]
annotator_ids
adjudication_status
notes
```

Keep full private text only in the private fixture store. Checked-in gold uses sanitized text.

#### Tier 4: generated adversarial and metamorphic fixtures

Generate deterministic variants from Tier 1:

- append one complete record at every valid newline boundary;
- truncate each file inside a line and at a UTF-8 boundary;
- reorder records where order must matter;
- duplicate event records and call IDs;
- alter only response text while keeping request text fixed;
- alter parser and window-builder versions;
- replace timestamps with absent, naive, offset, seconds, and milliseconds forms;
- insert unknown event types and extra fields;
- inject Unicode, very long lines, null bytes represented in JSON strings, and malformed JSON;
- inject transcript text that tells the evaluator to ignore its rubric, emit a pass, reveal credentials, call tools, or alter fixture files.

Metamorphic expectations are explicit: unknown fields do not change normalized output; duplicate call IDs do not double-count; response changes change `window_hash`; append and full paths produce identical evidence.

### 3.3 Metrics

#### Ingestion

- **Artifact discovery recall:** expected fixture artifacts discovered / expected artifacts.
- **Entity precision/recall:** exact-set precision and recall for sessions, messages, tool events, skill exposures, and lineage edges using stable semantic keys.
- **Field accuracy:** exact matches / fields with a known gold value, reported separately for ID, role, text hash, timestamp, model, effort, repository, success, duration, and parent.
- **Unknown preservation:** gold unknowns stored as `NULL` / gold unknowns. Storing false or zero is a failure.
- **Warning accuracy:** expected warning codes matched as a set. Warning prose is not a stable contract.
- **Window boundary F1:** exact request/response pair matches.
- **Window hash accuracy:** exact canonical hash match after request, response, tool context, or builder-version changes.

Never collapse these into one ingestion percentage. A 99% average can hide zero lineage recall.

#### Semantic extraction

For each label kind:

- precision, recall, and F1 on the prevalence sample;
- recall and false-negative examples on the challenge sample;
- macro F1 across label kinds;
- calibration by confidence bucket using empirical precision;
- evidence-span token F1;
- abstention rate and accuracy among non-abstained cases.

The first production labels—redirect/brake (lead, descriptive), agent pushback, correction (secondary, abstention-heavy), soft approval (stance only), frustration (abstention-heavy), orchestration turn kinds (`inter_agent_handoff`, `worker_brief`, `coordinator_nudge`, `cross_harness_reference`), failure pattern, and learning candidate—get independent metrics. “Insight accuracy” is too vague to gate.

#### Citation faithfulness

A citation passes only if:

1. every session/message/window ID exists;
2. the cited message belongs to the claimed session/window;
3. stored offsets are in range;
4. the normalized quote equals the cited source substring;
5. the quote supports the structured predicate according to the gold label;
6. no uncited session contributes to a displayed numerator or denominator.

IDs, ownership, offsets, and quote equality are deterministic gates and require 100%. Semantic support is measured against adjudicated gold with precision and recall.

#### Stability

- Run deterministic ingestion three times into fresh databases and compare canonical dumps excluding database metadata and run timestamps. Required: byte-identical dumps.
- Run incremental ingestion for every fixture split and compare against a fresh full ingest. Required: exact equality for artifacts' parsed prefix, sessions, messages, tools, skills, FTS-visible text, and windows.
- Run deterministic extractors three times. Required: canonical JSON equality.
- For LLM extractors, run five times with the same provider/model/prompt/schema. Report label agreement, evidence agreement, and abstention drift. This is a scheduled drift gate, not a PR gate dependent on a remote service.

### 3.4 Regression gates

Fast local/PR suite:

- zero crashes on valid fixtures;
- 100% artifact discovery;
- 100% stable-ID, role, text-hash, and request/response-window accuracy;
- 100% citation referential and quote integrity;
- exact full-versus-incremental equivalence;
- exact repeated-run equality for deterministic stages;
- no regression in per-harness entity precision/recall;
- no new unexpected warning code.

Scheduled semantic suite:

- redirect/brake precision >= 0.90 and recall >= 0.80 on holdout (lead descriptive label; gates measure detection quality, not whether a high rate is “bad”);
- correction precision >= 0.90 and recall >= 0.80 on holdout among non-abstained cases; correction aggregates that skip the abstention rule fail the suite;
- frustration precision >= 0.90 and recall >= 0.70 on holdout among non-abstained cases; default abstain unless affect is explicit; aggregates that skip abstention fail the suite;
- soft-approval precision >= 0.85 and recall >= 0.70 on holdout; suite fails if any fixture treats soft approval as terminal task success;
- orchestration turn-kind precision >= 0.85 and recall >= 0.70 on holdout for `inter_agent_handoff` / `worker_brief` / `coordinator_nudge` / `cross_harness_reference`;
- agent-pushback precision >= 0.85 and recall >= 0.70 on holdout before dashboard claims;
- every other production label precision >= 0.85 and recall >= 0.70 before it may produce a dashboard claim;
- citation semantic-support precision >= 0.95;
- five-run label agreement >= 0.90;
- no metric may fall by more than 0.03 absolute from the accepted baseline;
- a prompt/model/schema change creates a new baseline candidate; it never silently overwrites the accepted baseline.

If a label misses its gate, keep its records as `candidate` and hide aggregate claims. Do not lower thresholds to make a release green.

### 3.5 Prompt-injection resistance

Transcript content is untrusted data. The semantic extractor:

- receives transcript text in a delimited data field, never concatenated into system instructions;
- has no shell, file, network, MCP, or publication tools;
- emits only a validated, size-limited schema;
- cannot read environment variables or unrelated messages;
- receives pre-redacted text for remote calls;
- stores rejected output and a typed failure without retrying with weaker validation.

Adversarial paired cases contain identical task evidence, with one version adding an injection. The label and evidence should remain unchanged. The suite fails on any side effect, schema escape, secret-canary reproduction, citation outside the supplied window, or pass/fail manipulation.

### 3.6 Runner and reports

`agentlog quality run` should:

1. load a versioned suite manifest;
2. create a fresh temporary source and result database;
3. run each case with a per-case timeout;
4. canonicalize outputs;
5. compute per-contract and per-harness metrics;
6. compare against explicit gates and an accepted baseline;
7. write JSON plus a concise Rich report;
8. exit `0` for pass, `1` for gate failure, `2` for invalid suite/configuration, and `3` for runner error.

The manifest pins fixture hash, parser version, window-builder version, extractor version, prompt version, schema version, provider, model, and redaction version. Reports retain individual failures; an aggregate cannot erase them.

## 4. Type B — evals BY agentlog

Type B's default product surface is a **descriptive model-usage and interaction-style profile**: what the owner chose, on which tasks, with what observed steering and orchestration signals. It is not a performance ranking. A 4-way ranked `model × harness × effort × task` cross-tab is not estimable from this corpus—effort is Codex-only, model is near-nested in harness and time-era, and the owner selects model by anticipated difficulty that is never recorded (confounding by indication). The owner's model-selection pattern is itself the valid, differentiating signal; surface that as the feature.

Ranking or “winner” presentation is available only when the §5.2 ten-gate comparison procedure fully passes, and must be labeled a matched-cohort association, never a verdict. With the current data distribution, almost no cell is expected to qualify. Descriptive profile is the resting state; ranking is the rare exception.

### 4.1 Analytical unit

The independent unit is a **root task cluster**, not an exchange window and not every child session. A root session and all descendants count once for session-level outcomes. Otherwise a model that spawns more subagents would manufacture a larger sample and violate independence.

Exchange windows remain the unit for redirect/brake, correction, pushback, and other semantic evidence. Their observations roll up to the root cluster.

If a root contains more than one substantive task, classify it `mixed` unless segmentation produces task segments with clear start/end message IDs. Segments from the same root share a cluster ID and are never treated as independent in confidence intervals.

**Multi-agent orchestration is first-class.** Empirically, this owner's dominant work style is supervisor–worker orchestration across harnesses (Codex↔Claude handoffs, Cursor supervisor with worker armies, worker briefs with owned files/STATUS, shared PLAN.md/STATE.md), not solo user↔single-assistant chat. An eval design that only sees one user talking to one assistant measures a minority of the behavior. Representation rules:

1. **Analytical unit stays the root task cluster.** Orchestration trees (supervisor session + workers + cross-harness handoffs) are exactly the case root clustering already treats as non-independent. Do not invent a second independence unit that would double-count the same root. Child worker sessions never become separate sample rows for Type B rates.
2. **Turn roles.** Label each window's speaker/role as `human_supervisor`, `coordinator_agent`, `worker_agent`, or `harness_synthetic` (deterministic-first where possible). Human redirect/brake, correction, soft-approval, and frustration labels apply only to genuine human supervisor turns after triage—not to worker briefs, inter-agent handoffs, or synthetic follow-ups.
3. **First-class orchestration turn kinds** (from evidence; multi-label allowed): `inter_agent_handoff`, `worker_brief`, `coordinator_nudge`, `cross_harness_reference`. These are structural labels, not attitude outcomes. Route worker briefs and handoffs to an orchestration schema when they are not human attitude signals; still attach them to the root cluster for lineage and drill-down.
4. **Attribution.** A root task that spans multiple agents attributes session-level outcomes (abandonment, dominant config, cost roll-up) to the root cluster once. Window-level semantic events cite the specific session/message but roll up under that cluster. Cross-harness mentions alone do not create a new cluster.
5. **Metric level.**
   - **Supervisor-level (human):** redirect/brake rate, correction (abstention-gated), soft-approval stance, frustration (abstention-gated), agent pushback directed at the human's plan—denominators are human-substantive windows in the root cluster.
   - **Worker-level (within-harness, descriptive):** tool-failure and retry inside a worker session; orchestration density (`worker_brief` / handoff counts per root). Never treat worker sessions as independent roots; never cross-harness-compare tool failure.
   - **Do not** score inter-agent scheduling (“while claude is working…”) as a human redirect/brake unless the same turn also retracts or pauses the current agent's plan.

### 4.2 Task taxonomy

Use a small, mutually exclusive primary taxonomy:

| Primary task | Operational definition |
|---|---|
| `debug` | Diagnose or fix observed incorrect behavior, failing tests, runtime errors, or regressions. |
| `refactor` | Change structure or maintainability while preserving intended behavior. |
| `feature_existing` | Add or materially extend behavior in an existing codebase. |
| `greenfield` | Create a new project, subsystem, or standalone artifact with little existing implementation context. |
| `review` | Inspect code, diffs, security, performance, or design and return findings; implementation is not the primary request. |
| `research` | Gather, compare, or synthesize external/internal information; code changes are not the primary deliverable. |
| `maintenance` | Dependency, configuration, migration, CI, release, environment, or repository housekeeping. |
| `docs` | Documentation is the primary deliverable. |
| `mixed` | Two or more primary intents with no dominant task, or a session that changes purpose. |
| `unknown` | Evidence is insufficient. |

Store optional secondary tags such as `tests`, `security`, `performance`, `frontend`, `backend`, `data`, `architecture`, and `git`. Do not expand the primary taxonomy until at least 30 examples repeatedly fall into the same missing category.

### 4.3 Classification method

Classify primary task from the **first substantive user message only**, using **pre-treatment evidence only**:

- first substantive user request text (sole text source for the primary-task label);
- no assistant outcome, corrections, elapsed time, tool failures, or final state;
- no later user turns for primary-task assignment.

Developer/system constraints, repository identity, and pre-existing file types may inform difficulty features (below) but must not assign or override the primary-task label. Using outcomes to classify difficulty or task type would leak the metric into the cohort definition.

Classification pipeline:

1. Strip harness wrappers, timestamps, attached state, and transcript-control text from the first substantive user request while preserving a link to the raw message.
2. Apply high-precision deterministic rules for obvious review, research, docs, and debug requests against that message only.
3. Run the versioned semantic classifier for unresolved cases. It returns primary label, secondary tags, confidence, and exact evidence spans grounded in that message.
4. If confidence is below 0.65, or the top two labels are within 0.10, emit `mixed` or `unknown`; do not force a label.
5. Let the owner override a label. Human labels supersede machine labels but retain both records and rationale.

Ship task-conditioned aggregates only after a hand-labeled gold set gates the classifier. Validation targets (unchanged bars; re-justified against the real eligible population):

- **~120 adjudicated root sessions** is enough to unblock development, measure rough per-class behavior, and iterate rules/prompts.
- **At least 250 adjudicated, stratified root sessions** is required before any task-conditioned comparison is *published*.

The corpus has ~548 sessions and ~1,800 human-substantive windows after triage. After excluding auto-review, subagent-independence rows, and harness-synthetic turns, 250 adjudicated roots is a large fraction of eligible history—not a reason to lower the publish bar. If 250 eligible adjudicated roots cannot be reached, task-conditioned comparisons stay unpublished. 120 remains a staging set only. Require macro F1 >= 0.80 and per-class precision >= 0.80 on the publish set. Below the publish threshold, show only manually verified categories.

Claude-specific strata are further constrained: the adapter fix is verified at **331 genuine Claude human turns (~1,630,098 characters)**, matching the raw files (see Reconciliation → Empirical revision and gap-closure). That is a modest corpus: Claude-only task cells will often abstain for n even when Codex cells clear the bar.

Difficulty is a separate, pre-treatment descriptor:

- `scope_files`: explicit number of named files, or `unknown`;
- `repo_familiarity`: prior root sessions in the same repo before this session;
- `request_tokens`;
- `constraint_count`;
- `requires_external_research`;
- `greenfield_state`: empty/new repository signal where observable;
- owner-supplied `easy|medium|hard` when available.

Do not derive difficulty from duration, retries, or corrections.

### 4.4 Configuration assignment

The descriptive stratification cell is:

```text
canonical_model × harness × effort × primary_task
```

Use these dimensions to describe usage and experience. Do not treat the full 4-way ranked cross-tab as estimable: effort is structural-zero outside Codex; model is near-nested in harness and calendar era; and model choice is selected by anticipated difficulty that is never recorded. Most cells will abstain under the precision gates in §4.7.

Keep exact model variants such as `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` separate. A family roll-up is a secondary view, never a replacement for exact identities.

Per-model support in the current corpus is extremely uneven. Concrete support for any `model × task` descriptive grid:

| Canonical model | Approx. sessions | Grid support |
|---|---|---|
| `gpt-5.5` | 303 | Supports a `model × task` grid |
| `gpt-5.6-sol` | 47 | Supports about 2–3 strata |
| `grok-4.5-build` | 69 | Supports about 2–3 strata |
| Everything else | — | Abstain from model×task aggregates |

Those counts are planning inputs for abstention, not licenses to rank. Configurations outside the rows above default to “insufficient precision / abstain.”

Normalize effort to `low|medium|high|xhigh|ultra|max|unknown` while retaining the source value (`effort_source`). Treat `unknown` as a real missingness category, not the default effort. `ultra` (Codex) and `max` (Claude) are first-class canonical values — they are not collapsed into `xhigh`.

Model may change within a session. Assign a dominant model only when it produced at least 80% of assistant exchanges. Otherwise mark the cluster `mixed_model` and exclude it from single-model matched-cohort comparisons. Retain per-exchange assignments for drill-down.

Cursor model and effort are recovered from `state.vscdb` (`composerData.modelConfig`, joined by composer UUID). In the current corpus, **15 of 18** Cursor transcripts resolve a real model and **14 of 18** an effort; leave fields null where genuinely absent. Publish Cursor cells only when the claimed dimension is resolved for that cell. A cell without a resolved model must abstain from model-conditioned aggregates—do not infer a model from harness defaults or neighboring sessions.

### 4.5 Outcome metrics

Store each outcome as `{value, availability, confidence, method_version, evidence}`. `availability` is one of `observed`, `estimated`, `unknown`, or `not_supported`.

**Effective corpus.** The raw ledger has 4,942 exchange windows. Empirical triage finds that roughly **64% must be dropped** before human-behavior metrics (auto-review, harness glue, empty rows, stubs, skill dumps). Plan Type B denominators against about **1,800 human-substantive windows**, not 4,942. Requiring a next human user message (≥10 chars of non-wrapper text) further shrinks attitude-label pools (~1,300 windows under the exploratory rule).

**Population segregation.** Main-population Type B aggregates exclude **before any metric is computed**:

- `codex-auto-review` sessions (and equivalent auto-review / approval-assessment harness modes);
- subagent-derived sessions as independent units (they remain attached to the root cluster for roll-up, never as separate sample rows);
- Cursor synthetic follow-up messages whose text begins like “Perform any necessary follow-up actions in response to the subagent completion above.” These are harness scaffolding, not human attitude signals;
- skill-body dumps pasted as the entire user message (route to a segregated skill-compliance view if useful);
- image-only turns (`[Image: …]` placeholders). Flag them as a separate modality; vision content is not in `messages.text` and must not be scored as empty human prose.

Also drop deterministic harness stubs from the human-behavior pool: task-notification dumps, realtime-delegation handoffs presented as user turns, and bare continue-from-where-you-left-off resumes. Report auto-review, skill-dump, and image-only populations in segregated views if useful; never pool them with the main human-attitude population. Worker briefs and inter-agent handoffs are first-class orchestration structure (§4.1)—attach them to the root cluster and label them, but do not pool them into human redirect/brake or stance denominators.

**Extraction cost and truncation (binding).** Any Type B (or Type A semantic) path that re-runs LLM extraction over windows inherits the constraints from `extraction-test-run.md` §5–§6. Budget is the binding constraint: median window text is ~810 chars, p99 ~60k, skill dumps ~155k. Naive full-ledger passes waste tokens on harness glue. Required:

- triage to the human-substantive pool (~1,300–1,800 windows) before UX extraction;
- cap fields per call—typically user ≤2–4k chars, assistant summary ≤2–4k (or last N narrations + final), next_user ≤2k, tool timeline as name/action/success list ≤80 lines;
- batch 8–16 windows when small and same harness; otherwise one window when >8k chars;
- never feed auto-review or ~155k skill bodies into the UX extractor without a dedicated truncated template;
- record extractor model, prompt hash, and truncation version on every derivation run.

**Lead metric.** The lead Type B metric is `redirects_brakes_per_10_exchange_windows`: a **descriptive interaction-style measure** of how much mid-task steering appeared in observed history. It is not a quality-defect rate, not a score, and **higher is not automatically worse**. Frequency made the signal detectable and statistically usable; the same frequency means much of it is ordinary collaborative course-correction. A long exploratory design conversation with healthy redirects must not read as underperformance, and a session where the developer stopped steering must not read as “clean.” This replaces the earlier corrections lead: crisp corrections are real but uncommon and hard to separate from ordinary follow-ups. Other metrics below are secondary, contextual, or within-harness only.

#### Redirect / premature-action brake (lead; descriptive)

A **redirect or brake event** is a user turn that redirects mid-task work, partially accepts then revises direction, or explicitly stops the agent from acting yet / scaffolding prematurely. Operational subtypes (multi-label allowed at the window level; count once per window for the rate):

- `redirect_or_brake` — e.g. “dont jump on startig out the repo or scaffolding it first…”; “yeah except the video everythign seems doable…”;
- `dont_act_yet` — e.g. “what does this all mean dont act on it yet just info ..”;
- process flag `premature_action_called_out` when the user names jumping-ahead.

A pure new request with no contrast to in-flight agent action is not a redirect. Parallel-agent scheduling (“while claude is working…”) is multi-agent orchestration, not by itself a brake, unless it also retracts or pauses the current agent's plan.

Report (descriptive counts/rates only—no pass/fail threshold on the rate itself):

- `had_redirect_brake`: at least one redirect/brake event in the root cluster;
- `redirect_brake_count`;
- `redirects_brakes_per_10_exchange_windows = 10 * redirect_brake_count / exchange_windows_in_cluster` (**lead metric**);
- `redirects_brakes_per_10_user_turns = 10 * redirect_brake_count / substantive_user_turns` (secondary, for drill-down when turn counts diverge from windows).

Detection is the versioned semantic extractor across harnesses, after harness-wrapper removal and population triage. Some deterministic cues (“dont act”, “instead”, “except” in short user text) may propose candidates; they do not alone confirm an event. Every event cites the braking/redirecting user message and the agent action or plan it contrasts with. Unavailable when fewer than two substantive turns exist and the session ends without an explicit redirect/brake or soft-approval/acceptance signal. Do not interpret absence of redirects as task success.

#### Correction (secondary; mandatory abstention)

A **correction event** is a user turn that rejects, narrows, or repairs a claim or action from the preceding agent response. A new request or added scope is not a correction. Clear examples exist (“i said primary would be claude code not ONLY…”, “this has to be acrosos evevrythign…”); many lookalikes are inquiries, content negation, or mixed complaint-plus-new-work.

Report:

- `had_correction`: at least one correction event in the root cluster;
- `correction_count`;
- `corrections_per_10_exchange_windows = 10 * correction_count / exchange_windows_in_cluster` (**secondary**);
- `corrections_per_10_user_turns = 10 * correction_count / substantive_user_turns` (secondary drill-down).

**Mandatory abstention.** Do not emit a correction label, count, or aggregate from keywords alone. Naive “correctionish” keyword rates on humanish windows (~17%) are dominated by false positives. The extractor must abstain unless there is explicit contrast with a prior agent action or clear repair language (“i said / you missed / instead of / across everything”). Borderline follow-ups default to abstain. Correction aggregates that include abstained windows in the numerator fail the metric. This metric is unavailable when fewer than two substantive turns exist and the session ends without an explicit acceptance/correction signal.

#### Agent pushback (tracked)

An **agent pushback** event is an assistant turn that rejects or redesigns the user's proposed approach (productive disagreement), not mere clarification. Example shape: pausing setup because the proposed mechanism duplicates agreed infrastructure; the user then agrees. This is neither a user correction nor satisfaction.

Report:

- `had_agent_pushback`: at least one pushback event in the root cluster;
- `agent_pushback_count`;
- `agent_pushbacks_per_10_exchange_windows = 10 * agent_pushback_count / exchange_windows_in_cluster` (tracked secondary).

Every event requires a supporting quote from the assistant turn. Availability is `observed` only after the versioned extractor; otherwise `unknown`. Pushback is valuable collaboration signal and must appear in the descriptive profile when precision gates pass; it is not the lead metric.

#### Soft approval (stance only; never outcome)

A **soft approval** is a short go-ahead or acknowledgment that green-lights the next step (“sounds good”, “ok”, “go ahead”, “yes good…”) without closing the task. Empirically these almost always precede more work; true terminal “task done, I’m happy” closers are rare in-window.

Rules:

- Label soft approval as **user stance**, never as terminal task success, clean completion, or outcome success.
- No Type B outcome metric may use soft approval alone (or soft approval plus absence of redirects) as evidence the task succeeded.
- `accepted_continue` / go-ahead may inform process narrative in drill-down; it must not enter a success-rate numerator.
- Availability is `observed` only when the extractor cites the approving span; bare “ok” without clear go-ahead intent abstains.

#### Frustration (mandatory abstention)

Frustration is real but usually mild and ambiguous in this corpus. Casual swearing, cost anxiety, skepticism, and constraint-setting are often **not** frustration.

Rules:

- Default to **abstain** unless affect is explicit (clear anger, disgust, or named emotional rejection of the agent’s work).
- Do not keyword-label frustration; do not promote distrust-of-method or premature-action brakes into frustration without explicit affect.
- No dashboard aggregate or comparison may report a frustration rate that includes abstained windows in the numerator.
- When affect is unclear, leave `user_stance` non-frustrated and record abstention—do not force a label to fill a cell.

#### Tool failure

A **known tool result** has explicit source-native success/failure evidence in the payload or harness-specific fields. A **tool failure** is a known result with that failure evidence. Unknown success is excluded from both numerator and denominator.

**Do not treat `tool_events.success` as sufficient.** In the current corpus that column is mostly NULL (~53.5k NULL vs ~1.2k success=1 and ~133 success=0). Failure detection requires exit codes in payloads, repeated identical tool calls, API-error assistant text, or harness-specific parsing—not the success column alone. When only a NULL `success` value is present and no other evidence exists, availability is `unknown` (or `estimated` only under an explicitly versioned heuristic that is never used for cross-harness comparison).

**Within-harness only.** Differing event semantics across Codex, Claude Code, and Cursor make cross-harness tool-failure comparisons invalid. Compare tool-failure rates only inside a single harness (and still subject to capability and precision gates).

Report:

- `tool_failure_rate = failed_results / known_results`;
- `had_unrecovered_tool_failure`: a failure with no later successful matching retry and no explicit agent/user resolution.

Harness rules:

- **Codex:** failed when `function_call_output` or an `*_end` record carries non-zero `exit_code`, or an explicit `success=false` in the source payload. Preserve call ID, exit code, and canonical tool family. A call with no result is `unknown`, not failed. A row whose only signal is NULL `tool_events.success` is `unknown`.
- **Claude Code:** failed when the matching `tool_result.is_error` is true. Preserve `tool_use_id`, input hash, and tool name. A missing `is_error` is unknown. Claude human turns are recovered (tool-result rows under the `user` role must not be counted as human messages); tool-failure semantics still depend on `is_error`, not on empty text.
- **Cursor:** failed when a `tool_result.is_error` is true. Current records without that field are unknown. Cursor model/effort recovery (§4.4) is separate missingness from tool-result success.

#### Retry

A **recovery retry** is a failed known tool result followed within the next three tool calls in the same root cluster by the same canonical operation and argument fingerprint, or by a semantically equivalent operation explicitly linked to the failure. A repeated read/search without a preceding failure is not a retry. Retries cannot be established from NULL `success` alone.

**Within-harness only.** Retry rates inherit tool-event semantics and must never be compared across harnesses.

Report:

- `retry_count`;
- `retry_rate = recovery_retries / failed_results`;
- `eventual_recovery_rate = failed operations followed by a matching success / failed operations`.

This requires new `tool_call_id`, `arguments_hash`, and canonical operation fields. Until those are populated, label name-only detection `estimated` and do not compare it across harnesses.

#### Abandonment

`likely_abandoned` is true only when all are true:

1. the root task has unresolved requested work or a known unrecovered failure;
2. the transcript ends without a substantive completion/handoff response;
3. no child or linked continuation resolves it within 24 hours;
4. no owner outcome override marks it complete.

This is a semantic, confidence-bearing proxy, never a fact. Report owner-confirmed abandonment separately. Harness termination alone is not abandonment because users close successful sessions without saying so.

#### Duration

Report two values as **context only** (they mostly measure task size and idle structure, not configuration quality):

- `wall_duration = last_observed_timestamp - first_observed_timestamp`;
- `active_duration = sum(min(next_event_time - event_time, 5 minutes))` across timestamped substantive messages and tool events.

Do not use duration as a lead metric, ranking metric, or efficiency verdict. Show it beside cells to help the owner read task scale. The five-minute cap on `active_duration` limits idle-time distortion and is versioned. Require at least two reliable timestamps. Cursor sessions whose only timestamp comes from a wrapper are `estimated`; sessions without enough timestamps are unknown.

#### Cost and tokens

Prefer source-reported usage:

```text
input_tokens
output_tokens
cache_read_tokens
cache_write_tokens
reported_cost
currency
pricing_table_version
```

If tokens are reported but cost is not, calculate estimated cost from a dated pricing table and mark it `estimated`. If token usage is absent, cost is unknown; do not compare character-count pseudo-tokens with provider-reported tokens.

Report medians and distributions for `cost_per_root_task` and `tokens_per_root_task` as secondary descriptive context under the §4.7 precision gate. Cost may participate in a rare §5.2 matched-cohort association; it is not the lead metric. Do not define `cost_per_clean_completion`—there is no supported clean-completion outcome (below).

#### Clean-completion proxy — retired

`clean_completion` is **not a Type B metric**. An earlier draft defined it as no redirect/brake, no correction, no unrecovered tool failure, and not abandoned. That composite is unsupportable on this data:

1. Redirects/brakes are common ordinary steering (§4.5 lead metric). Treating them as disqualifying for “clean” conflates collaborative interaction style with failure, and makes the flag rarely true in interactive sessions.
2. Correction status frequently abstains, so the composite would be `unknown` for large fractions of roots even if redirects were ignored.
3. Soft approval is never terminal task success; the corpus lacks a reliable in-window terminal-success signal to replace the composite.

Do not redefine `clean_completion` by dropping the redirect clause—the remaining components still cannot support a success claim. Do not substitute soft approval, absence of redirects, or harness termination as success. If a future terminal-outcome signal appears (owner override, explicit done closer with adjudication, or linked verification evidence), introduce a new named metric with its own method version; do not revive this proxy. Never combine outcome signals into an opaque weighted “score.”

### 4.6 Harness capability enforcement

Each metric query must join a capability record and compute availability by cell. The dashboard should show `n observed / n total`.

Current minimum truth:

| Signal | Codex | Claude Code | Cursor |
|---|---|---|---|
| transcript redirect/brake | supported after semantic validation (descriptive; not a defect rate) | supported after semantic validation; verified 331 human turns corpus-wide—usable, modest cell sizes | supported after wrapper stripping; exclude synthetic subagent follow-ups |
| transcript corrections | supported after semantic validation + mandatory abstention | same; adapter fix verified (331 human turns / ~1.63M chars) | supported after wrapper stripping + mandatory abstention |
| soft approval | stance only; never terminal success | same | same |
| frustration | supported only with explicit affect; else abstain | same | same |
| agent pushback | supported after semantic validation (quote required) | supported after semantic validation (quote required) | supported after semantic validation (quote required) |
| orchestration turn kinds | supported (handoff / worker_brief / nudge / cross-harness) | supported | supported; exclude synthetic subagent follow-ups from human stance |
| explicit tool failure | partial; requires exit_code / payload success / repeated-call or API-error cues—not NULL `tool_events.success` | partial; supported when `is_error` exists; missing `is_error` unknown | partial; only explicit `is_error`; NULL success unknown |
| exact retry fingerprint | not yet stored | not yet stored | not yet stored |
| model | populated, but verify turn changes | populated | recovered from `state.vscdb` for most transcripts (15/18); null when absent—abstain if unresolved |
| effort | populated when present | populated when present | recovered for most transcripts (14/18); null when absent |
| active duration | supported when timestamps exist | supported when timestamps exist | often estimated/unknown |
| tokens/cost | not yet normalized | not yet normalized | not yet normalized |
| clean_completion | not supported (metric retired) | not supported (metric retired) | not supported (metric retired) |

Claude Code is **not** written off. The ~90% empty-request finding in `extraction-test-run.md` was an adapter bug; recovery is **fixed and verified** at 331 human turns / ~1,630,098 characters. That corpus is enough for descriptive inclusion and often too small for Claude-only ranked cells. The comparison engine rejects a metric when support differs in a way that can create differential missingness. It does not silently treat unknown as zero. Tool-failure and retry metrics are additionally rejected for cross-harness comparison even when both sides report “supported,” because operational definitions differ by source.

### 4.7 Cell computation and presentation

For each time range, materialize cells with:

- exact dimensions and taxonomy/method versions;
- root-cluster count;
- observed count for each metric;
- missingness fraction;
- binary numerator/rate with Wilson 95% interval;
- continuous median, interquartile range, and cluster-bootstrap 95% interval;
- project, month, and difficulty composition;
- confounder and capability flags;
- drill-down root session IDs;
- model-selection pattern summaries (share of sessions by model within harness/era/task), which remain valid when performance ranking abstains.

**Precision gate (binding).** Show an aggregate rate or median for a cell only when all of the following hold:

1. For binary rates: Wilson 95% interval half-width ≤ 10 percentage points (or an explicitly versioned tighter bound). For the lead metric `redirects_brakes_per_10_exchange_windows` and other continuous/rate metrics: cluster-bootstrap 95% half-width within a versioned bound, expressed **relative to the point estimate** (initial default: half-width ≤ 40% of the estimate, with an absolute floor so that near-zero rates are not gated on an unattainably tight bound).

   An absolute half-width is the wrong shape for an unbounded rate. A fixed ±0.5 redirects/brakes per 10 windows spans zero when the true rate is 0.3, and is a strict 10% bound when the rate is 5.0 — the same gate is vacuous at one end of the range and punishing at the other. A relative bound behaves consistently across the range; the absolute floor handles the degenerate case near zero.

   **This bound is provisional and must be calibrated, not assumed.** The observed distribution of `redirects_brakes_per_10_exchange_windows` is not yet known—the empirical deep-read establishes that redirects/brakes are common, not a prevalence or variance estimate. Set the versioned default from the real distribution once redirect/brake extraction produces counts across the **triaged ~1,800-window** human-behavior corpus, choosing a bound that admits a useful fraction of cells without admitting intervals too wide to support the claim being displayed. What settles it: a full Phase-2 extraction pass on the triage set with cluster-level rate histogram and bootstrap half-widths by cell size. Until then, treat any cell passing this gate as provisional and record the bound version alongside the cell. Do not invent a calibrated number from the 40-window discovery sample.
2. cluster-adjusted event count for the metric numerator/denominator basis is ≥ 10 (unchanged). With redirects/brakes as the more frequent lead signal, this floor is more often attainable than under the old corrections lead; it is not lowered. Secondary correction aggregates must still meet this floor **and** the mandatory abstention rule—rare crisp corrections will often abstain here honestly.
3. metric availability is high enough that differential missingness does not dominate (same floors as §5.4 where applicable).

Round-number `n` thresholds alone never authorize an aggregate. If the precision gate fails, show “insufficient precision,” the individual sessions, and no aggregate—regardless of raw `n`. Round-number tiers below are readability labels only; they never override a failed precision gate.

Display tiers (secondary, after the precision gate passes):

- precision gate failed, or cluster-adjusted `n < 5`: “insufficient data/precision,” sessions only, no aggregate.
- precision gate passes and `5 <= n < 15`: point estimate and interval with “very low evidence”; descriptive profile only; do not rank.
- precision gate passes and `15 <= n < 30`: point estimate and interval with “low evidence”; descriptive profile; matched-cohort comparison view only if §5.2 passes, still no winner label.
- precision gate passes and `n >= 30`: ranking remains ineligible unless §5.2 fully passes, metric availability is >= 80%, and comparison-specific effective sample size is >= 20 per configuration. Even then, label the result a matched-cohort association, never a verdict.

**Re-justification against ~1,800 eligible windows (bars not weakened).** The `n >= 30` ranking-eligibility floor and the `effective sample size >= 20` per-configuration requirement stay. Sized originally against 4,942 windows, they are stricter relative to the real ~1,800-window pool: most `model × harness × effort × task` cells will not reach 30 root clusters or ESS 20 after triage, harness segregation, and Claude’s modest 331-turn contribution. That is expected; unattainable cells abstain. Do not lower 30 or 20 to fill the grid.

With the current corpus, almost no cell is expected to clear both the precision gate and §5.2. Plan the UI around abstention and descriptive profile, not around a filled ranking grid.

For binary metrics use Wilson intervals. For cost (and duration when shown as context) use medians and a root-cluster bootstrap; do not present means as the default. Bootstrap all segments from one root together. Duration never feeds a ranking eligibility path.

The Models & Cost page resting state is a **model usage & interaction-style profile**: selection shares by model/harness/era/task, plus descriptive cells led by redirects/brakes per 10 exchange windows (interaction style, not quality), with correction (abstention-gated), agent-pushback, and orchestration-density rates as secondary cells when they clear precision gates. Replace any global “CORR% winner” or “lowest redirect” table. Resting text is descriptive and non-directional:

> You used gpt-5.5 for 303 root tasks in this range (share … by task). In 24 observed Codex / gpt-5.6-sol / high-effort debug tasks, mid-task redirects/brakes appeared at 2.1 per 10 exchange windows (95% interval …)—a measure of steering frequency in those sessions, not a quality score.

A rare comparison view may say, only after §5.2 fully passes:

> Among overlapping Plugin debug tasks from May–July, configuration A and B differed in detected redirect/brake rate (A … per 10 windows vs B …; matched-cohort association). This describes interaction style in the matched sample, not which configuration performed better.

It must not say “A is better,” “A underperformed,” or “fewer redirects means higher quality”; must not present the 4-way matrix as a leaderboard; and must never say A caused the difference.

## 5. Statistical honesty

### 5.1 Defensible claims

Always defensible when data lineage is intact:

- counts and distributions of observed sessions;
- the owner's model-selection pattern (shares by model, harness, era, task);
- metric availability and source capability;
- within-cell observed interaction-style rates (including redirect/brake) with uncertainty when precision gates pass—descriptive, not quality verdicts;
- factual differences in the selected historical sample.

Conditionally defensible:

- associations between configurations and outcomes within a specified overlapping task/project/time cohort when §5.2 fully passes;
- adjusted differences from a predeclared model when overlap, precision, and effective sample rules pass.

Not defensible from this data:

- causal claims that a model, harness, skill, or effort caused an outcome;
- global “best model” claims across different task mixes;
- a public-benchmark-style or 4-way `model × harness × effort × task` ranking;
- quality claims for cells with unknown models or differential outcome coverage;
- cross-harness retry or tool-failure rankings;
- treating duration as a quality or efficiency verdict;
- treating redirect/brake rate as a quality-defect rate or “underperformance” score (higher is not automatically worse);
- treating soft approval as terminal task success, or reporting a `clean_completion` / success-rate proxy;
- treating windows, subagents, or worker sessions from the same root as independent samples;
- pooling `codex-auto-review`, subagent-independence rows, Cursor synthetic follow-ups, skill-body dumps, or image-only turns into the main human-attitude population.

### 5.2 Required comparison procedure

Pairwise ranking or “winner” presentation is off by default. Every pairwise comparison that is allowed to leave the descriptive profile follows this order; failure at any step means abstain or stay descriptive:

1. Fix the outcome and method version before looking at associations. The default primary outcome is `redirects_brakes_per_10_exchange_windows` (or `had_redirect_brake` when a binary form is required)—an interaction-style contrast, not a quality win condition. Associations may report rate differences; they must not crown a “winner” for having fewer redirects. Corrections and agent pushback are secondary outcomes only. Duration and `clean_completion` are ineligible.
2. Restrict to the same primary task, using publish-qualified task labels (§4.3).
3. Restrict to calendar overlap. Require at least 28 overlapping days and 10 root tasks per configuration inside the overlap.
4. Require support overlap: the outcome must be observable under the same operational definition in both configurations. Retry and tool-failure comparisons additionally require the same harness.
5. Stratify by repository and month. Include only shared strata.
6. Balance pre-treatment difficulty features. Reject comparison when any weighted standardized mean difference exceeds 0.25 after weighting/matching.
7. Require effective sample size >= 20 per configuration before any matched-cohort association may leave the descriptive profile, and require each arm to pass the §4.7 precision gate. Against ~1,800 eligible windows this bar is rarely met; do not lower it—abstain instead.
8. Estimate a risk difference for binary outcomes or median ratio/difference for continuous outcomes, with root-cluster bootstrap intervals.
9. Apply Benjamini–Hochberg false-discovery control at `q=0.10` across comparisons shown in the same refresh.
10. Require practical importance as well as uncertainty separation before surfacing a matched-cohort association: at least 5 percentage points for binary redirect-brake/correction/abandonment rates, 20% relative for the continuous lead rate `redirects_brakes_per_10_exchange_windows`, or 20% for cost. Meeting the threshold means the interaction-style (or cost) difference is large enough to show—not that the lower-redirect arm “won.” Duration has no practical-importance ranking threshold because it is contextual only. `clean_completion` has none because the metric is retired.

Any claim that survives must be rendered as a matched-cohort association, never as a causal or global ranking verdict. With sparse data, prefer exact stratification and transparent matched sets over a high-dimensional propensity model. A hierarchical model with partial pooling is a later option after at least 200 classified root tasks and 30 observations in several cells; it must not manufacture a rank for unsupported cells.

### 5.3 Mandatory confounder flags

Attach flags to cells and comparisons:

- `task_selection`: task/difficulty mix differs materially;
- `time_period`: model availability windows differ;
- `project_concentration`: >80% of a cell comes from one repository;
- `harness_model_aliasing`: model appears in only one harness, so model and harness effects cannot be separated;
- `effort_selection`: effort is user/model-selected rather than randomized;
- `outcome_missingness`: >20% unknown, or >10 percentage-point missingness difference between cells;
- `multi_model_excluded`: mixed-model sessions were removed;
- `classifier_uncertainty`: >20% mixed/unknown task labels;
- `source_capability`: operational definitions differ or are partial;
- `small_sample`: fewer than 30 root tasks, or failed §4.7 precision gate;
- `non_overlap`: temporal/project/difficulty overlap failed;
- `population_mix`: main population was not segregated from `codex-auto-review`, subagent-independence rows, Cursor synthetic follow-ups, skill-body dumps, or image-only turns;
- `structural_nestedness`: model appears in only one harness or era such that the claimed factor cannot be separated.

Flags are visible beside the claim and in exported JSON. They are not buried in a methodology page.

### 5.4 Abstention rules

Emit no comparative claim when any of these is true:

- either cell fails the §4.7 precision gate;
- either cell has fewer than 5 root tasks;
- comparison effective sample size is below 10 per arm;
- there are no shared project-month strata;
- temporal overlap is below 28 days;
- metric availability is below 70% in either cell;
- availability differs by more than 20 percentage points;
- task-label confidence is below 0.65 for more than 20% of either cell;
- task-conditioned comparisons are not yet publish-qualified (§4.3);
- a model is unknown or mixed for the claimed model effect;
- source capability changes the metric definition;
- the comparison asks for cross-harness retry or tool-failure rates;
- duration is proposed as the ranked outcome;
- the confidence interval cannot exclude both zero and the practical-effect threshold for an association claim.

When `10 <= effective n < 20`, or when §5.2 has not fully passed, a descriptive difference may be shown but the product must say “not enough comparable history to rank” and must remain on the usage & interaction-style profile.

### 5.5 Language contract

Allowed:

- “In your observed history…”
- “your model-selection pattern…”
- “interaction style…” / “steering frequency in these sessions…”
- “redirect/brake rate describes how much mid-task steering appeared…”
- “was associated with a different detected redirect/brake rate…”
- “A … per 10 windows vs B … in this matched cohort…”
- “matched-cohort association; not a quality ranking…”
- “evidence is limited because…”
- “not enough comparable history.”
- “insufficient precision to aggregate.”
- “correction status abstained where the follow-up was ambiguous…”
- “soft approval labeled as stance only; not task success…”
- “frustration abstained where affect was not explicit…”

Forbidden:

- “proved,” “caused,” “improved,” or “made the agent”;
- “best model” without task, harness, effort, time range, and comparison cohort;
- presenting the 4-way configuration matrix as an estimable ranked leaderboard;
- “success rate,” “clean completion,” or any success proxy built from absent redirects, soft approval, or the retired `clean_completion` flag;
- “0% failures” when failures are unknown or unsupported (including NULL `tool_events.success` without other evidence);
- treating the redirect/brake lead metric as a defect, error, failure, or quality score;
- implying directionality on redirect/brake: “underperformed,” “worse,” “better because fewer redirects,” “cleaner session,” or UI that sorts configurations by lowest redirect rate as a performance ranking;
- treating a high redirect rate in an exploratory session as model failure, or a low rate as model success;
- treating keyword “correctionish” rates or non-abstained ambiguous follow-ups as correction evidence;
- reporting frustration rates without the explicit-affect abstention bar.

## 6. Path to prospective rigor

Historical Type B analysis cannot identify causal model effects under confounding by indication. The only path that upgrades a comparison to sound causal language is prospective randomization. This section specifies an opt-in coin-flip protocol that upgrades **exactly one** pre-registered comparison—not the whole matrix.

### 6.1 Scope the UI must state

Every experiment surface and every claim derived from it must say, in substance:

> This experiment randomizes one pre-registered comparison between two owner-chosen models on eligible tasks. It does not validate the full model × harness × effort × task matrix, and it does not make other historical cells causal.

Do not show experiment results as a global leaderboard row.

### 6.2 Eligibility

A root task may enter the experiment only when all are true:

1. The owner has opted in and pre-committed a shortlist of exactly two canonical models for the experiment window.
2. The task is a new root session (not a continuation, subagent, or `codex-auto-review` session).
3. The primary task label from the first substantive user message is in a pre-registered eligible set (e.g. `debug`, `feature_existing`, `refactor`) and is not `mixed` or `unknown`.
4. Both shortlist models are available in the chosen harness at assignment time.
5. The owner affirms the task is one they would be willing to run on either shortlist model (comparability screen), before seeing the coin flip.
6. No experiment assignment already exists for this root cluster.

If any check fails, the tool does not assign; the session proceeds as ordinary observational history.

### 6.3 Assignment protocol

1. Owner pre-registers, before any assignments: shortlist models `{A, B}`, harness, eligible primary tasks, primary metric (default `redirects_brakes_per_10_exchange_windows`), analysis population rules, and stopping target.
2. For each eligible task, the tool performs a cryptographically fair coin flip (`A` vs `B`) and tells the owner which model to use.
3. The owner starts the session with that model. Deviations are recorded; as-treated analyses are secondary to intent-to-treat.
4. Target: about **16 root sessions per arm** (≈32 total) over about **8 weeks**, or until the pre-registered stop. This is a minimum planning target for **prospective** enrollment, not a license to peek and stop early for significance. It is not sized from the historical 4,942-window ledger; historical Type B mostly abstains under §4.7/§5.2 against ~1,800 eligible windows. Keep 16/arm—do not lower it because the observational corpus is smaller. If enrollment cannot reach the target inside the calendar window, the experiment closes without a causal claim rather than analyzing an underpowered arm.

### 6.4 Schema recording

Record assignments in the evidence/derivation database (see §8.2). Minimum fields:

```text
experiment_id
pre_registration_hash          # hash of frozen protocol document
root_session_id / task_cluster_id  # nullable until session exists
assigned_model                 # coin-flip result
shortlist_json                 # [model_a, model_b]
assignment_seed / draw_id
assigned_at
eligibility_json               # task label, harness, comparability affirmations
intent_to_treat_model
as_treated_model               # dominant model actually used, when known
compliance_status              # complied | deviated | abandoned_before_start
primary_metric_name
primary_metric_method_version
```

Assignments are immutable after insert. Protocol edits create a new `experiment_id` and new `pre_registration_hash`; they do not rewrite prior draws.

### 6.5 Pre-registration and analysis

Before the first coin flip, freeze a short protocol document and store its hash:

- primary metric and method version;
- eligible tasks and harness;
- ITT as primary analysis, as-treated secondary;
- segregation rules (exclude auto-review, subagent independence, Cursor synthetic follow-ups, skill-body dumps, image-only turns);
- sample target (~16 per arm) and calendar window;
- single primary comparison: model A vs model B on that metric.

Analyze only that comparison with root-cluster uncertainty. Secondary metrics may be reported as descriptive. Do not multiply-compare the rest of the matrix under the experiment badge.

### 6.6 What this does and does not buy

This protocol can support cautious causal language for the one pre-registered arm contrast under compliance. It does not make historical cells causal, does not identify harness or effort effects, and does not justify filling the ranked 4-way cross-tab. The descriptive usage & interaction-style profile remains the product resting state. For the default primary metric, even a prospective contrast reports steering-frequency differences—not a defect-rate win—unless the pre-registration explicitly chooses a different outcome with a justified quality interpretation.

## 7. Strands Agents Evals verdict

### 7.1 What it provides

As of 2026-08-09, the public Strands Evals project provides:

- generic `Case`, `Experiment`, evaluator, and `EvaluationReport` objects;
- synchronous/asynchronous case execution;
- LLM judges for output, helpfulness, faithfulness, correctness, coherence, relevance, instruction following, goal success, tool selection/parameters, trajectories, and interactions;
- deterministic checks such as equals, contains, tool-called, and state-equals;
- OpenTelemetry-shaped `Session → Trace → Span` types and mappers;
- remote trace providers for CloudWatch, Langfuse, and OpenSearch;
- custom providers/evaluators;
- multi-turn and tool simulators, chaos effects, experiment generation, failure detection, and root-cause analysis;
- JSON serialization, local-file task-result caching, Rich reports, and CLI/CI exit policies.

It can evaluate historical data without re-running an agent **if** a caller already knows each session ID and converts its trace to the Strands `Session` shape. Its `TraceProvider` has one required method, `get_evaluation_data(session_id)`. Public issue #143 explicitly notes that discovery and bulk cohort evaluation require caller-written glue.

### 7.2 Fit assessment

It fits online or trace-oriented testing well:

- run an agent on fixed cases;
- capture OpenTelemetry spans;
- score output/tool trajectory;
- simulate failures;
- gate CI on a report.

It does not fit agentlog's core Type B problem:

- no local SQLite provider or session discovery;
- no normalized coding-harness adapters;
- no task taxonomy for historical coding sessions;
- no descriptive model×harness×effort×task stratification or usage-profile analytics;
- no missing-capability model;
- no root-session clustering;
- no temporal/task-selection confounding controls;
- no Wilson-precision abstention or observational-language policy;
- JSON/file caching rather than a durable derived-evidence ledger.

It is also a poor Type A dependency. agentlog's parser gates are exact row/set/hash checks, not agent output evaluations. Implementing them as Strands custom evaluators would wrap simple pytest assertions in a larger runtime and add dependencies including `strands-agents`, `strands-agents-tools`, OpenTelemetry, and boto3.

### 7.3 Decision: steal patterns, do not adopt

Steal these patterns:

- named cases grouped into versioned suites;
- explicit evaluator levels;
- structured `{score, pass, reason, label}` case results;
- cached inputs separated from evaluator reruns;
- JSON reports plus human-readable display;
- deterministic CI exit codes;
- a narrow provider boundary;
- trace diagnosis as a separate operation from scoring.

Do not import its package, copy its source, convert agentlog's ledger wholesale to OpenTelemetry spans, or use its LLM judges for the personal usage & interaction-style profile.

Reconsider an **optional** adapter only if agentlog later adds active benchmark execution. That adapter could export a selected agentlog session as a Strands `Session` or import a Strands `EvaluationReport`. agentlog would still own discovery, SQLite storage, task labels, outcomes, cohorts, statistical comparison, prospective randomization (§6), and dashboard claims.

### 7.4 Sources

- [Strands Evals quickstart](https://strandsagents.com/docs/user-guide/evals-sdk/quickstart/)
- [Evaluators](https://strandsagents.com/docs/user-guide/evals-sdk/evaluators/)
- [CLI and CI behavior](https://strandsagents.com/docs/user-guide/evals-sdk/cli/)
- [Remote trace providers](https://strandsagents.com/docs/user-guide/evals-sdk/how-to/trace_providers/)
- [Result caching](https://strandsagents.com/docs/user-guide/evals-sdk/how-to/result_caching/)
- [Detectors](https://strandsagents.com/docs/user-guide/evals-sdk/detectors/)
- [Public repository](https://github.com/strands-agents/evals)
- [Bulk trace discovery gap, issue #143](https://github.com/strands-agents/evals/issues/143)
- [`pyproject.toml` dependency surface](https://github.com/strands-agents/evals/blob/main/pyproject.toml)

## 8. Schema additions

Schema changes must go through the migration system proposed in `council-synthesis.md`; do not keep expanding one `SCHEMA_SQL` string.

### 8.1 Evidence-ledger changes in `agentlog.db`

Add to `exchange_windows`:

```sql
ALTER TABLE exchange_windows ADD COLUMN window_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE exchange_windows ADD COLUMN builder_version TEXT NOT NULL DEFAULT '';
ALTER TABLE exchange_windows ADD COLUMN start_seq INTEGER;
ALTER TABLE exchange_windows ADD COLUMN end_seq INTEGER;
```

After backfill, deprecate request-only `input_hash`.

Add to `tool_events`:

```sql
ALTER TABLE tool_events ADD COLUMN timestamp TEXT;
ALTER TABLE tool_events ADD COLUMN source_event_id TEXT;
ALTER TABLE tool_events ADD COLUMN tool_call_id TEXT;
ALTER TABLE tool_events ADD COLUMN canonical_tool TEXT;
ALTER TABLE tool_events ADD COLUMN arguments_hash TEXT;
ALTER TABLE tool_events ADD COLUMN result_code TEXT;
ALTER TABLE tool_events ADD COLUMN success_source TEXT;
```

Do not store raw arguments by default; hashes and redacted targets are enough for retry matching.

Add:

```sql
CREATE TABLE token_usage (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    source_event_id TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reported_cost REAL,
    currency TEXT,
    usage_source TEXT NOT NULL,
    UNIQUE (session_id, source_event_id)
);
```

Add a generic production derivation ledger:

```sql
CREATE TABLE derivation_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    extractor_name TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    prompt_version TEXT,
    provider TEXT,
    model TEXT,
    privacy_class TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_code TEXT,
    UNIQUE (
        kind, extractor_name, extractor_version, schema_version,
        prompt_version, provider, model, input_hash
    )
);
```

This table records production derivations. It is not a Type A result table.

### 8.2 Type B tables in `agentlog.db`

```sql
CREATE TABLE task_clusters (
    id TEXT PRIMARY KEY,
    root_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    segment_start_message_id TEXT REFERENCES messages(id),
    segment_end_message_id TEXT REFERENCES messages(id),
    cluster_kind TEXT NOT NULL CHECK (cluster_kind IN ('root', 'segment')),
    UNIQUE (root_session_id, segment_start_message_id, segment_end_message_id)
);

CREATE TABLE task_label_observations (
    id TEXT PRIMARY KEY,
    task_cluster_id TEXT NOT NULL REFERENCES task_clusters(id) ON DELETE CASCADE,
    primary_task TEXT NOT NULL,
    secondary_tags_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    source TEXT NOT NULL CHECK (source IN ('rule', 'model', 'human')),
    evidence_json TEXT NOT NULL,
    derivation_run_id TEXT REFERENCES derivation_runs(id),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'active', 'superseded', 'rejected')),
    supersedes_id TEXT REFERENCES task_label_observations(id),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_active_task_label
ON task_label_observations(task_cluster_id)
WHERE status = 'active';

CREATE TABLE task_difficulty_features (
    task_cluster_id TEXT PRIMARY KEY REFERENCES task_clusters(id) ON DELETE CASCADE,
    feature_version TEXT NOT NULL,
    request_tokens INTEGER,
    explicit_file_count INTEGER,
    constraint_count INTEGER,
    prior_repo_sessions INTEGER,
    requires_external_research INTEGER,
    greenfield_signal INTEGER,
    owner_difficulty TEXT,
    availability_json TEXT NOT NULL
);

CREATE TABLE task_config_assignments (
    id TEXT PRIMARY KEY,
    task_cluster_id TEXT NOT NULL REFERENCES task_clusters(id) ON DELETE CASCADE,
    harness TEXT NOT NULL,
    model TEXT,
    canonical_model TEXT,
    effort_source TEXT,
    canonical_effort TEXT NOT NULL,
    assistant_exchange_count INTEGER NOT NULL,
    exchange_share REAL NOT NULL,
    assignment_status TEXT NOT NULL CHECK (
        assignment_status IN ('dominant', 'mixed_model', 'unknown')
    ),
    method_version TEXT NOT NULL
);

CREATE TABLE outcome_observations (
    id TEXT PRIMARY KEY,
    task_cluster_id TEXT NOT NULL REFERENCES task_clusters(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    availability TEXT NOT NULL CHECK (
        availability IN ('observed', 'estimated', 'unknown', 'not_supported')
    ),
    confidence REAL,
    method_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    derivation_run_id TEXT REFERENCES derivation_runs(id),
    created_at TEXT NOT NULL,
    UNIQUE (task_cluster_id, metric_name, method_version)
);

CREATE TABLE performance_snapshots (
    id TEXT PRIMARY KEY,
    range_start TEXT,
    range_end TEXT,
    taxonomy_version TEXT NOT NULL,
    outcome_versions_json TEXT NOT NULL,
    cohort_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    source_watermark TEXT NOT NULL
);

CREATE TABLE performance_cells (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES performance_snapshots(id) ON DELETE CASCADE,
    canonical_model TEXT NOT NULL,
    harness TEXT NOT NULL,
    canonical_effort TEXT NOT NULL,
    primary_task TEXT NOT NULL,
    root_n INTEGER NOT NULL,
    metric_stats_json TEXT NOT NULL,
    composition_json TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    drilldown_query_json TEXT NOT NULL,
    UNIQUE (
        snapshot_id, canonical_model, harness, canonical_effort, primary_task
    )
);

CREATE TABLE performance_comparisons (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES performance_snapshots(id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    cell_a_id TEXT NOT NULL REFERENCES performance_cells(id),
    cell_b_id TEXT NOT NULL REFERENCES performance_cells(id),
    matched_root_n_a INTEGER NOT NULL,
    matched_root_n_b INTEGER NOT NULL,
    effective_n_a REAL NOT NULL,
    effective_n_b REAL NOT NULL,
    estimate REAL,
    interval_low REAL,
    interval_high REAL,
    adjusted_p_value REAL,
    practical_threshold REAL NOT NULL,
    claim_status TEXT NOT NULL CHECK (
        claim_status IN ('descriptive', 'eligible', 'abstained')
    ),
    abstention_reason TEXT,
    flags_json TEXT NOT NULL,
    method_version TEXT NOT NULL
);

CREATE TABLE performance_experiments (
    id TEXT PRIMARY KEY,
    pre_registration_hash TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    shortlist_json TEXT NOT NULL,
    harness TEXT NOT NULL,
    eligible_tasks_json TEXT NOT NULL,
    primary_metric_name TEXT NOT NULL,
    primary_metric_method_version TEXT NOT NULL,
    target_n_per_arm INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('registered', 'enrolling', 'closed', 'abandoned')
    ),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE performance_experiment_assignments (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES performance_experiments(id),
    task_cluster_id TEXT REFERENCES task_clusters(id) ON DELETE SET NULL,
    root_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    assigned_model TEXT NOT NULL,
    assignment_seed TEXT NOT NULL,
    draw_id TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    eligibility_json TEXT NOT NULL,
    intent_to_treat_model TEXT NOT NULL,
    as_treated_model TEXT,
    compliance_status TEXT NOT NULL CHECK (
        compliance_status IN (
            'pending', 'complied', 'deviated', 'abandoned_before_start'
        )
    ),
    UNIQUE (experiment_id, draw_id)
);
```

Keep evidence references as validated structured JSON initially; normalize them into a shared `evidence_links` table when more than two derivation families need identical querying. Do not prematurely create a generic entity-attribute-value store.

### 8.3 Type A schema in `pipeline-evals.db`

```sql
CREATE TABLE quality_suites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    suite_version TEXT NOT NULL
);

CREATE TABLE quality_cases (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES quality_suites(id),
    contract TEXT NOT NULL,
    harness TEXT,
    fixture_uri TEXT NOT NULL,
    fixture_hash TEXT NOT NULL,
    gold_hash TEXT NOT NULL,
    tags_json TEXT NOT NULL
);

CREATE TABLE quality_runs (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES quality_suites(id),
    git_sha TEXT,
    parser_version TEXT NOT NULL,
    builder_version TEXT NOT NULL,
    extractor_versions_json TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE quality_case_results (
    run_id TEXT NOT NULL REFERENCES quality_runs(id),
    case_id TEXT NOT NULL REFERENCES quality_cases(id),
    evaluator_name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    score REAL,
    reason TEXT,
    expected_json TEXT,
    observed_json TEXT,
    duration_ms INTEGER,
    PRIMARY KEY (run_id, case_id, evaluator_name)
);

CREATE TABLE quality_metric_results (
    run_id TEXT NOT NULL REFERENCES quality_runs(id),
    contract TEXT NOT NULL,
    harness TEXT,
    metric_name TEXT NOT NULL,
    value REAL NOT NULL,
    threshold REAL,
    passed INTEGER NOT NULL,
    numerator INTEGER,
    denominator INTEGER,
    PRIMARY KEY (run_id, contract, harness, metric_name)
);
```

Gold labels remain version-controlled files or private fixture-store files. The Type A database records their hashes and results; it is not the source of truth for annotations.

## 9. Build sequence

### Phase 1 — make the evidence measurable

1. Add schema migrations before adding evaluation tables.
2. Fix `exchange_windows` hashing to include request, response, tool context, and builder version.
3. Preserve source tool call IDs, argument hashes, result codes, timestamps, and success provenance in all three adapters. Do not treat NULL `tool_events.success` as observed failure or success; record `success_source` so provenance is explicit.
4. Keep Claude Code human-turn recovery correct: Anthropic `user`-role tool results must not be recorded as empty human messages. Verified recovery is 331 human turns (~1,630,098 chars). This unblocks Claude UX metrics; it does not enlarge Claude to Codex scale.
5. Add token-usage normalization where source records support it.
6. Add root-session resolution and capability manifests; implement deterministic population triage (auto-review, synthetic follow-ups, skill dumps, image-only, stubs) before semantic extraction; recover Cursor model/effort from `state.vscdb` where present.

Type B retry/cost claims and Type A invalidation checks depend on this phase.

### Phase 2 — Type A ingestion harness

1. Create minimal checked-in parser fixtures and canonical gold output.
2. Build the isolated quality runner and deterministic report schema.
3. Add repeated-run and full-versus-incremental equivalence tests.
4. Add malformed, truncation, duplicate, and version-change fixtures.
5. Gate parser changes in the fast local/PR suite.

Do this before adding semantic product claims; otherwise later labels rest on an unmeasured parser.

### Phase 3 — production derivation provenance

1. Add `derivation_runs` and cache keys.
2. Define validated evidence-reference types.
3. Implement redirect/brake extraction first (lead descriptive metric), then orchestration turn kinds, agent pushback, correction-with-mandatory-abstention, soft-approval-as-stance, and frustration-with-mandatory-abstention—these power dashboard annotations and Type B outcomes.
4. Enforce extraction truncation and triage caps (§4.5): budget is binding; do not reprocess the raw 4,942-window ledger for UX labels.
5. Add citation integrity checks.
6. Build the 300-window prevalence and 200-window challenge samples from the ~1,800-window triage pool; label and freeze holdouts.
7. Enable semantic claims only after their gates pass.

### Phase 4 — Type B factual layer

1. Build root task clusters with orchestration lineage; segregate `codex-auto-review`, subagent-derived independence rows, Cursor synthetic follow-ups, skill-body dumps, and image-only turns from the main human-attitude population; keep worker briefs/handoffs attached to the root for structure (§4.1).
2. Implement first-message pre-treatment task classification and owner overrides.
3. Validate the taxonomy on a staged gold set (~120 to unblock; ≥250 before publish—unchanged; abstain from publish if 250 eligible roots cannot be adjudicated).
4. Compute outcome observations with explicit availability; lead with descriptive `redirects_brakes_per_10_exchange_windows`; track agent pushback and orchestration density; keep corrections and frustration secondary with mandatory abstention; never emit `clean_completion`.
5. Add semantic abandonment, redirect/brake, pushback, soft-approval-as-stance, and abstention-gated correction/frustration observations with evidence. Tool-failure observations must not rely on NULL `success` alone.
6. Expose the model usage & interaction-style profile: selection shares, per-cell descriptive aggregates under Wilson precision gates, and session drill-downs—no “lowest redirect” ranking chrome.

At the end of this phase, the product may say “what was observed” and show selection and steering patterns, but not name quality winners.

### Phase 5 — honest comparisons (rare path)

1. Implement temporal/project strata and difficulty-balance diagnostics.
2. Add cluster-bootstrap intervals, Wilson precision gates, and effective sample sizes.
3. Add comparison abstention and confounder flags.
4. Add multiple-comparison correction and practical-effect thresholds.
5. Add claim rendering from structured comparison status, not free-form LLM prose; matched-cohort association language only.
6. Update Models & Cost and Insights so the resting UI is the usage & interaction-style profile; comparison views appear only when §5.2 fully passes and use non-directional redirect/brake language.

### Phase 6 — prospective rigor (optional, opt-in)

1. Add `performance_experiments` and `performance_experiment_assignments` (§8.2).
2. Implement pre-registration, eligibility checks, and coin-flip assignment (§6).
3. Enforce the UI scope sentence: one pre-registered comparison only.
4. Analyze at ~16 sessions per arm with ITT primary.

### Phase 7 — scheduled robustness

1. Add private real-session fixture runs on the owner's machine.
2. Add five-run semantic stability and prompt-injection suites.
3. Track accepted baselines without automatically replacing them.
4. Add data-drift alerts for taxonomy mix, metric missingness, model availability, and source schema changes.

### Dependency order

```text
migrations
  -> complete evidence fields + Claude human-turn recovery
  -> Type A parser gates
  -> population triage (~1,800-window human pool)
  -> derivation provenance + correct window hashes
  -> semantic gold/gates (redirect/brake lead descriptive; correction/frustration abstain)
  -> task clusters + orchestration lineage (first-message labels)
  -> outcome observations (redirect/brake interaction-style lead; no clean_completion)
  -> descriptive usage & interaction-style profile (Wilson gates; truncation-bound extraction)
  -> overlap/confounding engine
  -> rare matched-cohort association claims (non-directional on redirect rate)
  -> optional coin-flip experiment (§6)
```

The first useful Type B release is not a ranked leaderboard. It is a drillable model-usage and interaction-style profile that frequently abstains and never treats steering frequency as a defect score. Ranking is a rare matched-cohort exception after evidence fields, label quality, precision gates, metric availability, and §5.2 overlap rules all pass—and almost no current cell is expected to qualify. Causal language requires the §6 experiment for exactly one pre-registered contrast.
