# Product north star — observatory + harness coach

**Locked 2026-08-09** from owner interview answers (all eight reads confirmed `right`) plus explicit product framing.

## What agentlog is

A **local observatory and harness coach** with human in the loop:

1. **Observe** — sessions, presence, tokens, graph, activity across harnesses (keep building this).
2. **Fact cards** — small, interesting, Paxel/YC-style facts from real transcripts (not vibes).
3. **Harness suggestions** — concrete, reviewable diffs for `AGENTS.md` / skills / rules: add this, remove that, tighten this instruction — **you apply by hand**.

It is **not** a feelings dashboard. Do not surface anger, insults, or emotional tone as first-class board content.

## What the board should optimize for

| Want | Examples |
|------|----------|
| Instruction follow / miss | Agent ignored “keep private”; ignored `--agent-teams`; started editing instead of spawning workers |
| Skill use / non-use | Skill X fired in these sessions; skill Y is installed but never referenced in evidence we can see (honest about detector limits) |
| Tiny facts | Model mix on project Z; N redirects after standing rule R; same ask repeated across K sessions |
| Actionable coach | “Add bullet to `~/AGENTS.md`: …” / “Consider dropping duplicate skill …” with quotes + session links |

| Do not lead with | Why |
|------------------|-----|
| Frustration / “are u dumb” / sentiment | Owner does not want expressions reflected; those are weak product signal |
| Unused-skill DEPRECATED banners from zero exposures | Exposure telemetry is incomplete; misleads |
| Auto-apply to harness files | Manual apply only |

## Human-in-the-loop loop (RL-shaped, not end-to-end)

```
transcripts → extract facts + follow/miss + skill evidence
           → LLM drafts proposal diffs (Grok agents / later CLIProxyAPI)
           → board shows fact + suggestion + citations
           → owner Accept / Reject / Defer
           → owner edits AGENTS.md / skills by hand
           → config ledger notices correspondence later (association, not causation)
```

No automatic write to harness configs. No remote transcript egress by default.

## Interview takeaway (calibration)

Owner confirmed the eight contextual reads were right. Use those threads as **instruction-compliance** exemplars (private bot, unfinished delivery, standing agent-teams rule, workers-not-DIY, verify-don’t-guess, etc.) — not as emotion taxonomy training.

## Existing UX emotion labels

Keep ~2.2k `ux_observations` as **weak mining prior** only. Do **not** publish Overview “feelings” metrics. Prefer re-aiming extraction toward:

- standing instruction stated → later violated / followed
- skill mention / activation evidence
- repeat of the same concrete ask (delivery miss), without framing as “user was mad”

## Relation to what already works

Keep: ingest, presence, graph, tokens, adjudication paused as optional, proposals board (manual), write-guard, config ledger, MCP read-only, t3 adapter, etc.

Change emphasis: proposals + Insights cards → facts + follow/miss + skill coach, not sentiment.
