# Labeling pivot — interview gold over 100-window enums

**Status:** active strategy (2026-08-09). The 100-window constrained adjudication pack is **paused as a blocker**. Owner time goes to interview + spot-check, not schema junk.

## What ingest already solved

Deterministic triage / adapters already own:

- **Who spoke:** `messages.role`, `authored_by_agent`, harness envelopes, Cursor/Claude/Codex unwrap
- **Tool plumbing:** `is_tool_plumbing`, tool-event join (post-H8)
- **Route / request_kind:** `window_det_classifications` (`ux` + `substantive` vs worker/auto-review/drop)
- **Structural provenance:** session lineage, content hashes, model identity

Humans should **not** hand-label “human vs agent” as the primary task. If a window is agent/harness, that is an ingest or queue-filter bug, not an adjudication taxonomy.

## What still needs LLM semantics

**Primary (board / coach):** instruction follow vs miss, repeated concrete asks,
skill activation evidence, paste-ready `AGENTS.md` / skill suggestions.
See `docs/product-north-star.md` — **not** a feelings board.

**Secondary (internal mining only, do not lead the UI):**

| Signal | Intent |
|--------|--------|
| `repeat_request` | Same ask restated; “again / I said / still” (delivery / compliance) |
| `redirect` | Stop, wait, change direction, brake |
| `unmet_ask` | Agent failed to deliver what was asked |
| `pushback` | Skeptical challenge to plan/claim |
| `satisfaction` | Explicit acceptance / “good / works” |
| `frustration` | **Do not surface** as a product metric; may only help find compliance misses |
| `quote` + `signal_notes` | Evidence span + free-text required |

Enums only where they earn keep. **`signal_notes` is required**. Role identity is not a label target.

## Why the audit pack / 100-window UI failed the owner

1. **Wrong job.** Forced vague `turn_kind` / stance enums on windows whose speaker identity was already known.
2. **Confusing triage.** First question “is there a real human turn?” looks like “who spoke?” Clicking “no — agent or harness” clears enums and reveal showed HUMAN `turn_kind` as **`(empty)`** in red vs LLM `coordinator_nudge` — looked like a save bug.
3. **Volume.** 100 windows of schema adjudication is too much work for weak gold.
4. **Grok’s job was inverted.** Extractor should propose semantics; owner calibrates via interview — not first-pass labeling.

## New pipeline

### A — Open-ended Grok extraction (workers / packets)

- Population: `route=ux` ∧ `request_kind=substantive` ∧ `authored_by_agent=0` ∧ not tool plumbing
- Schema: human signals above + required `signal_notes` + quote spans
- Keep legacy `turn_kinds` / stances only as **weak side channels** during migration, not as the publish gate
- No xAI egress by default — Cursor Grok high-fast packet workflow

### B — Owner interview (~15–30 high-signal excerpts, not 100)

- Stratify: frustration, repeat, redirect, unmet, pushback, satisfaction
- Format: excerpt → Grok read → yes/no → one clarifying question
- First pack: `docs/review/owner-interview-pack.md` (10 items live)

### C — Agreement metrics after calibration

- Definitions freeze only after interview answers land
- Then: small spot-check (≤30) for precision/recall on the **new** signal schema
- Do **not** require finishing the old 100-window pack for this

### D — Existing ~2.2k `ux_observations`

**Recommend: keep as weak prior; discard as audit-publish authority; re-extract substantive windows under the new signal schema.**

| Action | Why |
|--------|-----|
| **Keep rows** | Useful for mining candidates, proposal evidence hints, not wasted compute |
| **Do not publish lead metric from them** | Gate is synthetic/restore (`gate_not_validated`) — already blocked |
| **Re-extract substantive only** | New schema + H8-clean tool context; skip worker/handoff/skill dumps |
| **Pause / de-emphasize 100-window UI** | Optional spot-check later; not a P0 blocker |

Do **not** wipe the DB.

## Overview lead metric

Confirmed live: published UX run `71b85e2bad3dc19c1e056335` has `gate_passed=NULL` → `run_is_publishable` → **`gate_not_validated`** → `published_ux_run_id` is `None` → Overview redirect/brake cell stays **unavailable**. Remains abstained until a real calibration gate passes on the new method.

## How proposals / insights consume new signals

- **Proposals:** Prefer clusters of `repeat_request` + `unmet_ask` + `redirect` with quote evidence; theme claims must cite signal_notes, not bare `turn_kind` counts
- **Insights / AGENTS.md suggestions:** Only after interview-calibrated definitions and a spot-check gate; manual apply still required
- **Claims extract:** Mine new signal booleans; treat old `dont_act_yet` / `redirect_or_brake` counts as exploratory priors only
- **Config:** Still observatory — LLM proposes, human applies

## Owner ask: re-label everything?

**No full manual re-label.** Strategy:

1. Pause 100-window enum adjudication as blocker (now)
2. Interview pack → calibrate signal meanings (now / this week)
3. Grok re-extract substantive windows with new schema (workers)
4. Owner spot-checks ~15–30 disagreements after calibration
5. Only then authorize publish / proposal strength

Optional parallel: Grok analyzes a few full transcripts; owner confirms/corrects narrative — same interview energy, higher context.
