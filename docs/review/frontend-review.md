# Frontend Code Review — agentlog dashboard (`web/`)

**Date:** 2026-08-09
**Scope:** Full read-only review of `web/src` (React + Vite + TypeScript) against `docs/dashboard-redesign-v2.md`, the live API on port 8787, and the running dashboard.
**Method:** Read every file under `web/src`; ran `npm run build` (tsc + vite — **clean, zero errors**); probed live API endpoints (`/api/summary`, `/api/models`, `/api/timeseries/sessions`, `/api/attention`, `/api/graph`) with curl; inspected the running dashboard in a browser (zero console errors/warnings).

Findings in `ConstellationGraph.tsx` and the Overview live-presence rail are marked **[provisional]** — a worker is editing those files.

Line numbers are as of commit state at review time.

---

## Severity: HIGH

### H1. Tokens KPI renders real data as unavailable, and its type no longer matches the API — confirmed defect

**Files:** `web/src/views/Overview.tsx` ~382–383, `web/src/lib/api.ts` ~82–83, `web/src/components/kpi.tsx` ~160–162

`fetchSummary`'s type declares `kpis.tokens_est: { status, message }`, but the live API now returns a rich corpus-totals object:

```json
"tokens_est": {
  "status": "partial",
  "totals": { "input_tokens": 2049428845, "output_tokens": 27723403, ... },
  "coverage": { "sessions_with_usage": 473, "sessions_total": 632, ... },
  "note": "Native token usage from 473/632 sessions...",
  "cost": { "status": "unavailable", "message": "pricing.toml has no model rates..." }
}
```

There is no `message` field at the top level. The Overview passes `kpis.tokens_est.message` (undefined) into `KpiUnavailable`, whose tooltip is empty, and `KpiUnavailable` hardcodes the visible caption `"pending token normalization"` for every metric.

**Consequence:** The dashboard shows "—  Est. tokens  pending token normalization" while the backend has 2.05B input tokens with explicit 473/632 coverage. This violates statistical honesty in the *other* direction — the UI implies no data exists when partial data with a labeled denominator is available. The cost well's real reason (missing pricing rates) is also invisible except on hover, replaced by the same wrong caption.

**Reproduce:** Open Overview on `all`; compare the Tokens well against `curl :8787/api/summary?range=all | jq .kpis.tokens_est`.

**Fix:** Update the `fetchSummary` type to the new shape. Render partial totals with their coverage denominator (e.g. "2.0B in · 27.7M out — usage on 473/632 sessions"), keeping the gated treatment only for `status: "unavailable"`. Make `KpiUnavailable` display the actual `reason` text instead of a hardcoded caption (see M6).

### H2. Models daily chart silently drops a real model series — confirmed defect

**File:** `web/src/views/Models.tsx` ~71–77

The server already caps `/api/timeseries/sessions?by=model` at top-8 + `(other)`. The frontend then applies `.slice(0, 8)` to `modelKeys`, whose order is *first-seen in the rows*, not by volume. Live check on `all`: key order is `(other), (unknown), claude-fable-5, gpt-5.2-codex, ..., grok-4.5` — the slice keeps `(other)` and `(unknown)` and drops `grok-4.5`, an actual top-8 model.

**Consequence:** The stacked daily chart under-reports totals with no truncation label — exactly the "chart that silently drops rows" failure mode the honesty spec forbids. `(other)` in the legend has no explanation either.

**Reproduce:** Models view on `all`; count legend entries vs `jq '.series[0] | keys'` on the timeseries endpoint; `grok-4.5` (or whichever model is 8th by volume but last-seen) is missing.

**Fix:** Remove the client-side slice (server already caps), or sort keys by total volume before slicing. Same for the `.slice(0, 6)` in `monthlyChart` (~86) — if kept, add a "+N more" indicator.

### H3. Switching between cached ranges fires a false "N sessions ingested" toast and mass spawn/reheat — [provisional]

**File:** `web/src/components/ConstellationGraph.tsx` ~563–601

