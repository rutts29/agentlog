# agentlog Dashboard — Design Document

Status: Draft v1 — 2026-08-09
Scope: UI/UX design for the agentlog Unified Command Center — a local, single-user dashboard over all AI coding agent activity (Codex, Claude Code, Cursor, Warp, and future harnesses).

---

## 1. Design principles

1. **Cockpit, not brochure.** Every pixel earns its place. No hero sections, no marketing whitespace, no onboarding fluff. The user already knows what this is.
2. **Dark, quiet, dense.** One background family, one text family, a handful of muted accent colors used only to encode meaning (harness identity, status). No gradients, no glow, no emojis.
3. **Numbers first, charts second.** A stat you can read in 200ms beats a chart you have to interpret. Charts support the numbers, not the other way around.
4. **Everything drills down.** Any aggregate (a bar, a table row, a stat card) links to the sessions behind it. The terminal state of every drill-down is a single session transcript.
5. **Time is the universal axis.** One global time-range control governs the whole app. Changing it never navigates away.

---

## 2. Recommended tech stack

### Decision: React (Vite) + FastAPI, not Streamlit

| Criterion | Streamlit | React + shadcn/ui | Winner |
|---|---|---|---|
| Matches "shadcn/ui inspired" requirement | Approximation via CSS hacks | Native | React |
| Information density / custom layout | Fights you (vertical flow model) | Full control | React |
| Interaction latency (filter, hover, drill-down) | Full-script rerun per interaction | Client-side, instant | React |
| Dark theme control | Theming is coarse | Token-level control | React |
| Dev speed for v0 | Faster | Slower | Streamlit |
| Long-term: this is the product's face | Ceiling hit quickly | Scales | React |

Streamlit is the right call for a throwaway internal report. This dashboard *is* the product for its one user, and the design requirements (shadcn aesthetic, density, subtle interactions) are exactly the things Streamlit is bad at. Take the extra setup cost once.

### Stack specification

- **Backend:** FastAPI (already a Python project; reads `~/.agentlog/agentlog.db` directly). Serves JSON at `/api/*` and the built SPA as static files. Single process: `agentlog serve` → `http://localhost:3000`.
- **Frontend:** Vite + React + TypeScript.
- **Components:** shadcn/ui (Radix primitives + Tailwind).
- **Charts:** **Recharts** — the shadcn charts library is built on it, so chart theming inherits the design tokens for free. Rules: no animated entrances, no 3D, `strokeWidth 1.5`, muted fills at 70–80% opacity.
- **Tables:** TanStack Table v8 wrapped in shadcn `<Table>` — sorting, column visibility, virtualized rows for the sessions list.
- **Data fetching:** TanStack Query. All filter state lives in the URL (`?range=30d&harness=codex,claude`) so views are bookmarkable and back-button works.
- **Date handling:** `date-fns`.
- **No state library** beyond URL params + TanStack Query cache. This app is read-only.

Heatmap (time-of-day grid) and sparklines are trivial to hand-roll as SVG/div grids — do not add a library for them.

---

## 3. Information architecture

Five views. Sidebar navigation, persistent top bar.

```
Overview          — the cockpit; answers "what's my AI usage right now?"
Sessions          — the ledger; filterable/searchable table of every session
Models & Cost     — model distribution, token/cost estimates, model comparisons
Skills            — activation stats, dead skills, effectiveness
Insights          — computed findings: correction hotspots, failure patterns
Session detail    — (not in nav; reached by drill-down) one transcript, annotated
```

Navigation rationale: Overview is the daily glance. Sessions is the workhorse for "what was that session last Tuesday." Models & Cost and Skills each have enough depth to justify a page. Insights is deliberately separate — it holds *derived claims* (with confidence caveats), keeping the other pages strictly factual.

---

## 4. Layout & wireframes

### 4.1 App shell

```
┌──────────┬────────────────────────────────────────────────────────────────┐
│          │  agentlog        [ 7d | 30d | 90d | All | Custom ▾ ]   ⌘K  ⟳  │ ← top bar
│ Overview │────────────────────────────────────────────────────────────────│
│ Sessions │                                                                │
│ Models   │                                                                │
│ Skills   │                    <view content>                              │
│ Insights │                                                                │
│          │                                                                │
│──────────│                                                                │
│ 1,204    │                                                                │
│ sessions │                                                                │
│ synced   │                                                                │
│ 2m ago   │                                                                │
└──────────┴────────────────────────────────────────────────────────────────┘
```

