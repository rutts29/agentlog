# Semantic Extraction Test Run

Exploratory read of real exchange windows from `~/.agentlog/agentlog.db`. Discovery only — no annotations written to the DB.

**Date:** 2026-08-09  
**Corpus:** 548 sessions · 31,559 messages · 54,883 tool events · 4,942 exchange windows (2026-04-12 → 2026-08-01)  
**Harness mix (windows):** Codex 2,458 · Claude Code 2,080 · Cursor 404

---

## 1. Sampling method and what was selected

### Method

1. Joined every `exchange_windows` row to `sessions`, request/response `messages`, tool counts, session-level `success=0` tool fails, and nearby `skill_exposures`.
2. Built a stratified pool of **71** windows with overlapping strata:
   - harness (Codex / Claude / Cursor)
   - month (Apr–Aug 2026; Cursor often has null `started_at`)
   - model (`gpt-5.5`, `grok-4.5-build`, `gpt-5.6-sol`, `codex-auto-review`, `glm-5.2`, `claude-opus-4-7`, `openai`, `gpt-5.6-terra`, …)
   - repo / cwd diversity (SolProbe, local-sec, ai_sec, Plugin, jito-mcp, research-papers, empty-window, …)
   - session length (≤15 msgs, ≥150 msgs)
   - “rough” proxies (session tool fails ≥3, window tool_count ≥15)
   - “smooth” short substantive turns
   - skill-adjacent windows, large-text windows, and trivial/empty controls
3. Downselected to **40** for deep reading: capped auto-review to 3 exemplars, preferred non-empty human text and presence of a next user message, kept all three harnesses and clear correction/redirect candidates.

An exchange window was reconstructed as: **request user message → all messages and tool_events with `seq` between request and the next user message (inclusive)**. Joining only `request_message_id` + `response_message_id` is insufficient — **2,695 / 4,942** windows have more than one assistant message before the next user turn.

### Deep-read composition (n=40)

| Dimension | Coverage |
|-----------|----------|
| Harness | Codex 17 · Claude 14 · Cursor 9 |
| Month | Apr 5 · May 6 · Jun 5 · Jul 13 · Aug 2 · null/Cursor 9 |
| Models | gpt-5.6-sol, gpt-5.5, (null)/Cursor, openai, grok-4.5-build, codex-auto-review, claude-opus-4-7, glm-5.2, gpt-5.6-terra, `<synthetic>` |
| Repos | ~12 distinct repo/cwd identities |

This is a discovery sample, not a prevalence estimate. Rare signals may be oversampled; empty Claude windows may be undersampled relative to the corpus (they are ~38% of all windows).

---

## 2. Raw findings (with quoted examples)

Secrets, tokens, and Discord/user IDs redacted below.

### A. Many “windows” are not human conversational turns

Request-text taxonomy across all 4,942 windows:

| Kind | Count | Notes |
|------|------:|-------|
| Empty request text | 1,868 | Almost entirely Claude Code (89.8% of Claude windows) |
| Substantive-looking | 1,434 | Still includes wrappers, briefs, skill dumps |
| Codex auto-review / approval assessment | 1,056 | Synthetic reviewer turns |
| Cursor-wrapped (`<timestamp>` / `<user_query>`) | 394 | Real user text inside XML |
| Tiny (&lt;20 chars) | 66 | |
| Task notifications | 42 | Subagent completion dumps |
| Realtime delegation | 34 | App/computer-use handoffs |
| “Continue from where you left off.” | 24 | Harness resume stubs |
| Slash commands | 12 | e.g. `/discord:access` |
| Skill wrapper / skill body as user msg | ~12 | Huge pasted skill markdown |

**Example — auto-review (not a user):**  
> “The following is the Codex agent history added since your last approval assessment…”

**Example — task notification as “user”:**  
> `<task-notification>…<summary>Agent "Review catalog binding" finished</summary>…**APPROVE**…`

**Example — inter-agent handoff as next user:**  
> `[CODEX -> CLAUDE] Codex here, working in … Ruttansh explicitly asked us to coordinate…`

### B. Claude Code text is often missing

Empty request rate by harness: Claude **89.8%**, Codex **0%**, Cursor **0%**. Many Claude windows are `user "" → assistant ""` with only tool_events carrying signal. Non-empty Claude requests that do appear are often skill bodies (~154k chars), audit briefs, teammate messages, or `[Image: …]` placeholders.

This is a **data-quality / adapter** issue before it is an extraction issue. Semantic labels on empty Claude windows are guesswork.