The diff effect compares the current graph payload against `prevStatsRef` and treats every unseen id as newly ingested. When the user flips between ranges React Query has cached (e.g. All → 7d → All), the graph doesn't unmount and the ref still holds the other range's id set, so hundreds of "new" ids appear: a "N sessions ingested" toast fires, and either ≤25 spawn animations or a full simulation reheat run — for pure view churn, not data arrival.

Additionally, `d3ReheatSimulation()` resets alpha to 1.0; the spec (§2.5) prescribes `alphaTarget(0.12)` for 800 ms then decay. A reheat on range switch is a much more violent settle than spec'd.

**Consequence:** Misleading ingest notifications and gratuitous motion on ordinary navigation — undermines trust in the live indicator and violates "no motion while nothing is happening."

**Reproduce:** Overview → wait for graph to settle → switch range to 7d → wait → switch back to All. Toast appears with no ingestion.

**Fix:** Reset `prevStatsRef` (and skip the animation branch) whenever `range` changes — pass `range` into the component and clear the ref in an effect keyed on it, or key the whole diff by range. Use `alphaTarget(0.12)`-style gentle reheat per spec.

### H4. `]` / `[` keyboard collision between AppShell and Adjudicate — confirmed defect

**Files:** `web/src/components/layout/AppShell.tsx` ~75–84, `web/src/views/Adjudicate.tsx` ~430–434

AppShell binds `[`/`]` globally to cycle the time range. Adjudicate binds `]` on its own window listener to jump to the next unlabeled pair. Both listeners fire — `preventDefault` doesn't suppress a sibling listener — so pressing `]` in Adjudicate advances the pair **and** silently mutates the global range in the URL.

**Consequence:** Invisible state corruption: the user returns to Overview later and everything is scoped to a range they never chose.

**Reproduce:** Open `/adjudicate`, watch the range control in the top bar, press `]`.

**Fix:** Either give Adjudicate a different key (`u` for "next unlabeled" is free), or have AppShell skip bracket handling on routes that claim it (simplest: `if (location.pathname.startsWith('/adjudicate')) return` in the bracket branch).

---

## Severity: MEDIUM

### M1. Model-mix rows are visually indistinguishable and lose harness on click — confirmed defect

**File:** `web/src/views/Overview.tsx` ~531–543

`/api/models` items are keyed by (harness, model), and live data has two `grok-4.5` rows (16.3% under claude, 11.2% under another harness) and two `(unknown)` rows. The list renders only the model name — identical labels with different bars, no way to tell them apart. Clicking either navigates to `/sessions?model=grok-4.5`, silently merging both harnesses. The API's `subtitle` ("Share of sessions with a recorded model...") is fetched but never rendered, so the percentages have no stated denominator on this panel.

**Fix:** Prefix each row with its harness dot (colors exist in `harnessColor`), include `harness` in the link query, and render the subtitle line. Same duplication exists in the Models view table (~152–176), which does show a separate harness column but sorts interleaved so twin names sit apart.

### M2. `profiles` / `by_agent_profile` lane from `/api/models` is dropped entirely — confirmed defect

**Files:** `web/src/lib/api.ts` (`fetchModelMix` type), `web/src/views/Models.tsx`

The API deliberately separates agent profiles (explorer 35%, worker 29%, `grok-4.5-build` 20%, …) from model mix — the fix for the earlier "agent identities rendered as model names" bug — and ships `profiles`, `by_agent_profile`, and `profiles_note` explaining the distinction. The frontend types and UI ignore all three. Sessions attributed only to a profile have no surface anywhere, and the model mix implies it's the complete attribution story.

**Fix:** Add a "Agent profiles" section to the Models view rendering `profiles` with `profiles_note` as the caveat line.

### M3. Multi-year x-axes strip the year, producing apparently non-monotonic ticks — confirmed defect

**Files:** `web/src/views/Overview.tsx` ~631–634, `web/src/views/Models.tsx` (tickFormatter), `web/src/views/AutoReview.tsx`