- Sidebar: 200px fixed, collapsible to 48px icon rail. Bottom shows ingest freshness (session count, last sync). `⟳` triggers re-ingest.
- Top bar: global time-range segmented control (applies everywhere), `⌘K` command palette (jump to session, project, skill), no user menu — single user.

### 4.2 Overview

```
┌─ Sessions ──┐ ┌─ Tokens (est) ┐ ┌─ Cost (est) ─┐ ┌─ Correction ─┐ ┌─ Streak ────┐
│ 87          │ │ 41.2M         │ │ ~$63.80      │ │ 11.3%        │ │ 14 days     │
│ ▲ 12% vs    │ │ ▼ 4% vs       │ │ ▲ 9% vs      │ │ ▼ 2.1pt      │ │ longest: 22 │
│ prev period │ │ prev period   │ │ prev period  │ │ good         │ │             │
└─────────────┘ └───────────────┘ └──────────────┘ └──────────────┘ └─────────────┘

┌─ Sessions by harness ──────────────────────────┐ ┌─ Model mix ───────────────┐
│ ▓ codex  ▓ claude  ▓ cursor  ▓ warp            │ │ claude-4.5-opus   ████ 38%│
│  ▁▃▅▂▇▅▃▁▂▄▆▃▅▇▄▂▁▃▅▆  (stacked bars, daily)   │ │ gpt-5.6           ███  29%│
│                                                │ │ composer-2.5      ██   17%│
│                                                │ │ gemini-3-pro      █     9%│
│                                                │ │ other             █     7%│
└────────────────────────────────────────────────┘ └───────────────────────────┘

┌─ Activity heatmap (hour × weekday) ────────────┐ ┌─ Top projects ────────────┐
│      00 02 04 06 08 10 12 14 16 18 20 22      │ │ ai-sec          31  ▂▅▇▃  │
│ Mon  · · · · ▪ ▪ ▓ ▓ ▪ ▓ ▓ ▪                  │ │ Plugin          22  ▁▃▇▅  │
│ Tue  · · · · ▪ ▓ ▓ ▪ ▪ ▓ ▪ ·                  │ │ solprobe        14  ▅▂▁▃  │
│ ...                                            │ │ research-papers  9  ▃▁▂▁  │
└────────────────────────────────────────────────┘ └───────────────────────────┘

┌─ Recent sessions ──────────────────────────────────────────────────────────┐
│ TIME       HARNESS  MODEL             PROJECT      DUR    TOKENS   STATUS  │
│ 13:42      codex    gpt-5.6           ai-sec       24m    182k     ok      │
│ 11:07      cursor   claude-4.5-opus   Plugin       1h02m  841k     retried │
│ ...                                                                (8 rows)│
└─────────────────────────────────────────────────────────────────────────────┘
```

Hierarchy: KPI strip (glance) → two chart rows (patterns) → recent sessions (entry point to drill-down). Every KPI card shows delta vs. previous equal-length period. Clicking any harness segment, model bar, heatmap cell, or project row navigates to Sessions pre-filtered.

### 4.3 Sessions

```
┌─ Filters ───────────────────────────────────────────────────────────────────┐
│ [Harness ▾] [Model ▾] [Project ▾] [Status ▾] [Has skill ▾]  [Search…      ] │
│ Active: harness=codex ×   project=ai-sec ×                     Clear all    │
└─────────────────────────────────────────────────────────────────────────────┘
┌─ 214 sessions ── sorted by start ▾ ──────────────────────────── [Columns ▾]─┐
│ START            HARNESS MODEL           PROJECT   DUR   MSGS TOOLS TOK  ST │
│ Aug 9 13:42      codex   gpt-5.6         ai-sec    24m   31   18   182k ok │
│ Aug 9 11:07      cursor  claude-4.5-opus Plugin    1h02  74   52   841k rt │
│ Aug 8 22:15      claude  claude-4.5-opus ai-sec    41m   48   35   390k ok │
│ ...                                            (virtualized, ~40 visible)  │
└─────────────────────────────────────────────────────────────────────────────┘
```