### C. Clear user corrections exist — and so do lookalikes

**Clear correction (Cursor):**  
> “i said primary would be claude code not ONLY claude code oriented. be mindful of this tho”

**Clear scope correction (Codex, after agent fixed one spot):**  
> “this has to be acrosos evevrythign not jsutthe one i pointed out”

**Process correction (Codex):**  
> “and hwo u started making the changes instead of pawning it off to the workers again??”

**Preference correction (Cursor):**  
> “ok lemme correct we use gpt 5.6 terra medium or high as base and Sol medium for reasoning tasks and for grok we use grok 4.5 high only”

**Borderline — inquiry, not correction:**  
> “also all three values are same is t hat wrong or OK as per demo…”

**Borderline — follow-up that looks like negation:**  
> “MS and Google wont they be already too secure?”  
(This is a content question, not “you did it wrong.”)

**Borderline — complaint mixed with new work:**  
> “the landong page is not done yet? also … the logo is too small … and dont keep commitng eevrything unless needed the ci ffialed on that last pus”

Naive keyword rates on humanish windows (~3,886 excl. auto-review) hit ~17% “correctionish,” but inspection shows heavy false positives (assistant prose in notifications, “wrong” in security findings, casual “dont”).

### D. Frustration is real but usually mild and ambiguous

Explicit rage is rare. What appears more often:

- Casual swear as energy, not anger: “yo! I have shit ton of Usage and its expiring in 24 hours!”
- Distrust of method: “i dont trust regex only i have had numerours bad experiances…”
- Quality pressure: “if half assed then is replced by full assed lol”
- Premature-action pushback: “dont jump on startig out the repo or scaffolding it first this all plan is still immature; cost is a biggest factor…”

Calling these “frustration” without abstain would over-label. Many are **constraint-setting** or **skepticism**.

### E. Mid-task redirects and premature-action brakes are common and valuable

**Redirect / brake (Cursor):**  
> “dont jump on startig out the repo or scaffolding it first…”

**Partial accept + redirect (Codex):**  
> “yeah except the video everythign seems doable and should be done too use --agent-teams…”

**Don’t-act-yet:**  
> “what does this all mean dont act on it yet just info ..”

**Parallel-agent scheduling:**  
> “while claude is wokrng i need you to use terminal ai_sec2 and try to exploit…”

These are often clearer than “correction” and more actionable for product metrics (agent jumped ahead; user pulled scope back).

### F. Satisfaction / approval is soft and non-terminal

**Soft approval + more work:**  
> “yes good based on the readearch and tools we already have find if need more come up with a workflow…”

**Sounds-good as go-ahead:**  
> “sounds good to me use the cursor ggrok 4.5 high fast as your worker army…”

**Bare acknowledgments:** “go ahead”, “ok”, “ok done now loggedin proceed”

True “task done, I’m happy” closers are hard to spot in-window; the next message usually continues the project. Treat approval as **stance**, not outcome.

### G. Instruction violation is hard; some cases are legible

Legible when the next user message names the violation:

- “dont keep commitng eevrything unless needed”
- “and hwo u started making the changes instead of pawning it off to the workers again??”
- Coordinator injection: “Wrong agent context—do not act. This message was intended for the Task 2 implementer.”

Not legible from a single window when the violated rule lived in AGENTS.md / an earlier turn / a skill that was not loaded into text. **Abstain unless the window contains both the constraint and a contradicting action or a user callout.**

### H. Skill / rule influence

Structural hooks that work without an LLM:

- `skill_exposures` (336 rows: matched 167, injected 113, tool_use 56) — Cursor/Vercel/Next skills dominate counts.
- Cursor `<manually_attached_skills>` blocks (e.g. Aikido `/issues` skill) with explicit format rules.
- Claude “# Update Config Skill” bodies pasted as the entire user message (~154k chars).

What does **not** work cleanly: inferring that a skill *caused* behavior. Example: skill says use `AskUserQuestion` for ambiguity; window shows AskUserQuestion tool use — consistent with skill, but not proof of counterfactual influence. Separate claims: **loaded / attached**, **explicitly referenced**, **behavior consistent with**, **no evidence**.

### I. Recurring failure modes (detectable patterns)

