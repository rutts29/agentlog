# agentlog Dashboard — v2 Visual Redesign: "Observatory"

Status: Proposal — 2026-08-09
Supersedes the *visual* layer of `dashboard-design.md` v1. Information architecture, statistical-honesty patterns, keyboard model, and the API surface all survive. This document changes how the app **looks and feels**, not what it says.

---

## 0. Why v2

v1 shipped the right skeleton: dense, honest, keyboard-first. The owner's verdict — "too plain / mono" — is accurate. Diagnosis from the current screenshots (`docs/ui-screenshots/`):

1. **No focal point.** Every panel has identical visual weight (same `#111113` card, same 1px `#26262a` border). The eye has nowhere to land.
2. **Color is only whispered.** Harness colors are deliberately desaturated to similar luminance, so the one dimension that could give the UI identity reads as noise-gray at a glance.
3. **No depth.** Flat fills, no elevation model, no light. The v1 rule "no shadows, 1px borders only" produced a wireframe that never got painted.
4. **Numbers don't perform.** KPI values cap at 24px; the most important figures on screen are visually smaller than a nav label in most dashboards.

v2 keeps v1's discipline and adds a visual system with a **center of gravity**: a live force-directed graph of the owner's agent activity, flanked by telemetry.

---

## 1. Design direction — "Observatory"

**One paragraph of intent.** The dashboard is a dark observatory: a black room whose instruments all face one glowing object in the center. Reference 1 (the sci-fi HUD) supplies the *anatomy* — a luminous centerpiece on a dot-grid stage, flanked by dense monospace telemetry columns, thin rules, engraved labels. Reference 2 (the dark SaaS analytics app) supplies the *paint* — confident saturated accents, big display numerals, gradient-filled charts, soft rounded panels with breathing room. The fusion rule that keeps this coherent rather than a costume: **structure from ref 1, color behavior from ref 2, restraint from v1.** Concretely — layout density, mono telemetry, and glow belong to the graph stage and its immediate frame; saturated color appears only where it encodes meaning (harness identity, live activity, state), but when it appears it is *fully saturated*, not the v1 whisper. Dark only. No emoji anywhere. No decorative color: every hue on screen is a legend entry.

Three enforcement rules a builder can check any screen against:

