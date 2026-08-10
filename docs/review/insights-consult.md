# Insights / Learnings Consult — Senior AI-Dev Bar

**Date:** 2026-08-09
**Mode:** adversarial read + document only (no product refactor)
**Provenance:** replacement Grok review after the requested Fable review was interrupted by the API limit; not a completed Fable review.
**Sources:** live `TestClient` against `~/.agentlog/agentlog.db`, `src/agentlog/analysis/claims/`, `/api/proposals` + `/api/claims`, `docs/research/eval-architecture.md`, `docs/status-and-gaps.md`, `docs/review/backend-review.md` (H8), extraction verification under `.research/`.

## Verdict (short)

The proposals board is **honest-underwhelming, not secretly rich**. With ~80 root sessions, triage showing only **1,509 / ~3,600** windows as `substantive`, labels authorized by a **synthetic fixture audit** (13 live adjudications), and theme matches in the single digits for most instruction claims, shipping AGENTS.md / skill-removal suggestions as “learnings” would fail a senior bar. Keep the advisory surface; **do not** treat current pending proposals as ready instruction edits. Cut noise claim kinds from the review queue; mine stronger **deterministic** signal before investing in more LLM theme proposals.

## Corpus facts (live, this machine)

| Fact | Value |
|---|---|
| Root sessions (non-`skills:`) | **80–81** |
| `exchange_windows` | **3,702** (triage runs saw ~3,592–3,600) |
| Deterministic triage `request_kind_counts.substantive` | **1,509** |
| UX-eligible route count (same run) | **~1,866** (includes wrapped/non-substantive UX routes) |
| `ux_observations` | **1,065** |
| Adjudications | **13** (`labels_validated` gate wants ≥20) |
| Pending proposals | **9** (8× unused-skill archive, 1× usage profile) |
| Instruction proposals | regenerated themes mostly **superseded**; only `scope_narrow` is `support=ok` (11 sessions / 12 windows) |

Triage meta from derivation run `2a3ff974…`: `substantive=1509`, `auto_review=1059`, `worker_brief=450`, plus drops/stubs — the owner’s “only 1509/3601 substantive” framing is correct.

## What the board actually proposes

1. **Unused skills (deterministic)** — majority of pending cards. Zero exposures across ~80 root sessions for inventory entries under `agents` / `codex` / `cursor`. Rationale correctly says this does **not** prove delete-worthiness. Still weak as a product surface: many are intentional rare tools, rename-join misses, or coordinator-only skills. Deprecation banners are safer than deletion, but **eight near-identical cards** is review fatigue without ranking (age of skill, last file mtime, whether name appears in AGENTS.md, harness bundling).
2. **Usage profile note (deterministic)** — one solid card (`solprobe`, 17 sessions) writing under `~/.agentlog/context/`. This is the right target (not AGENTS.md). Descriptive, non-causal. Keep.
3. **Recurring instructions (LLM-gated)** — live claims:
   - `scope_narrow`: ok, 11 sessions / 12 windows
   - `verify_before_done`: insufficient, 8 / 9
   - `dont_act_yet_brake`, `spawn_workers`: abstain (1 session / 2 windows each)
   Prior superseded proposals that cited larger `n_sessions` for brake/spawn were **stale relative to current derive** (sample sizes in superseded rows do not match current claim rows). Do not resurrect them from the board history.

Keyword themes over LLM-labeled correction/brake windows are a thin slice of the 1,509 substantive turns. They are not a curriculum.

## Label trust (why the bar fails)

- **Audit gate was synthetic.** `docs/status-and-gaps.md` §P0.5: authorizing metrics come from `synthetic_labeled_audit` / fixture gold, not adjudicated live corpus. Publishing instruction text from those labels is circular.
- **Adjudication incomplete.** 13 rows vs ≥20 for even the soft `labels_validated` flag in `claims/extract.py`; eval-architecture wants κ≥0.70 and hundreds of gold windows before trusting Type A semantics.
- **H8 tool-context defect (backend-review).** Loader historically compared tool `seq` to message `seq`, dropping tool timelines (`tool_count=0`) for linked Codex tools. Code now joins via `message_id` (`window_context.py`). **Any UX labels produced before that fix remain potentially tool-blind** — stance/turn_kind on tool-heavy windows may be wrong. Until those windows are re-extracted under a new `content_hash` / builder version that includes tool context, treat LLM-derived claims as contaminated.
- **Correction theme rate** (~11.5% of 374 labeled sessions) is a descriptive label frequency, not a quality score. It must not drive AGENTS.md.

## Honest-underwhelming vs unmined signal