| Pattern | Evidence in sample / corpus |
|---------|-----------------------------|
| Long wait/narration loops | Codex windows with 30–40+ short “Still running / Waiting” assistant msgs |
| API hard fail as the whole turn | `API Error: 500 … Internal server error` then user repeats same request |
| Usage-limit stop | “You're out of extra usage · resets …” |
| Tool `success` sparsely populated | 53,542 NULL · 1,208 success=1 · 133 success=0 — cannot rely on success alone |
| Empty Claude message chains | Tool calls with blank user/assistant text |
| Computer-use blocked | Realtime delegation: “I still can’t inspect the live terminal…” |
| MCP unavailable | Aikido skill attached but server missing → canned setup message |
| Cross-agent busy-tree / compile breaks | “Status nudge from lead: … undefined: SandboxRequest” |

### J. Dominant work style: multi-agent orchestration

This corpus is not mostly “solo developer + one agent.” Recurring shapes:

- Codex ↔ Claude handoffs (`[CODEX -> CLAUDE]`, “while claude is working”)
- Worker briefs with owned files + STATUS (“You are Phase3 Sandbox Core Worker…”)
- Cursor supervisor + Grok worker army
- Auto-review lane assessing Codex history
- Shared PLAN.md / STATE.md as coordination substrate

Any taxonomy that only models “user ↔ single assistant” will misread a large fraction of turns.

### K. Agent pushback on the user’s plan

High-value and under-hypothesized:

> “Pause before I set anything up — your proposed mechanism duplicates infrastructure we already agreed on…”  
> (Claude arguing against syncing AGENTS.md ↔ CLAUDE.md; user then agrees)

This is neither correction nor satisfaction; it is **productive disagreement**.

---

## 3. Hypothesis scorecard

| Hypothesized signal | Verdict | Detectability | Evidence |
|---------------------|---------|---------------|----------|
| User corrections | **Real, uncommon as crisp events; common as ambiguous follow-ups** | LLM + abstain; keywords alone fail | Clear “i said…”, “across everything”, “instead of spawning workers”; many false friends |
| Frustration | **Real but rare/mild; often overcalled** | LLM with abstain; do not keyword | Distrust, “half assed”, cost anxiety; casual swearing ≠ frustration |
| Mid-task redirects | **Real and common** | LLM good; some deterministic cues (“dont act”, “instead”, “except”) | Scaffolding brakes, partial accepts, don’t-act-yet |
| Satisfaction / approval | **Real as soft stance; rare as terminal success** | LLM with low confidence; “ok/sounds good” alone insufficient | “yes good…”, “sounds good to me…” usually precede more asks |
| Ignoring / violating instructions | **Real when user callouts; mostly unobservable otherwise** | Needs constraint+action(+callout); abstain default | Commit spam callout; worker-bypass callout; wrong-agent coordinator |
| Skill/rule influence | **Loading is structural; causal influence is mostly guesswork** | Deterministic for attach/inject; LLM only for “consistent with” | `skill_exposures`, attached skills, skill-body user msgs |
| Recurring failure modes | **Real and partly structural** | Mostly deterministic | Wait loops, API errors, empty Claude, usage limits, MCP missing |

---

## 4. Discoveries we did not anticipate

1. **Window pollution is the main problem.** Auto-review, task notifications, skill dumps, continue stubs, and empty Claude rows dominate raw counts. Extraction without triage measures harness glue, not developer experience.
2. **Claude adapter gap.** ~90% empty request text makes Claude nearly unusable for semantic labels until message text (or tool I/O) is recovered.
3. **Multi-agent / supervisor-worker is first-class.** Labels should include `inter_agent_handoff`, `worker_brief`, `coordinator_nudge`, `cross_harness_reference`.
4. **Premature action / scope brake** is clearer and more frequent than classic “you’re wrong.”
5. **Agent pushback** (assistant rejects or redesigns the user’s proposed approach) is measurable and valuable.
6. **Soft approval is not outcome.** “Sounds good” ≈ green light for next step.
7. **Exchange window ≠ two messages.** Reconstruct by `seq` range; 54% of windows have multiple assistant messages.
8. **User English is noisy** (typos, compressed speech). Extractors must not rely on clean grammar; ASR-like text is normal.
9. **Huge asymmetric messages.** Median window (req+resp+next) is 810 chars; p99 is ~60k; skill dumps hit ~155k. Cap inputs or cost explodes.
10. **`tool_events.success` is mostly NULL.** Failure detection needs exit codes in payloads, repeated identical tools, API-error assistant text, or harness-specific parsing — not the success column alone.
11. **Synthetic “user” follow-ups from Cursor** (“Perform any necessary follow-up actions in response to the subagent completion…”) are not human attitude signals.
12. **Image-only turns** (`[Image: …]`) need a separate modality flag; vision content is not in `messages.text`.

---

## 5. Feasibility and cost estimate

### How big is a window?

Including next user message text:

| Set | n | p50 chars | p90 chars | Total chars | ~tokens (÷4) |
|-----|--:|----------:|----------:|------------:|-------------:|
| All windows | 4,942 | 810 | 12,025 | 24.4M | ~6.1M |
| Drop auto-review | 3,886 | — | — | 6.4M | ~1.6M |
| **Human-substantive triage*** | **1,782** | **1,171** | **4,459** | **5.5M** | **~1.4M** |
| Same + require next user ≥10 chars | 1,326 | — | — | 4.5M | ~1.1M |
| Human-substantive, cap req/resp/next at 4k each | 1,782 | — | — | 2.9M | **~0.71M** |

\*Triage rule used above: not auto-review; `length(trim(request)) ≥ 40`; not `<task-%` / realtime_delegation / continue-stub.

**Typical deep-read window:** a few hundred to ~2k chars of human text plus a tool timeline; outliers are skill dumps and task-notification pastes.

**Naive full 4,942 pass:** ~6M input tokens before prompts/tools — wasteful and mostly non-human.  
**Recommended first full run:** ~1,300–1,800 windows, truncated, ≈ **0.7–1.5M input tokens** plus output schema tokens (roughly another 10–20%).

### Proposed triage rule

**Drop (deterministic) if any:**

1. Request matches Codex auto-review / approval-assessment boilerplate  
2. `length(trim(request.text)) < 40` (kills empty Claude + tiny noise)  
3. Request is continue-stub / realtime_delegation / task-notification / Cursor subagent-followup boilerplate  
4. Optional for attitude labels: no next human user message with ≥10 chars of non-wrapper text  

**Keep but route to other pipelines:**

- Auto-review → separate `auto_review` schema (security/approval judgments, not UX)  
- Worker briefs (`Owned files:`, `Finish with STATUS`) → `worker_task` schema  
- Skill-body-as-user → skill-compliance schema, truncated  

**Filter effect:** human-substantive keeps **1,782 / 4,942 (36%)**, drops **3,160 (64%)**. Requiring a next user message drops to **1,326 (27%)**.  
**Caveat:** this currently **starves Claude** (only 165 human-substantive Claude windows) until the adapter fills message text.

### What is detectable with no model call?

| Signal | How |
|--------|-----|
| Request kind taxonomy | Prefix/contains rules on `messages.text` |
| Auto-review / task-notif / delegation / continue | Same |
| Tool density, tool-name histogram, wait-loop shape | `tool_events` seq patterns; many short assistant msgs |
| Skill attach/inject/tool_use | `skill_exposures` + Cursor skill XML |
| API error turns | Assistant text prefix `API Error:` |
| Usage-limit stops | Assistant text patterns |
| Multi-assistant narration | Count assistant msgs in seq range |
| Worker-brief shape | Regex on owned-files / STATUS / “You are … Worker” |
| Cross-harness mentions | Mentions of claude/codex/cursor/grok in short user text (weak) |
| Image-only user | `[Image:` prefix |
| Empty Claude windows | harness=claude ∧ empty text |

Attitude labels (correction, frustration, satisfaction, violation, pushback quality) need a model **or** remain unlabeled.

---

## 6. Proposed extraction design

### Label taxonomy (fit to this corpus)

Primary enums (multi-label allowed):

**Turn kind (deterministic-first)**  
`human_task` · `human_followup` · `clarifying_question` · `soft_approval` · `correction` · `redirect_or_brake` · `dont_act_yet` · `inter_agent_handoff` · `worker_brief` · `coordinator_nudge` · `auto_review` · `harness_synthetic` · `skill_invocation` · `slash_command` · `image_only` · `empty_or_unparseable`

**User stance (LLM, nullable)**  
`neutral` · `approving` · `correcting` · `redirecting` · `skeptical` · `frustrated` · `confused` · `blocked_waiting_on_user` · `abstain`

**Agent stance (LLM, nullable)**  
`executing` · `investigating` · `narrating_wait` · `asking_clarification` · `pushing_back` · `handing_off` · `failing_tooling` · `abstain`

**Outcome of prior work (LLM on next-user, nullable)**  
`accepted_continue` · `accepted_done` · `partial_accept` · `rejected_redo` · `ignored_by_user_topic_shift` · `abstain`

**Process flags (bool or enum)**  
`premature_action_called_out` · `scope_expansion` · `scope_narrowing` · `multi_agent_reference` · `instruction_violation_alleged` · `verification_requested` · `usage_or_api_limit`

**Escape hatch**  
`novel_observations: string[]` — free-text bullets for anything not in the schema (required to be short; empty if none).