- **R1 — One glow center per view.** Exactly one element per view may emit light (the graph on Overview; the active transcript turn on Session detail; the selected row's accent bar elsewhere). Everything else reflects light (borders, fills), never emits.
- **R2 — Saturation budget.** Full-saturation color is limited to: harness dots/strokes/node fills, live-pulse cyan, state chips, and one accent per chart series. Backgrounds, panels, and text stay in the neutral ramp. If a screenshot looks "rainbow", the budget is blown.
- **R3 — Mono = machine, Sans = human.** JetBrains Mono renders everything the machines produced (ids, paths, telemetry readouts, transcript tool output, axis ticks). Inter renders everything addressed to the human (labels, prose, buttons). This is already half-true in v1; v2 makes it a hard rule and leans on mono for the ref-1 telemetry feel.

---

## 2. The graph centerpiece

The Overview hero is an Obsidian-style force-directed graph on a dark stage. It must answer, in one glance: *what has my fleet of agents been doing, where, and is anything happening right now?*

### 2.1 Two candidate schemes

**Scheme A — "Constellation of sessions" (recommended)**

| Element | Encoding |
|---|---|
| **Node: session** (~600 at `All`, fewer under range filter) | Fill = harness color at 90% opacity. Radius = `4 + 3·log10(1 + messages)` px, clamped 4–14px (a 600-message monster reads ~12px, a 2-message ping 4px). |
| **Node: repo anchor** (~10–15) | Larger hollow ring (16–22px radius by session count), stroke `--graph-anchor` (neutral `#3D4654`), label always visible in 10px mono uppercase. Anchors are *places*, sessions are *stars around them*. |
| **Edge: orchestration** (parent → child, real `parent_session_id` links) | Solid 1.25px stroke in the parent's harness color at 55% opacity. These are the marquee edges — a supervisor with 12 workers forms a visible burst. |
| **Edge: session → repo anchor** | Faint 0.5px `#232A35` at 35% opacity — present so clusters read as deliberate, nearly invisible individually. |
| **Live state** | The most recent session in the last 15 minutes gets the pulse treatment (§2.5). |

Cluster physics does the geographic work: sessions gravitate to their repo anchor, so repos become constellations without any manual layout. Orchestration bursts (one bright hub with a ring of children) are the visual reward — they exist in the data today (some sessions have 10+ children) and no other view shows them spatially.

**Scheme B — "Capability map"**

Nodes are the *vocabulary* rather than the events: repos (~15), models (~20), skills that fired (~26 of 94). Edges are co-occurrence weighted by session count: model↔repo (model used in that repo), skill↔repo (skill fired there), skill↔model. Node size = session count; edge width = `log(co-occurrence)`. ~60 nodes, ~200 edges.

*Why B loses:* it's a prettier ontology but a worse instrument. It has no drill-down terminal (clicking a "gpt-5.5 ↔ local-sec" edge gives a filter, not a session), it barely changes day to day so the live SSE story dies, and co-occurrence edges invite causal misreading — which violates the project's statistical-honesty stance. Scheme B survives as a possible future "Map" tab, not the hero.

**Recommendation: Scheme A.** Every node is a real, clickable session; orchestration edges are real foreign keys, not inferred affinities; new sessions are new stars, which makes the live behavior obvious and honest.

### 2.2 Physics feel

Target feel: **settled observatory, not screensaver.** The graph should reach visual rest in ~2 seconds and then hold still until data changes.

- `forceManyBody().strength(-28)` — enough repulsion to prevent overlap clumps without scattering clusters.
- `forceLink()` — orchestration links `distance(26).strength(0.9)` (children hug their supervisor); anchor links `distance(60).strength(0.05)` (loose gravity toward the repo).
- `forceCollide(r + 2)` — no node overlap ever.
- `forceX/forceY` toward each repo anchor's seeded position (anchors arranged on a golden-angle spiral from center, largest repo nearest center) at `strength(0.04)`.
- `alphaDecay(0.035)`, and **stop the simulation** when `alpha < 0.005` — do not idle-tick. Reheat only on: data change (`alphaTarget(0.15)` for 1s), node drag, or SSE arrival (§2.5).
- **Bare drag pans the stage; node drag is off** (`enableNodeDrag={false}`). Node-drag competed with panning on every press and made the stage feel unpredictable — panning wins. Pinning a node has no owner binding today.

### 2.3 Hover, click, keyboard

- **Hover (mouse):** ego-network highlight. Hovered node + direct neighbors render at full opacity with a 6px outer glow in the node's harness color; all other nodes/edges dim to 18% over a 120ms transition. A tooltip card (popover token, §3) shows: title/first-prompt snippet, harness dot + model chip, repo, start time, duration, message/tool counts, child count if supervisor.
- **Click:** selects and pins the highlight; a compact **inspector strip** slides into the stage's bottom edge (not a modal) with the same metadata plus `Enter → open session` and `c → filter Sessions to this repo`. Click empty stage to deselect.
- **Double-click / Enter on selection:** navigate to Session detail. Every graph interaction terminates at the same place all v1 drill-downs do.
- **Keyboard (preserves keyboard-first):** with graph focused — arrow keys walk the ego network (left/right cycle siblings by angle, up to parent, down to first child); `Tab` cycles supervisor hubs by child count; `Enter` opens; `Esc` deselects; `f` fits view; `+/-` zoom. The stage is one tab-stop from the app's normal flow; focus ring on the stage border, selection ring on the node.
- **Reduced-motion:** all of the above with transitions at 0ms and no pulses; the graph renders its settled layout immediately (run simulation synchronously for 300 ticks on load, then draw once).

### 2.4 Implementation recommendation

**Use `react-force-graph-2d`** (vasturiano) — canvas rendering, d3-force physics, built-in zoom/pan/drag/hover-picking, `nodeCanvasObject` for fully custom painting (glow, rings, labels), and `d3Force()` access for the custom forces above. It is maintained, current, and the de-facto standard; at 600 nodes canvas is comfortably within budget (the library is routinely used at 10k+).

- **Why not hand-rolled `d3-force` + canvas:** we'd re-implement zoom/pan (d3-zoom), drag (d3-drag), and quadtree hit-testing — three more d3 packages plus ~300 lines of fiddly code, to end up with what the library ships. One dependency beats four.
- **Why not Cosmograph / WebGL-first libraries:** GPU physics is for 50k+ nodes; heavier dependency, less custom-paint control. Overkill at 600.
- **Custom paint details** (inside `nodeCanvasObject`): draw glow as a second, larger `arc` fill with a radial gradient at 25% alpha *only* for hovered/selected/pulsing nodes — never all nodes (600 gradient fills per frame would hurt; ≤20 is free). Repo anchor labels via `fillText` in 10px mono, drawn only above zoom level 0.8.
- Node count guard: at `All` range ~600 nodes is fine; if the DB grows past ~3,000, degrade automatically to "sessions from last 90d + all supervisors" with a stage-corner note ("showing 90d · N older sessions hidden") — honest truncation, never silent.

### 2.5 Live SSE behavior

The ingest event stream makes the graph *alive*, on a strict animation budget:

1. **New session event:** node spawns at its repo anchor's position + small random offset, radius animates 0→target over 350ms ease-out, and emits **one** expanding ring (stroke in harness color, radius → 40px, opacity → 0, 600ms). Simulation reheats to `alphaTarget(0.12)` for 800ms so neighbors make room, then cools.
2. **New child-session event:** same spawn, plus its orchestration edge draws in over 250ms (stroke-dash sweep).
3. **Session-activity event** (messages appended to a live session): the node gets a soft 1.2s glow pulse — no ring, no reheat.
4. **Coalescing guard:** events are queued and drained at most once per 2s; a burst of 10 spawns becomes one reheat with 10 staggered (60ms apart) spawn animations. Hard cap: if >25 events are queued (bulk re-ingest), skip animation entirely, apply data, single reheat, show a toast ("47 sessions ingested").
5. **Visibility guard:** `document.hidden` → pause simulation and drain queue without animation on return.

---

## 3. Color system

Direction: keep the neutral ramp *nearly* black but shift it blue-cold (ref 1's near-black is not pure gray), and replace the v1 whisper-accents with saturated ones (ref 2). All tokens live in `web/src/index.css` `:root` — the existing views consume them by name, which is what makes this a re-skin, not a rebuild.

### 3.1 Base surfaces (replaces v1 values, same token names)

| Token | v1 | **v2** | Use |
|---|---|---|---|
| `--background` | `#0a0a0b` | **`#07090D`** | App background (blue-cold near-black) |
| `--card` | `#111113` | **`#0D1016`** | Panels, cards |
| `--popover` | `#161618` | **`#131722`** | Menus, tooltips, graph inspector |
| `--muted` | `#1c1c1f` | **`#151A23`** | Table headers, input fills |
| `--border` | `#26262a` | **`#1E2530`** | 1px panel borders |
| `--border-faint` | `#1c1c1f` | **`#161B24`** | Hairlines, row dividers |
| `--foreground` | `#e7e7ea` | **`#E8ECF3`** | Primary text |
| `--muted-foreground` | `#8b8b93` | **`#8B95A6`** | Secondary text |
| `--faint-foreground` | `#55555c` | **`#525C6E`** | Timestamps, placeholders |
| `--primary` | `#d4d4d8` | **`#D7DEE9`** | Buttons, active nav |
| `--ring` | `#3f3f46` | **`#2DD4BF`** at 60% | Focus rings — focus is now *visible* |

New surface tokens:

| Token | Value | Use |
|---|---|---|
| `--stage` | `#05070A` | Graph stage background — one step darker than `--background`, so the stage reads as a recessed window |
| `--elevated` | `rgba(19, 23, 34, 0.78)` | Overlay panels (palette, inspector) — pairs with `backdrop-filter: blur(14px)` |
| `--graph-anchor` | `#3D4654` | Repo anchor rings/labels |

### 3.2 Accents — six, each with a job

| Token | Hex | Semantic assignment |
|---|---|---|
| `--accent-live` | **`#22D3EE`** (cyan) | Reserved: live/now. SSE pulses, "synced Ns ago" dot, live session marker, streaming indicators. This is ref 1's cyan and it means exactly one thing: *happening now*. Never used for a harness or chart series. |
| `--harness-codex` | **`#5B9DFF`** (blue) | Codex identity everywhere: dots, node fills, chart series, transcript chips |
| `--harness-claude` | **`#F5A623`** (amber) | Claude identity |
| `--harness-cursor` | **`#2DD4BF`** (teal) | Cursor identity (distinct from `--accent-live` cyan by hue and role; they never appear in the same encoding) |
| `--harness-warp` | **`#E45CC3`** (magenta) | Warp identity |
| `--harness-other` | `#66718A` | Unknown/other — deliberately gray |

Same hue *families* as v1 (codex was blue-ish, claude tan, cursor green, warp purple) so learned associations survive — they just get turned up from ~30% to full saturation.

State tokens (small elements only — dots, deltas, chips, ≤2px strokes; never large fills):

| Token | Hex |
|---|---|
| `--status-ok` | `#4ADE80` |
| `--status-warn` | `#FBBF24` |
| `--status-error` | `#FB7185` |
| `--status-info` | `#7DA8FF` |
| `--speaker-human` | `#F0C674` (brightened from v1 `#e3b76e`; the human stays the warmest voice in transcripts) |

### 3.3 Glow and elevation

Two-layer elevation model replacing v1's "borders only":

- **Resting panel:** `border: 1px solid var(--border)` + `box-shadow: inset 0 1px 0 rgba(255,255,255,0.04)` — a 1px top inner highlight that reads as ambient light from above. Costs nothing, kills the "flat sticker" look on every card at once.
- **Active/focused element:** border shifts to the relevant accent at 45% + outer glow `0 0 20px -6px <accent at 35%>`. Applies to: focused inputs, the selected table row's left accent bar, the active nav item, the graph stage while focused.
- **Glow discipline (R1):** outer glows only on the single glow-center per view + transient states (focus, hover on graph nodes, live pulse). No permanent glowing chrome.

### 3.4 Gradient rules

1. **Chart area/bar fills:** vertical `linear-gradient(<series> 32% → transparent)` with a 1.5px solid stroke on top. This alone converts every existing Recharts chart from wireframe to ref-2 without touching chart code beyond `<defs>`.
2. **Stage vignette:** `radial-gradient(ellipse at center, rgba(34,211,238,0.05), transparent 65%)` over `--stage` — the faint ref-1 "instrument glow" behind the graph.
3. **KPI numerals** (optional, tasteful): `background: linear-gradient(180deg, #E8ECF3, #A9B4C6); background-clip: text` on display numbers only.
4. **Prohibited:** gradient buttons, gradient borders, gradient text below display size, any diagonal gradients. Gradients are lighting, not decoration.

### 3.5 Re-skin by token swap — view by view

| View | What changes with tokens alone | Small targeted edits (not rebuilds) |
|---|---|---|
| **Sessions** | Harness dots/model chips saturate; row hover picks up `--muted`; focus ring becomes visible teal | Selected row gets a 2px left accent bar in harness color |
| **Models** | Chart series inherit saturated harness/model colors; bars get gradient fills via shared `<defs>` | Rank-1 model row gets display-numeral treatment in its share cell |
| **Session detail / transcripts** | Speaker tokens brighten (human gold pops harder against colder base); tool chrome stays faint | Active turn (keyboard cursor) becomes the view's glow center: left border 2px `--speaker-human`/harness + soft background wash |
| **Skills** | GATED chips restyle to outlined `--status-warn` with dashed border; trend bars gain gradient | None |
| **Adjudicate** | Stance/outcome chips map onto state tokens; keyboard-selected card gets active-element glow | None |
| **Orchestration** | Tree rails color by harness; supervisor cards get child-count in display numerals | "Open in graph" affordance linking to Overview with that hub selected |

**Statistical honesty survives untouched:** GATED chips, em-dash unavailable values, denominators, "AWAITING DATA" wells all keep their exact copy and logic. Visual upgrade: unavailable wells render on `--stage` with a dashed `--border` and centered mono caption — dark, recessed, clearly *inactive* rather than broken. Gated metrics never receive accent color; they stay in the neutral ramp until they earn data.

---

## 4. Depth and texture — killing the flatness

All CSS-implementable. Each technique lists its performance guard.

1. **Recessed stage + raised panels.** Three-plane depth: stage (`--stage`, darkest, recessed) < app background < cards (`--card` + inner top highlight, raised). The eye reads depth from luminance stepping alone — zero blur cost.
2. **Layered translucency.** `--elevated` + `backdrop-filter: blur(14px)` on *overlays only*: command palette, graph inspector strip, tooltips, toasts. **Guard:** never on scroll containers or persistent panels (compositing cost on large scrolling surfaces); ≤2 blurred elements on screen at once.
3. **Dot-grid stage floor.** `background-image: radial-gradient(rgba(139,149,166,0.05) 1px, transparent 1px); background-size: 22px 22px` on the graph stage — ref 1's engineering-paper texture. **Guard:** static CSS background, one paint, zero animation.
4. **No ambient / decorative motion.** Nothing loops, breathes, or sweeps as ornament. Idle screens must be byte-stable. Motion is otherwise permitted as (a) a direct response to user input (hover, click, zoom, view transition), (b) a one-shot reaction to a genuine live-data event (SSE spawn/arrival), or **(c) a loader for genuinely in-progress agent work**. Category (c) is required, not optional: when a live session is in `streaming` or `tool_running`, its ACTIVE NOW orb animates continuously for as long as that state holds — same justification as a spinner (motion *is* the information). When the state ends, the animation ends. A `waiting` session is blocked on the human and must use a settled/quiet treatment with **no** loader loop. Do not delete working loaders because this section bans ambient motion — they are different things. Live-connection offline state stays a static mono label in the stage corner. (The earlier scan-line sweep accent was cut as decorative ambient motion.)
5. **Border glow on active elements.** Per §3.3. `box-shadow` transitions at 150ms.
6. **Chart gradient fills.** Per §3.4.1 — shared SVG `<defs>` component consumed by all Recharts instances.
7. **Display numerals.** New type roles above the v1 24px cap: `display-xl` 48px/640 (the single hero stat, e.g. session count on Overview), `display-md` 32px/600 (KPI cards), both Inter with `tnum` + `-0.02em` tracking; 11px mono uppercase labels with `+0.08em` tracking underneath (engraved-plate look). The v1 "no font above 24px" rule is repealed — it was a major cause of flatness.
8. **Heatmap re-paint.** Cells go from gray-opacity ramp to a two-hue ramp: `#151A23` → `#0E4A57` → `#22D3EE` (dark → deep teal → live cyan). Same cells, same data, new ramp — the single cheapest "wow" in the app.
9. **Micro-animation thresholds.**
   - *Animates (one-shot):* graph pulses/spawns (§2.5); hover/focus transitions (120–150ms opacity/color/shadow); KPI count-up on range change only (250ms, once); toast enter/exit.
   - *Animates (while work is in flight):* ACTIVE NOW thinking-orb loaders for `streaming` / `tool_running` sessions (live cyan). Stops the instant the session leaves that state. Graph live nodes keep a findable static halo; the rail orb is the primary loader surface.
   - *Never animates:* idle / `waiting` orbs; table rows; transcript content; layout/size of persistent panels; chart data (no entrance animation — v1 rule kept); anything on initial page load when no session is working; scrolling; decorative loops of any kind.
   - *Global guard:* every animation behind `@media (prefers-reduced-motion: no-preference)`; only `transform`/`opacity`/`box-shadow` are animated (compositor-friendly); no `transition: all`.

---

## 5. Layout evolution — Overview as observatory

Ref 1's anatomy: dense left column · central stage · dense right column. Applied to Overview (sidebar nav and top bar unchanged):

```
┌sidebar┬──────────────────────────────────────────────────────────────────────┐
│       │ top bar: range · ⌘K · sync                                           │
│       ├──────────────┬──────────────────────────────────┬────────────────────┤
│       │ TELEMETRY (L)│        GRAPH STAGE               │ TELEMETRY (R)      │
│       │ 260px        │        (fills remaining, min     │ 300px              │
│       │              │         520px, ~60% width)       │                    │
│       │ SESSIONS     │                                  │ ATTENTION          │
│       │ 581  (disp-xl│   · dot grid · vignette ·        │ inbox items,       │
│       │  + spark)    │   · force graph ·                │ state-chip styled  │
│       │ MESSAGES     │   · loaders only while working · │                    │
│       │ 32.9k        │                                  │ MODEL MIX          │
│       │ TOOL CALLS   │   [inspector strip slides in     │ top 6, gradient    │
│       │ 57.0k        │    at stage bottom on select]    │ bars + share %     │
│       │ WINDOWS      │                                  │                    │
│       │ 3,249        │                                  │ LIVE FEED          │
│       │ TOKENS  EST. │                                  │ last 8 sessions,   │
│       │ COST    EST. │                                  │ ticker rows,       │
│       │ (gated wells)│                                  │ newest glows 2s    │
│       │              │                                  │                    │
│       │ HARNESS MIX  │                                  │                    │
│       │ 4 rows: dot +│                                  │                    │
│       │ count + bar  │                                  │                    │
│       ├──────────────┴──────────────────────────────────┴────────────────────┤
│       │ BELOW THE FOLD (full width, unchanged components, re-skinned):       │
│       │ activity heatmap (new ramp) · sessions-by-harness area chart         │
│       │ (gradient fills) · top projects · duration histogram · tool usage    │
└───────┴──────────────────────────────────────────────────────────────────────┘
```

**Component moves:**

| Component | v1 position | v2 position | Size change |
|---|---|---|---|
| KPI strip (6 cards) | Horizontal row, top | Left column, stacked compact tiles; SESSIONS gets `display-xl`, others `display-md` | Each smaller in area; hero number much bigger |
| Sessions-by-harness chart | Prime top-left slot | Below the fold, full width, gradient re-skin | Same |
| Model mix | Top right | Right column, top 6 only ("all models →" links to Models) | Smaller |
| Activity heatmap | Mid-page left | Below the fold, full width, new color ramp | Same |
| Top projects | Right column | Below the fold (the graph's repo clusters now carry this info spatially) | Smaller |
| Recent sessions table | Bottom, 8 rows | Right column "LIVE FEED", 8 ticker rows (time · harness dot · repo · duration); newest row glows on SSE arrival | Narrower |
| Request kinds / gated wells | Mid-page | Left column bottom, compact gated-well styling | Smaller |
| **Graph stage** | — | **Center, the hero** | New |

Column headers are 11px mono uppercase with a thin rule — the ref-1 "instrument label" register. Below 1200px viewport width the right column drops below the stage; below 900px the stage collapses to a static pre-settled snapshot with a "open graph" affordance (force sim on a phone-width window isn't worth it; this is a desktop tool).

Other views keep their v1 layouts — they receive the token/texture re-skin (§3.5) only. One structural addition: Orchestration gains "open in graph" links into the Overview stage with the hub pre-selected.

---

## 6. Build plan

Ordered for a single worker; each item lands independently shippable. Sizes: S ≤ half day, M ≈ 1–2 days, L ≈ 3–5 days.

| # | Item | Size | Acceptance criteria |
|---|---|---|---|
| 1 | **Token swap + type scale.** Replace `:root` values in `index.css` per §3.1–3.2; add `--stage`, `--elevated`, `--graph-anchor`; add `display-xl`/`display-md` classes; repeal 24px cap; inner-top-highlight on the shared card style. | S | All views render on the new palette with no component edits; harness colors clearly distinguishable at a glance in Sessions and Models; focus ring visibly teal; no contrast regressions below WCAG AA for body text. |
| 2 | **Depth + texture pass.** Stage/vignette/dot-grid CSS, gradient `<defs>` for Recharts, heatmap ramp, gated-well restyle, active-element glow, reduced-motion guards. | M | Overview screenshot before/after shows obvious depth; charts have gradient fills; heatmap ramp is teal-cyan; gated/unavailable states keep exact v1 copy; `prefers-reduced-motion` kills all animation. |
| 3 | **Graph MVP.** `react-force-graph-2d` (new dep — justified §2.4: one package replacing four d3 packages + custom hit-testing). Scheme A nodes/edges from a new `GET /api/graph?range=` endpoint (sessions: id, harness, repo, msg count, parent_id, started_at). Custom paint, cluster forces, hover ego-highlight, tooltip, click-select + inspector strip, Enter → session detail. No SSE yet. | L | 600 nodes settle in <2.5s and hold at 60fps idle (sim stopped); hover highlight <16ms; every node opens its session; repo clusters visibly separate; orchestration hubs visually obvious; keyboard walk per §2.3 works. |
| 4 | **Overview layout reorg.** Three-column observatory per §5; KPI tiles to left column, live feed right, charts below fold. | M | No component rebuilt (moves + tile restyle only); 1200px/900px breakpoints behave per §5; keyboard tab order: topbar → left column → stage → right column. |
| 5 | **Graph goes live.** SSE subscription → spawn/pulse/reheat per §2.5 with coalescing, burst cap, visibility guard; live-feed row glow (one-shot). | M | Ingesting a session while watching shows spawn ring within 2s; bulk re-ingest (50 sessions) causes zero dropped frames and one toast; killing the stream shows "stream offline" in the stage corner. |
| 6 | **View re-skin audit.** Walk Sessions, Models, Session detail, Skills, Adjudicate, Orchestration, Auto-review against §3.5; selected-row accent bars; active-turn glow in transcripts; Orchestration → graph links. | M | Each view screenshot-reviewed against R1/R2/R3; transcripts keep speaker hierarchy with human warmest; no view has more than one glow center. |
| 7 | **Performance + polish pass.** Profile graph at `All` range; devtools paint-flash audit for blur surfaces; animation budget check; degrade path for >3k nodes. | S | 60fps interaction on the stage; no full-page repaints on hover; graph memory stable over 30min of SSE. |

**Risk register:**

- **Canvas graph perf (600 nodes).** Low risk with sim-stop-at-rest + glow-only-on-≤20-nodes rules. The failure mode is accidental per-frame gradient/text painting for all nodes — the §2.4 paint rules exist to prevent exactly that. Mitigation: item 7's paint audit.
- **SSE animation jank.** Medium risk: naive per-event reheats during bulk ingest would thrash the sim. The §2.5 coalescing/cap design is the mitigation; test with a synthetic 100-event burst.
- **`backdrop-filter` cost.** Low risk if confined to overlays per §4.2. Do not "upgrade" cards to glass — that's the known perf trap and also blows the saturation/glow budget.
- **Color regressions in edge views.** Auto-review and Adjudicate were built against neutral chips; item 6 must actually open them rather than trusting the token swap blindly.
- **Dependency creep.** Exactly one new package (`react-force-graph-2d`). Anything else proposed during build should be hand-rolled or rejected.