Tick formatter is `d.slice(5)` (MM-DD). On `all`, data spans 2025-06 → 2026-08, so the axis reads `06-23, 06-27, 07-06, 04-25, 06-05…` — looks unsorted/broken. Observed live.

**Fix:** When the series spans years, include the year (`YY-MM-DD` or `MMM 'YY`); trivially: conditional format based on first/last day prefix.

### M4. Overview `dailyTotals` double-counts via the API's `total` key — confirmed defect (currently masked)

**File:** `web/src/views/Overview.tsx` ~319–324

`/api/timeseries/sessions?by=harness` rows include a `total` key alongside per-harness counts (verified live). The reducer sums every numeric key except `day`, so each day's value is exactly 2× actual. Today this is invisible because the Sparkline normalizes to its own max — but any future labeled use (tooltip, axis, count-up) will be wrong, and the sibling `harnessTotals` (~326–333) hardcodes four harness names, so any new harness would silently vanish from the Harness mix panel (suspicion — none exist in data today).

**Fix:** Use `row.total` directly for `dailyTotals`; derive harness keys from the data (excluding `day`/`total`) for `harnessTotals`.

### M5. Graph Tab handling is a keyboard trap — [provisional]

**File:** `web/src/components/ConstellationGraph.tsx` ~1157–1163

When the stage has focus, Tab and Shift+Tab are always `preventDefault`ed to cycle repo hubs. There is no keyboard path out: Esc only deselects a node, never blurs the stage. The spec wants both "stage is one tab stop" (§7) and "Tab cycles hubs" (§2.2) — as implemented, the second silently kills the first.

**Fix:** Intercept Tab only while a node is selected (Esc then releases), or let Shift+Tab from the first hub / Tab past the last hub fall through to normal focus traversal.

### M6. `KpiUnavailable` hardcodes one caption for all metrics — confirmed defect

**File:** `web/src/components/kpi.tsx` ~160–162

The visible caption is always `"pending token normalization"` regardless of the `reason` prop, which is relegated to a `title` tooltip. For the Cost well the real reason is missing pricing rates — different actionable cause, invisible without hovering. Compounds H1.

**Fix:** Render `reason` (truncated) as the caption; keep the full text in `title`.

---

## Severity: LOW

### L1. Tautological live-session check — [provisional]

`web/src/components/ConstellationGraph.tsx` ~297–303. `linkedLive` filters on `liveById.has(s.session_id)`, but `liveById` is built from the same array, so the test is always true; the intent was almost certainly `nodeMapRef.current.has(...)` / graph-node membership. Harmless today because the pending/linked partition still covers all sessions, but the dead check will mislead the next editor.

### L2. Redundant duplicate effect — confirmed

`web/src/views/Overview.tsx` ~238–248. Two back-to-back effects both assign `prevTopRef.current`; the first one's assignment is dead. Collapse into one. Scar tissue from the rapid-fix passes.

### L3. Sessions filter refetches per keystroke; stale row selection — confirmed

`web/src/views/Sessions.tsx` ~108–113, ~104–106. The text filter writes the URL (and triggers a query) on every keystroke with no debounce — Search.tsx already has a 220 ms debounce pattern to copy. Separately, the keyboard row selection resets on `cursor/range/q/sort/order` changes but not when facet filters (harness, model, project) change, so `Enter` can open a row that shifted under the cursor.

### L4. `d3-force-3d` is a phantom dependency — confirmed

`web/src/components/ConstellationGraph.tsx` line 14 imports `d3-force-3d`, and `web/src/types/d3-force-3d.d.ts` types it, but it isn't in `web/package.json` — it resolves only as a transitive dependency of `react-force-graph-2d`. A future `npm update`/dedupe can break the build. Add it to `dependencies`.

### L5. Adjudicate keydown effect re-subscribes every render — confirmed

`web/src/views/Adjudicate.tsx` ~415–488 has no dependency array (same pattern in `Transcript.tsx` ~55–73). Functionally correct but wasteful and fragile; add deps or move mutable state into refs.

### L6. `enableNodeDrag={false}` contradicts the binding spec — decision needed