- Facet dropdowns are multi-select combo-boxes with counts (`codex (98)`).
- Search hits session titles + first user prompt (FTS on the SQLite side).
- Row click → Session detail. Middle-click opens in new tab (it's a URL).
- Status column values: `ok`, `retried`, `corrected`, `abandoned` — rendered as small colored dot + text, never a filled badge wall.

### 4.4 Session detail

```
┌─ ← Sessions                                                                 │
│ cursor · claude-4.5-opus · Plugin · Aug 9 11:07 → 12:09 (1h02m)             │
│ 74 messages · 52 tool calls · ~841k tokens (~$1.94) · 3 corrections         │
├──────────────────────────────────────────────┬──────────────────────────────┤
│ TRANSCRIPT (scroll)                          │ SESSION ANATOMY              │
│                                              │                              │
│ ▸ user     11:07  "Refactor the ingest…"     │ Timeline (1 row/min):        │
│ ▸ agent    11:07  [thinking, 3 tool calls]   │  ▁▂▅▇▅▂▁▁▃▅▂ …               │
│   └ shell: pytest -q          ✓              │                              │
│   └ edit:  src/ingest/base.py                │ Tool call mix:               │
│ ▸ user     11:19  "No — keep the check…"  ◄C │  edit 21 · shell 17 ·        │
│ ▸ agent    11:19  …                          │  read 11 · search 3          │
│                                              │                              │
│ (correction turns flagged ◄C in gutter,      │ Skills fired:                │
│  jump-to-next-correction button)             │  systematic-debugging (2)    │
│                                              │                              │
│                                              │ Files touched: 9             │
└──────────────────────────────────────────────┴──────────────────────────────┘
```

Two-pane: transcript left (collapsed tool-call groups, expandable), computed anatomy right. Corrections are gutter-flagged and jumpable — this is the micro version of the "correction hotspot" insight.

### 4.5 Models & Cost

```
┌─ Est. spend by model over time (stacked area, weekly) ──────────────────────┐
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
┌─ Model comparison table ────────────────────────────────────────────────────┐
│ MODEL            SESS  TOK/SESS  $/SESS  CORR%  RETRY%  AVG DUR  TREND      │
│ claude-4.5-opus  142   610k      $1.41   8.2%   4.1%    38m      ▂▃▅▃      │
│ gpt-5.6          97    340k      $0.62   12.7%  6.8%    26m      ▅▃▂▁      │
│ composer-2.5     61    120k      $0.11   9.9%   3.2%    12m      ▁▂▃▅      │
└─────────────────────────────────────────────────────────────────────────────┘
┌─ Tokens: input vs output ──────────────┐ ┌─ Cost by project ────────────────┐
│ (grouped bar per model)                │ │ (horizontal bars)                │
└────────────────────────────────────────┘ └──────────────────────────────────┘
```

All cost figures labeled "est." with a hover explaining the pricing table used and its date. Correction% and retry% in the same table as cost is the point of this page: cost-per-quality, not cost alone.

### 4.6 Skills

```
┌─ Activations ─┐ ┌─ Distinct fired ┐ ┌─ Never fired ─┐ ┌─ Est. overhead ────┐
│ 312           │ │ 23 / 61         │ │ 38            │ │ ~140k tok/day      │
└───────────────┘ └─────────────────┘ └───────────────┘ │ from unused defs   │
                                                        └────────────────────┘
┌─ Skill table ───────────────────────────────────────────────────────────────┐
│ SKILL                      SOURCE   FIRES  LAST FIRED  CORR% W/  CORR% W/O  │
│ systematic-debugging       claude   84     today       6.1%      13.0%     │
│ verification-…-completion  claude   51     yesterday   7.4%      11.2%     │
│ firecrawl-scrape           plugin   0      never       —         —      ⚠  │
│ ...                                                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

- "CORR% W/ vs W/O" = correction rate in sessions where the skill fired vs. comparable sessions where it didn't — the before/after effectiveness proxy. Shown only when sample size ≥ 20 sessions; otherwise em-dash with a "low sample" tooltip. Never fake confidence.
- Dead skills (0 fires over the full range) get a muted warning marker and roll up into the "estimated overhead" card — the token-waste story.
- Row click → Sessions filtered to that skill.

### 4.7 Insights

A vertical feed of computed findings, each a card with: claim, evidence summary, confidence tag (`high / medium / low`), and a "show sessions" link.

```
┌─ Correction hotspot ──────────────────────────────────────── confidence: high ┐
│ 41% of corrections in `ai-sec` occur in sessions touching `parser/` files.    │
│ 17 sessions · avg 3.2 corrections each · 2.4× project baseline                │
│                                                              [show sessions]  │
└───────────────────────────────────────────────────────────────────────────────┘
┌─ Recurring failure ────────────────────────────────────────── confidence: med ┐
│ `pytest` tool calls fail then get retried within 2 turns in 23 sessions —     │
│ most often missing-fixture errors.                          [show sessions]   │
└───────────────────────────────────────────────────────────────────────────────┘
```

Insights are precomputed at ingest (the existing `analysis/` package), stored with their evidence session-IDs. The UI never computes claims client-side.

---

## 5. Color palette (dark mode)

Near-black neutral base with slightly desaturated accents. Accents encode *identity* (harness) and *status* only — never decoration. Expressed as design tokens compatible with shadcn theming.

### Base tokens

| Token | Hex | Use |
|---|---|---|
| `--background` | `#0A0A0B` | App background |
| `--card` | `#111113` | Panels, cards |
| `--popover` | `#161618` | Menus, tooltips |
| `--border` | `#26262A` | 1px borders (the only panel separation — no shadows) |
| `--muted` | `#1C1C1F` | Table header rows, input backgrounds |
| `--foreground` | `#E7E7EA` | Primary text |
| `--muted-foreground` | `#8B8B93` | Secondary text, axis labels |
| `--faint-foreground` | `#55555C` | Tertiary: timestamps, placeholders |
| `--primary` | `#D4D4D8` | Buttons, active nav (monochrome primary — no brand color) |
| `--ring` | `#3F3F46` | Focus rings |

### Status tokens

| Token | Hex | Use |
|---|---|---|
| `--status-ok` | `#4ADE80` at 85% | Success dot, positive deltas |
| `--status-warn` | `#FBBF24` at 85% | Retries, low-sample warnings, dead skills |
| `--status-error` | `#F87171` at 85% | Corrections, failures, negative deltas |
| `--status-info` | `#60A5FA` at 85% | Neutral annotations |

Status colors appear only in ≤8px dots, small deltas, and thin chart strokes — never as backgrounds or large fills.

### Harness series colors (charts)

Muted, similar luminance so no harness "shouts":

| Series | Hex |
|---|---|
| codex | `#7C9ACB` |
| claude | `#C4A484` |
| cursor | `#8FBC9F` |
| warp | `#B48FB4` |
| other | `#6E6E76` |

Chart fills use these at 70% opacity over `--card`; strokes at 100%.

---

## 6. Typography

Two families, both local/free:

- **UI:** Inter (fallback `system-ui`). Variable font, tabular-numbers feature (`font-feature-settings: "tnum"`) on all metric and table cells so columns of numbers align.
- **Mono:** JetBrains Mono (fallback `ui-monospace`). Transcripts, session IDs, file paths, code, tool-call output.

Scale (rem, 16px base):

| Role | Size / weight | Example |
|---|---|---|
| KPI value | 24px / 600, tabular | `41.2M` |
| Page title | 18px / 600 | `Sessions` |
| Card title | 13px / 500, `--muted-foreground`, +0.02em tracking | `SESSIONS BY HARNESS` |
| Body / table cell | 13px / 400 | table rows |
| Small / meta | 12px / 400, `--muted-foreground` | deltas, timestamps |
| Micro / axis | 11px / 400, `--faint-foreground` | chart axes |

Line-height 1.45 for prose (transcripts), 1.2 in tables. No font size above 24px anywhere.

---

## 7. Component specifications (shadcn mapping)

| Dashboard component | shadcn/ui base | Notes |
|---|---|---|
| App shell sidebar | `Sidebar` | Collapsible to icon rail |
| Time-range selector | `Tabs` (segmented) + `Popover`+`Calendar` for custom | Writes to URL param |
| KPI card | `Card` | Value + delta + 12px label; delta colored by status token |
| Stacked bar / area charts | `Chart` (shadcn charts, Recharts under the hood) | `ChartTooltip` with monochrome style |
| Model mix / project bars | Custom: div-based horizontal bars in a `Card` | Recharts is overkill for 5 static bars |
| Activity heatmap | Custom SVG grid | Cells 12×12px, 5-step opacity ramp of `--foreground` |
| Sparklines | Custom inline SVG, 60×16px | Single `--muted-foreground` stroke |
| Sessions table | `Table` + TanStack Table | Virtualized; `DropdownMenu` for column toggle |
| Facet filters | `Popover` + `Command` (multi-select combobox) | Counts next to options |
| Active filter chips | `Badge` variant `outline` | With `×` remove |
| Command palette | `CommandDialog` | ⌘K; sessions, projects, skills, nav |
| Status indicator | Custom: 6px dot + text | Never `Badge` with filled background |
| Insight card | `Card` + `Badge` (confidence) | Evidence line in `--muted-foreground` |
| Session transcript | `Collapsible` per tool-call group | Mono font; role gutter |
| Tooltips (est. cost etc.) | `Tooltip` | All "est." values must have one |
| Ingest refresh | `Button` variant `ghost` + `Sonner` toast | Toast on completion, no spinner theatrics |

General rules: `border-radius: 8px` cards, 6px controls. No box-shadows — 1px `--border` only. Density: 16px card padding, 8px table row padding.

---

## 8. Interaction patterns

1. **Global time range.** Segmented `7d / 30d / 90d / All / Custom`. Persisted in URL; every query includes it. Changing range updates in place (TanStack Query refetch), never navigates.
2. **Universal drill-down.** Chart segments, heatmap cells, table rows, KPI cards, and insight cards all resolve to a Sessions URL with filters applied. One mental model: *click anything aggregate → see the sessions behind it → open one*.
3. **Filters are URLs.** All filter state serializes to query params. Back button undoes filtering. Copy the URL to save a view.
4. **Hover, don't clutter.** Exact values, "est." explanations, low-sample caveats live in tooltips. The resting UI shows rounded figures.
5. **Keyboard.** `⌘K` palette; `[` / `]` cycle time range; `j`/`k` row navigation in Sessions; `Enter` opens; `n` jumps to next correction in a transcript.
6. **Comparative deltas everywhere.** Every KPI compares against the previous equal-length window. Green/red used only here and in status dots.
7. **Honest data.** Estimates labeled "est." with methodology on hover. Insights carry confidence tags. Metrics below sample thresholds render as em-dash, not zero.
8. **Read-only, near-instant.** No save states, no modals except the command palette and date picker. Target: any filter change paints in <100ms from local SQLite.

---

## 9. Backend API sketch (for implementation planning)

```
GET /api/summary?range=30d                      → KPI strip + deltas
GET /api/timeseries/sessions?range=&by=harness  → stacked bars
GET /api/models?range=                          → comparison table
GET /api/heatmap?range=                         → hour×weekday counts
GET /api/sessions?range=&harness=&model=&project=&status=&skill=&q=&cursor=
GET /api/sessions/{id}                          → transcript + anatomy
GET /api/skills?range=                          → skill table + overhead
GET /api/insights?range=                        → precomputed insight cards
POST /api/ingest                                → trigger re-ingest
```

All queries hit the existing `~/.agentlog/agentlog.db`; the `analysis/` package precomputes insights and per-session anatomy at ingest time.

---

## 10. Build order recommendation

1. App shell + tokens + Overview KPI strip (fake data acceptable for 1 day)
2. Sessions table + filters + URL state — the workhorse
3. Session detail (transcript pane first, anatomy second)
4. Overview charts + heatmap
5. Models & Cost
6. Skills
7. Insights (depends on `analysis/` maturity — ship last, ship honest)