**Honest:** the pipeline abstains correctly on thin themes; usage notes avoid prompt bloat; API is advisory-only (no apply endpoints); `does_not_prove` text is present.

**Still unmined (higher senior-bar ROI than more skill-archive cards):**

| Signal | Why it clears the bar more easily |
|---|---|
| Attention inbox / stuck / needs-input (deterministic) | Already computed; actionable without LLM gold |
| Config ledger correspondence after *owner* edits | Observes what the owner actually kept |
| Skill exposure **with outcome joins** (existing `analysis/skills.py`) | Exposure≠effectiveness, but gated rates beat “0 exposures ⇒ archive” |
| Tool failure patterns with real names + success coverage | `tool_failure_pattern` exists but is sparse/`success` mostly NULL — fix evidence fields first |
| Brief / handoff quality (length, missing citations) | Library exists; not on the proposals board |
| Repo-scoped verify/scope themes **after** gold adjudication | Only then consider AGENTS.md diffs |

Thin instruction proposals are not “leaving a goldmine on the table”; they are correctly starved. The unmined value is mostly **deterministic ops signal**, not more regex themes.

## Claim kinds — cut vs add

### Cut or demote (stop promoting to pending proposals)

| Kind | Action |
|---|---|
| `skill_unused` | **Demote:** keep as claim/inventory; do **not** auto-emit up to 8 archive proposals. Require extra gates (age, non-bundled, zero mentions in configs, owner allowlist) or fold into a single “unused inventory” digest. |
| `recurring_instruction` for abstain/insufficient | **Cut from proposals** (already gated for `ok` only — keep that; do not loosen). |
| `correction_theme` | **Never propose** config edits from it; optional Insights descriptive chip only. |
| `tool_failure_pattern` (current) | **Hold proposals** until `tool_events` success/canonical fields exist; claiming failures on sparse `success=0` is misleading. |

### Keep

| Kind | Why |
|---|---|
| `harness_model_usage` → context file | Deterministic, scoped, non-prescriptive. |
| `skill_exposure` (claim only) | Useful inventory; do not jump to removal. |

### Add (only with gates)

| Kind | Gate before proposals |
|---|---|
| `instruction_gap_vs_config` | Theme ok **and** adjudicated spans **and** config inventory shows absence **and** H8-clean re-extract |
| `repeated_attention_state` | Deterministic attention transitions across ≥10 root sessions |
| `verify_command_stated_but_unrun` | Deterministic: user asked for test/typecheck; no matching tool success before “done” (needs better tool identity) |
| `skill_helped_or_hurt` (observational) | Reuse skills analysis rates with Wilson/abstention language — never causal |

## AGENTS.md / skills bar (owner question)

Learning suggestions clear a senior AI-dev bar only if they are:

1. **Evidence-backed** at root-session denominators the eval doc respects (≥10 for a finding; themes today mostly fail).
2. **Not label-laundered** through synthetic audit + unadjudicated UX rows.
3. **Actionable and non-redundant** with existing global/project AGENTS.md (scope routing already skips some overlaps — good).
4. **Preferential to deterministic misses** (verify-not-run, attention loops) over “user matched wait/don’t act.”

**Today: fail.** Safe product stance: Insights = descriptive observatory + usage/context notes; skill archive and instruction diffs stay behind explicit “experimental / low trust” labeling until gold + H8 re-extract.

## Recommended sequencing (doc only)

1. Finish Type A gold / adjudication; re-extract UX after window hash includes tool context (eval-architecture §8.1 + H8).
2. Collapse unused-skill spam into one digest claim.
3. Surface deterministic attention + skill effectiveness already in the library.
4. Revisit `recurring_instruction` proposals only for themes with ok support **and** adjudicated agreement.

## Non-goals confirmed

- No auto-apply / no reintroduction of proposal apply HTTP paths (board note is correct).
- No restyle of Overview/proposals in this consult.
- MCP remains stdio/DB — orthogonal to learnings quality.

## Follow-up (2026-08-09 execution)

**Done:** `archive_skill` / unused-skill → DEPRECATED banner proposals are hard-gated off (`EMIT_UNUSED_SKILL_ARCHIVE_PROPOSALS = False`); `skill_unused` claims abstain with `exposure coverage insufficient`. Pending spam superseded as system status; usage-profile notes kept.

**Still open — exposure detection gap:** Cursor and Codex often invoke installed skills without writing the `skill_exposures` rows agentlog joins on. Do **not** re-enable archive proposals until a detector sees those invocations (or an explicit owner allowlist + stronger non-use evidence). Inventory claims alone remain fine.