Spec §2.2 says "Drag pins a node"; the code disables drag entirely (the accepted fix for the pan-hijack bug). One of the two should change: either amend the spec, or restore drag with `onNodeDragEnd` pinning (`fx`/`fy`) now that the pan bug's root cause is understood.

### L7. Static scanline texture on `.app-backdrop` — decision needed

`web/src/index.css` ~279–288. A repeating-linear-gradient scanline at 0.05 opacity. It's static (the *animated* scan-line was the banned one) but it isn't in the spec's §4 texture vocabulary and reads as a leftover of the removed design. Keep deliberately or delete.

### L8. Attention panel truncates without a count — confirmed

`web/src/views/Overview.tsx` ~342–345. Urgent items `.slice(0, 4)` and resumable `.slice(0, 3)`, but the API's `count`/`resumable_count` are never rendered. With 9 urgent items the panel shows 4 with no "+5 more" — silent truncation, the same honesty class as H2 at lower stakes.

### L9. Minor duplication worth consolidating — confirmed

- `isEditable()` copy-pasted in `AppShell.tsx`, `Sessions.tsx`, `Adjudicate.tsx`.
- `CHART_TOOLTIP` style object duplicated in `Overview.tsx`, `Models.tsx`, `AutoReview.tsx`.
- `truncLabel` exists in `lib/utils.ts` and is re-implemented inline in `ConstellationGraph.tsx`.
- Vestigial `Math.sqrt(1)` constant in the hub-lobe radius formula (`ConstellationGraph.tsx` ~520).

That is the full extent of the "horseshit code" sweep: **no orphaned components, no dead views, no unused route, no leftover components from the cockpit/first-Observatory passes were found.** All files under `web/src` are imported and reachable; Tailwind tokens map to used CSS variables. The codebase is cleaner than its history suggests.

---

## Suspicions needing investigation (not confirmed)

- **Stale `selected` node in the inspector after refetch** (`ConstellationGraph.tsx`): `selectedRef` holds the old node object after a graph refetch replaces node instances, so inspector metadata (last activity, counts) could lag until re-selection. Hard to confirm without live ingest during review. [provisional]
- **`harnessTotals` hardcoded harness list** (see M4) — latent, not active with current data.
- **Command palette Esc scope**: Esc closes the palette only via the input's handler; if focus leaves the input (mouse interaction with the list), Esc may not close it. Behavior depends on focus retention I couldn't fully exercise read-only.

## Explicitly checked and clean

- **Hooks discipline** — every view calls all hooks before any early return; the class of bug that blanked Overview is gone. Build passes `tsc --noEmit` clean, zero browser console errors.
- **SSE lifecycle** (`useIngestStream.ts`) — single `EventSource`, closed on unmount, refs prevent stale closures, and the spec's coalescing/burst-cap/tab-visibility rules (§2.5) are implemented faithfully.
- **Force-graph hygiene** — node positions preserved across refetches via `nodeMapRef`, `autoPauseRedraw` correct, paint budget respected (glow only on hover/selected/pulse, ≤20 concurrent pulses), `prefers-reduced-motion` renders a pre-warmed static layout.
- **Motion discipline** — the only looping animations are live-session orbs in `streaming`/`tool_running` states, exactly the sanctioned case. Waiting states are static. The global reduced-motion CSS kill-switch works.
- **Statistical honesty is mostly strong** — gated wells with reasons, GATED chips, `AggregatePanel`'s unavailable/abstain handling with n and flags, adjudication rates shown as `matches/n`, the graph's corner truncation note ("632 sessions · 31 repos"), search's "N+ hits" phrasing. The failures above (H1, H2, M1, L8) are the exceptions, not the rule.
- **Adjudication blind flow** — commit-before-reveal with optimistic updates and label-preserving retry is genuinely well built.
- **Keyboard-first navigation** — Cmd+K palette, `j`/`k`, `[`/`]`, Esc-to-back, and the full graph shortcut set (`f`, `+`/`-`, arrows, Enter, Esc) all present and working, modulo H4 and M5.