### Structured output schema (sketch)

```json
{
  "window_id": "…",
  "extractor": {"name": "ux_v1", "version": "0.1.0", "model": "…", "prompt_hash": "…"},
  "turn_kind": ["human_followup", "correction"],
  "user_stance": "correcting",
  "agent_stance": "executing",
  "prior_outcome": "partial_accept",
  "flags": {
    "premature_action_called_out": false,
    "scope_expansion": false,
    "scope_narrowing": true,
    "multi_agent_reference": true,
    "instruction_violation_alleged": true,
    "verification_requested": false,
    "usage_or_api_limit": false
  },
  "spans": [
    {"role": "next_user", "quote": "i said primary would be claude code not ONLY…", "supports": ["correction"]}
  ],
  "confidence": {"user_stance": 0.7, "prior_outcome": 0.4},
  "abstain_reasons": [],
  "novel_observations": []
}
```

Store LLM rows in a **separate namespace** from deterministic facts (aligned with prior design reviews): rerunnable, versioned, not canonical evidence.

### Reliability tiers

| Label | Tier | Rule of thumb |
|-------|------|----------------|
| `turn_kind` harness_synthetic / auto_review / empty | Deterministic | No LLM |
| `correction` | LLM + abstain | Require explicit contrast with prior agent action or “i said / you missed / instead of” |
| `redirect_or_brake` / `dont_act_yet` | LLM | Usually clearer than correction |
| `frustrated` | LLM + abstain | Default abstain unless affect is explicit |
| `soft_approval` | LLM | Never treat as task success alone |
| `instruction_violation_alleged` | LLM | Only with user callout or clear contradicting tool act in-window |
| `pushing_back` (agent) | LLM | Quote required |
| Skill *causation* | Do not label | Only load/attach/consistent-with |

### Batch / processing strategy

1. **Phase 0 — fix Claude text ingestion** (or extract tool I/O into window text). Otherwise Claude UX metrics are fiction.  
2. **Phase 1 — deterministic classify all 4,942** into turn_kind buckets; write structural features (tool counts, multi-assistant, skills).  
3. **Phase 2 — LLM extract on triage set (~1.3–1.8k)** with:
   - Truncation: user ≤2–4k, assistant summary ≤2–4k (or last N assistant narrations + final), next_user ≤2k, tool timeline as name/action/success list ≤80 lines  
   - Batch size: **8–16 windows per call** if same harness; else **1 window** when &gt;8k chars  
   - Pin one extraction profile (model + prompt hash); record disagreement later if re-run on another host  
4. **Phase 3 — stratified audit:** hand-label 100 windows (not used as quiet training without consent) for precision/recall of correction/redirect/frustration.  
5. **Do not** dump auto-review or 154k skill bodies into the UX model without a dedicated truncated template.

Rough cost order of magnitude for Phase 2 at ~0.7–1.5M input tokens: depends entirely on which subscription/API path is used; the design should assume **budget is the constraint**, so triage + truncation are mandatory.

---

## 7. Honest limitations of a 40-window sample

- **Not prevalence.** Oversampled rough sessions, corrections, and Cursor; undersampled relative mass of empty Claude and auto-review.
- **One developer’s style.** Informal spelling, multi-agent workflows, security/portfolio projects — labels may not transfer.
- **Claude blindness.** Conclusions about Claude UX are provisional until text is present.
- **No ground truth.** All “clear correction” judgments are this reader’s; borderline cases would disagree across annotators.
- **Window ≠ session outcome.** Satisfaction and “task succeeded” often need later turns or git evidence.
- **Skill influence and instruction violation** usually need config snapshots and prior turns beyond one window.
- **Keyword corpus scans** in §2/§3 are heuristic probes, not validated rates.
- **Tool success field** underrepresents failures; “smooth vs rough” sampling used imperfect proxies.
- **Redaction:** quotes were scrubbed for secrets/IDs; some nuance may have been lost.

---

## Bottom line for the 4,942-window run

1. **Triage first** — ~64% of windows should not enter the UX semantic extractor.  
2. **Fix or quarantine Claude empties** before claiming cross-harness coverage.  
3. Prefer labels this data actually supports: **redirect/brake, soft approval, correction-with-abstain, multi-agent handoff, agent pushback, tooling failure** — not a generic emotion taxonomy.  
4. Keep **deterministic structure** and **LLM interpretation** in separate versioned stores.  
5. Cap every field; otherwise skill dumps and task notifications will dominate token spend without teaching you about the developer.
