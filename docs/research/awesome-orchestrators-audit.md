# Awesome Agent Orchestrators — Audit for agentlog

**Source:** [andyrewlee/awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators) (cloned 2026-08-09)  
**Purpose:** Inform architecture for **agentlog** — a unified, local-first command center over AI coding-agent activity (Codex, Claude Code, Cursor, Warp, …).  
**Method:** Static analysis only — README of the awesome-list + GitHub metadata (`gh api`) + README text of ~35 high-relevance repos. No installs, no execution of third-party code.  
**Scope note:** The list contains **169** projects across 8 active categories + Resting. Blurbs and READMEs are the primary evidence; provider/memory fields below are inferred from documented claims, not runtime verification.

---

## 1. Executive summary

The orchestration landscape splits into five product archetypes. **agentlog maps to only one of them** — the *observability / attach-to-existing-sessions* archetype — while most of the list is *spawn-and-supervise* tooling.

| Archetype | What it optimizes | agentlog fit |
|---|---|---|
| **Session discovery & triage** | Read on-disk transcripts/PTYs; show fleet status; search history | **Core** — steal patterns |
| **Parallel harness / worktree IDE** | Spawn N agents in worktrees; diff/merge; kanban | Adjacent UI patterns only |
| **Multi-agent swarm / A2A** | Inter-agent messaging, claims, org charts | Low for v1; memory-handoff ideas later |
| **Autonomous loop / task runner** | Ralph loops, issue → PR, GitHub Actions | Out of scope (execution, not observability) |
| **Personal assistant / OpenClaw family** | Always-on chat agents with memory | Different product; memory primitives only |

**Strategic takeaway for agentlog:** Do **not** become another orchestrator that owns execution. The winners closest to our thesis (CCC, agent-console, codecast) all treat **provider on-disk state as source of truth** and the dashboard as a **lens**. That matches agentlog’s existing data-inventory work (`~/.codex/sessions`, `~/.claude/projects`, Cursor `state.vscdb`).

**Top 5 to study (detailed in §3):**

1. **Claude Command Center (CCC)** — attach-to-existing + multi-engine + local dashboard  
2. **codecast** — permanent searchable session corpus + blame + inbox  
3. **agent-console** — transcript-first discovery for Codex/Claude  
4. **agent-deck** — multi-provider command center + cost/skills surfaces  
5. **guild** — local SQLite hybrid memory / handoff protocol via MCP  

**Honorable mentions:** Better Agent (FastAPI + React stack twin), octomux (permission inbox + monitor grid), diri (daemon-owned PTYs + status detection), LionClaw (`.lionclaw/` control-plane directory), ai-maestro (multi-machine + AMP messaging).

---

## 2. Landscape at a glance

| Section | Count | Dominant pattern |
|---|---:|---|
| Parallel Coding Agents — Terminal (TUI/CLI) | 14 | tmux / worktrees / session TUI |
| Parallel Coding Agents — Desktop & Web | 45 | Kanban + diff review + multi-session GUI |
| Multi-Agent Swarms | 24 | A2A messaging, hierarchies, shared boards |
| Autonomous Loop Runners | 10 | Ralph / verify-until-done |
| Autonomous Task Runners | 17 | Issue/cron → sandbox → PR |
| Agent Infrastructure & Primitives | 16 | MCP, runbooks, control planes, skills |
| Personal Assistants | 26 | Always-on OpenClaw-style agents |
| Resting (inactive) | 17 | Watchlist; vibe-kanban sunsetting |

**Licenses (GitHub SPDX):** ~91 MIT, ~37 Apache-2.0, ~11 AGPL-3.0, ~20 NOASSERTION (often source-available / custom), rest unknown/GPL.

**Common providers named in blurbs:** Claude Code, Codex, Gemini CLI, OpenCode, Cursor, Amp, Pi, Antigravity, Copilot, Aider, Grok, Kilo, Hermes.

---

## 3. Top 5 most relevant to agentlog

### 3.1 Claude Command Center (CCC) — **Highest architecture match**

| Field | Detail |
|---|---|
| Link | https://github.com/amirfish1/claude-command-center |
| Stars / License | ~114 / source-available (NOASSERTION; free non-commercial claimed in README) |
| Stack | Python local server + web dashboard; installs WatchTower queue engine |
| Providers | Claude Code, Codex, Cursor, Antigravity, Kilo, Kimi, OpenCode, Devin (spawn matrix varies) |
| Local-first? | **Yes** — loopback by default; phone/LAN opt-in |
| Memory/state | Reads `~/.claude/projects/*.jsonl`, live session registry, hooks/sidecars; FTS + optional semantic search; worker “learnings” files via WatchTower |
| Relevance | **High** |

**What it does:** Local dashboard that *attaches* to sessions already running on the machine — including ones launched outside the tool. Spawning/resume are additive. Kanban + Flow canvas + needs-you signals + cost/tier awareness.

**Why it matters for agentlog:** CCC’s manifesto is almost identical to ours: wrappers that own execution go blind the moment you use a raw terminal; the durable truth is the engines’ on-disk state. Study:

- Engine support matrix (spawn vs monitor vs transcript ingest vs follow-up)  
- SSE `/api/sessions/events` instead of polling  
- Windowed conversation load for long transcripts  
- “Needs you” derived from transcript, not process heuristics alone  
- Cursor as **metadata-only** sync (honest about limits — we face the same with `state.vscdb`)

**Caution:** Non-MIT license; installs companion tooling; broader orchestration surface than agentlog should ship in v1.

---

### 3.2 codecast — **Closest product narrative**

| Field | Detail |
|---|---|
| Link | https://github.com/codecast-sh/codecast |
| Stars / License | ~21 / MIT |
| Stack | Prebuilt CLI binary + daemon; web/desktop/phone; optional cloud sync |
| Providers | Claude Code, Codex, Cursor, Gemini (+ OpenCode/Pi planned) |
| Local-first? | **Hybrid** — daemon watches local history files; syncs to server (self-hostable) |
| Memory/state | Permanent searchable corpus; FTS + semantic; `cast ask` / `cast context` / `cast handoff`; `cast blame` line→session attribution |
| Relevance | **High** |

**What it does:** Background daemon watches provider history files and builds a durable, searchable record of every agent conversation. Live inbox (working / needs input / idle), conversation viewer with tool-call collapse, task mining, plans, workflows.

**Why it matters for agentlog:** This is the product category we are in — *see, search, and remember sessions* — not *spawn fleets*. Steal:

- Inbox priority ordering: Pinned → Working → Needs Input → Idle → Deferred  
- Parent/child sub-session hierarchy  
- Activity feed / daily digest by project  
- Auto-redaction of secrets + privacy levels  
- `cast blame` as the killer differentiation for “what did AI write?” (pairs with our Cursor AI tracking DB)  
- Cmd+K command palette over sessions (already in our dashboard design)

**Caution:** Cloud sync / team features may pull away from pure local-first; evaluate self-hosting path carefully. Installer is curl|sh (do not run blindly).

---

### 3.3 agent-console — **Canonical discovery pattern**

| Field | Detail |
|---|---|
| Link | https://github.com/buhuipao/agent-console |
| Stars / License | ~15 / Apache-2.0 |
| Stack | Rust TUI |
| Providers | Codex + Claude Code |
| Local-first? | **Yes** — fully local |
| Memory/state | Discovers sessions from providers’ own transcripts; optional isolated summarizer; archive/restore; does **not** replace native UI |
| Relevance | **High** |

**What it does:** Finds recent Codex/Claude sessions (including started elsewhere), shows working/waiting/idle/failed, alerts on approvals, resumes the **native** agent UI rather than reimplementing chat.

**Why it matters for agentlog:** Validates the “lens not runtime” design. Study:

- Workspace-grouped session list + cross-workspace search  
- Status model + jump-to-alert (`a`)  
- Config for provider command wrappers without owning the agent  
- Title = first user prompt for life (stable identity)  
- Explicit limitation: can only reconnect to processes it owns — transcript discovery ≠ live attach

**agentlog action:** Our ingest pipeline should mirror their discovery sources and status taxonomy even if our UI is a web cockpit.

---

### 3.4 agent-deck — **Multi-provider command-center UX**

| Field | Detail |
|---|---|
| Link | https://github.com/asheshgoplani/agent-deck |
| Stars / License | ~688 / MIT |
| Stack | Go TUI + optional web UI (`:8420`); tmux-backed sessions |
| Providers | Claude Code, Codex, Gemini, OpenCode (+ Pi fork support) |
| Local-first? | **Yes**; optional Telegram/Slack “conductor” for remote supervision |
| Memory/state | Session registry/state DB; fork inherits native history; skills pool + MCP manager; cost dashboard |
| Relevance | **High** |

**What it does:** Mission-control TUI for many concurrent agent sessions — groups, search, fork, worktrees, cost tracking, skills/MCP attach UI, phone-controlled conductor.

**Why it matters for agentlog:** Best-in-class *operations* UX around multi-provider fleets. Steal:

- Cost dashboard as first-class view (aligns with our Models & Cost page)  
- Skills Manager / MCP Manager as dedicated surfaces (aligns with Skills page)  
- Session fork as a first-class concept in the data model  
- Declarative groups in config.toml  
- Conductor pattern = optional later; not v1

**Caution:** Still primarily a session *launcher/multiplexer*; agentlog should borrow UX, not tmux ownership.

---

### 3.5 guild — **Cross-session memory substrate**

| Field | Detail |
|---|---|
| Link | https://github.com/mathomhaus/guild |
| Stars / License | ~310 / Apache-2.0 |
| Stack | Single Go binary; embedded SQLite; MCP server |
| Providers | Any MCP client (Claude Code, Codex, Cursor, …) |
| Local-first? | **Strictly local** — “nothing leaves your machine” |
| Memory/state | Hybrid BM25 + vector RRF search; quests (atomic claims); lore; session briefs/handoffs; project oath |
| Relevance | **High** (as a pattern / optional integration, not a competitor UI) |

**What it does:** Persistent sanctuary for amnesiac agents: `session_start` returns oath + last brief + top quest; agents claim work, inscribe lore, write parting briefs. Three write lifetimes: journal (quest), lore (durable), brief (next session).

**Why it matters for agentlog:** We already inventory MEMORY.md / Codex memories. Guild shows a clean **taxonomy of memory lifetimes** and an agent-operable MCP surface. Steal:

- Hybrid keyword+semantic search over prior sessions  
- Handoff brief as structured object linking sessions  
- Atomic claim for multi-agent (future)  
- “Appraise before research” discipline → Insights that cite prior sessions

**agentlog action:** Consider exposing read-only `agentlog_*` MCP tools that query `~/.agentlog/agentlog.db` the way guild exposes lore — without becoming a task board.

---

## 4. Patterns to adopt

### 4.1 Multi-provider orchestration / adapter layer

| Pattern | Seen in | Adopt for agentlog? |
|---|---|---|
| **On-disk state as SoT** | CCC, agent-console, codecast | **Yes — core** |
| **Per-engine capability matrix** (spawn / resume / transcript / steer) | CCC, diri | **Yes** — document honesty per harness |
| **Provider as data file, not code** | diri (JSON agent defs) | **Yes** for parsers |
| **Harness adapter + swappable sandbox** | omnigent, sandbox-agent, LionClaw | Later if we ever execute |
| **Quota-aware account rotation** | Claudexor, codecast accounts | Cost Insights only |
| **Zero-token orchestrator** (no LLM in control loop) | bernstein | Good discipline if we add automation |

### 4.2 Memory propagation across sessions/agents

| Pattern | Seen in | Adopt? |
|---|---|---|
| **Handoff / parting brief** | guild, codecast `cast handoff` | **Yes** as derived Insight / session link |
| **Layered memory lifetimes** (journal / lore / brief) | guild | **Yes** conceptually |
| **Hybrid FTS + embeddings** | guild, codecast, CCC | **Yes** for search; start FTS, add vectors later |
| **Line-level attribution (`blame`)** | codecast | **High value** with Cursor AI tracking + git |
| **Shared learnings file per queue** | CCC WatchTower | Optional |
| **Git as shared state bus** | gnap | Interesting for multiplayer; not solo local v1 |
| **MCP coordination protocol** | swarm-protocol, guild, hcom | Optional later |

### 4.3 State management

| Pattern | Seen in | Adopt? |
|---|---|---|
| **Local SQLite as system of record** | guild, sortie, Better Agent, agentlog already | **Yes** |
| **Daemon owns PTYs; UI is disposable** | diri (`dirijord`), tlbx, Better Agent runners | Only if we ever attach live; not for analytics v1 |
| **Crash restore of task/session registry** | octomux, Better Agent offline-first queue | Good for future live mode |
| **Project-scoped control dir** (`.lionclaw/`) | LionClaw | Optional sidecar; agentlog stays in `~/.agentlog` |
| **SSE / WebSocket event stream** | CCC, Better Agent | **Yes** for live dashboard refresh |
| **Windowed transcript load** | CCC | **Yes** — long Codex JSONL will hurt otherwise |

### 4.4 UI patterns

| Pattern | Seen in | Adopt? |
|---|---|---|
| **Needs-you / attention states** | CCC, diri, agent-console, codecast inbox | **Yes** on Overview |
| **Kanban over agent work** | octomux, vibe-kanban, Ouijit, openkanban | **No for v1** — we are analytics cockpit, not task board |
| **Monitor grid of terminals** | octomux | No (we don’t own PTYs) |
| **Unified permission inbox** | octomux | Only if we later attach live |
| **Cmd+K palette** | codecast, our design doc | **Yes** |
| **Cost / usage dashboard** | agent-deck, CCC, clave | **Yes** — Models & Cost |
| **Diff review workstation** | octomux, vibe-kanban, Garcon, parallel-code | Out of scope v1 |
| **Graph of session handoffs** | GraphCode, Codex `state_5.sqlite` spawn edges | **Yes** — we already have spawn edges in inventory |
| **Phone / remote lens** | agent-of-empires, clideck, Garcon, CCC | Later; keep loopback-first |

### 4.5 Plugin / skill architectures

| Pattern | Seen in | Adopt? |
|---|---|---|
| **SKILL.md + lockfile pins** | skillfold | Aligns with our skills audit |
| **YAML → SKILL.md compiler** | agent-runbook | Not needed for agentlog |
| **Portable Markdown routing defs** | sub-agents-skills | Pattern for harness adapters |
| **Out-of-process extension SDK over loopback** | Better Agent | Good if we add plugins |
| **MCP as the integration surface** | guild, swarm-protocol, diri | **Yes** — expose agentlog as MCP later |

---

## 5. Patterns to avoid

1. **Owning execution / requiring spawn-through-us** — CCC explicitly warns this goes blind for hand-launched sessions. agentlog must stay a lens.  
2. **Kanban-as-the-product** — vibe-kanban (27k★) is sunsetting; many clones compete. Don’t pivot agentlog into yet another worktree kanban.  
3. **Cloud-required sync for core value** — keep `~/.agentlog/agentlog.db` sufficient offline. Optional sync later.  
4. **Replacing native agent UIs** — agent-console’s “resume native UI” is wiser than reimplementing every harness chat.  
5. **LLM-in-the-coordination-loop for status** — bernstein’s zero-token coordination; derive status from files/events, not another model call.  
6. **AGPL / source-available contamination** — study claude-squad, tlbx, Ouijit, CCC, Better Agent for ideas; don’t copy code without license review.  
7. **curl \| bash installers & companion auto-installs** — security risk; our distribution should be explicit and auditable.  
8. **Personal-assistant / OpenClaw sprawl** — huge star counts, different problem (always-on chat). Don’t dilute the coding-agent observability mission.  
9. **File locking as hard enforcement** — swarm-protocol wisely keeps conflicts advisory; agents break locks. Prefer detect-and-surface.  
10. **Boiling the ocean of 10+ harnesses on day one** — CCC’s matrix shows uneven support; ship Codex + Claude Code + Cursor deep, then expand.

---

## 6. Specific code / architecture to study further

Static follow-ups (read-only clone + file read; do **not** install or run):

| Priority | Repo | What to open |
|---|---|---|
| P0 | `amirfish1/claude-command-center` | Session scanners, engine matrix, transcript parsers, SSE events API |
| P0 | `buhuipao/agent-console` | Provider transcript discovery paths, status derivation, config.toml providers |
| P0 | `codecast-sh/codecast` | History-file watchers, inbox taxonomy, blame pipeline, search indexing |
| P1 | `ofekron/better-agent` | FastAPI + React + WebSocket architecture (closest stack twin to our design doc) |
| P1 | `mathomhaus/guild` | SQLite schema, lore/quest/brief model, BM25+vector RRF |
| P1 | `asheshgoplani/agent-deck` | Cost dashboard, skills/MCP managers, session registry |
| P2 | `ShreyPaharia/octomux` | Permission inbox aggregation, monitor grid UX (concepts only) |
| P2 | `cristicretu/diri` | Daemon/UI split, JSON agent definitions, status-from-PTY |
| P2 | `moshthepitt/lionclaw` | `.lionclaw/` control plane layout, audit trail, runtime mounts |
| P2 | `23blocks-OS/ai-maestro` | Multi-machine mesh, Agent Messaging Protocol, memory search |
| P3 | `farol-team/gnap` | Git-native four-entity protocol (agents/tasks/runs/messages) |
| P3 | `phuryn/swarm-protocol` | Intent/claim/signal/context-package MCP tools |
| P3 | `tempestai-dev/tempest` | Shared local code-knowledge graph for token reduction |
| P3 | `AliHamzaAzam/repomon` / `codecast` | Multi-repo fleet views if we expand beyond single-machine |

---

## 7. Full catalog summary table

Relevance is scored for **agentlog** (local-first multi-harness observability), not general popularity. Providers column reflects **blurb mentions only** (many tools support more than listed).


### Parallel Coding Agents — Terminal (TUI/CLI)

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [agent-deck](https://github.com/asheshgoplani/agent-deck) | 688 | MIT | Go | Claude Code, Codex, Gemini, OpenCode | local-first | high | One TUI covering sessions across Claude Code, Codex, Gemini, and OpenCode, with live st… |
| [agent-console](https://github.com/buhuipao/agent-console) | 15 | Apache-2.0 | Rust | Claude Code, Codex | local-first | high | Rust TUI that finds Codex and Claude Code sessions from the providers' own transcripts,… |
| [agent-of-empires](https://github.com/agent-of-empires/agent-of-empires) | 3020 | MIT | Rust | Claude Code, Codex, Gemini, OpenCode, Mistral | local-first | medium | Pairs a TUI with a matching web view, so the same sessions stay reachable from a phone.… |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | 8260 | AGPL-3.0 | Go | Claude Code, Codex, OpenCode, Amp | local-first | medium | Runs each agent as a detached background session with its own worktree, so work continu… |
| [thurbox](https://github.com/Thurbeen/thurbox) | 44 | MIT | Rust | — | local-first | medium | TUI orchestrator with remote SSH sessions, inter-session messaging, and a native code-r… |
| [repomon](https://github.com/AliHamzaAzam/repomon) | 9 | Apache-2.0 | Rust | — | local-first | medium | Rust TUI that supervises a fleet across many repositories at once, in durable tmux sess… |
| [cmux](https://github.com/manaflow-ai/cmux) | 25811 | NOASSERTION | Swift | — | mostly local | low | Ghostty-based macOS terminal with vertical tabs and per-agent notifications, built for … |
| [agterm](https://github.com/umputun/agterm) | 445 | MIT | Swift | Pi | mostly local | low | Native macOS terminal with named workspaces, a live dashboard, attention states, and a … |
| [agentbox](https://github.com/madarco/agentbox) | 339 | MIT | TypeScript | — | local-first | low | Gives each agent its own sandboxed VM — local Docker or cloud via Hetzner, Daytona, Ver… |
| [amux](https://github.com/andyrewlee/amux) | 145 | MIT | Go | — | local-first | low | Minimal TUI for spawning parallel coding agents in git worktrees. |
| [openkanban](https://github.com/TechDufus/openkanban) | 130 | AGPL-3.0 | Go | — | mostly local | low | Kanban board for orchestrating coding agents, rendered entirely in the terminal. |
| [herdr](https://github.com/ogulcancelik/herdr) | 26078 | Apache-2.0 | Rust | — | mostly local | low | Agent-aware multiplexer with persistent workspaces, tabs, panes, and status detection f… |
| [dmux](https://github.com/standardagents/dmux) | 1732 | MIT | HTML | — | local-first | low | Dev agent multiplexer pairing coding agents with git worktrees over tmux. |
| [tmux-ide](https://github.com/wavyrai/tmux-ide) | 535 | MIT | TypeScript | — | local-first | low | Turns any project into a tmux IDE from a checked-in `ide.yml`, including preset agent-t… |

### Parallel Coding Agents — Desktop & Web

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [Claude Command Center (CCC)](https://github.com/amirfish1/claude-command-center) | 114 | NOASSERTION | Python | Claude Code, Codex, Cursor, Antigravity, Kilo | local-first | high | Local dashboard for spawning, monitoring, and resuming sessions across Claude Code, Cod… |
| [Better Agent](https://github.com/ofekron/better-agent) | 53 | NOASSERTION | Python | Claude Code, Codex, Gemini | local-first | high | Local web workspace with persistent state, approvals, and restart recovery for native C… |
| [ai-maestro](https://github.com/23blocks-OS/ai-maestro) | 744 | MIT | TypeScript | Claude Code, Cursor, Aider | mostly local | high | Dashboard spanning multiple machines, adding memory search, code-graph queries, and age… |
| [octomux](https://github.com/ShreyPaharia/octomux) | 21 | MIT | TypeScript | — | local-first | high | Local dashboard with a kanban fleet view, one unified permission inbox across agents, a… |
| [diri](https://github.com/cristicretu/diri) | 229 | Apache-2.0 | Rust | Claude Code, Codex, Gemini, Cursor | local-first | high | Native macOS app running Claude Code, Codex, Cursor, Gemini, and shells in parallel acr… |
| [clideck](https://github.com/rustykuntz/clideck) | 150 | MIT | JavaScript | Claude Code, Codex, Gemini, OpenCode | mostly local | high | Chat-app-style dashboard with autopilot routing between agents and full control from a … |
| [tlbx](https://github.com/tlbx-ai/tlbx) | 100 | AGPL-3.0 | C# | — | local-first | high | Self-hosted browser workspace holding persistent real PTY sessions on your own machines… |
| [Tempest](https://github.com/tempestai-dev/tempest) | 35 | Apache-2.0 | TypeScript | — | local-first | high | Tauri desktop ADE running CLI agents in parallel isolated worktrees, with a shared loca… |
| [Garcon](https://github.com/cfal/garcon) | 51 | NOASSERTION | TypeScript | — | local-first | medium | Self-hosted browser and mobile workspace with diff review, Git/PR workflows, mobile app… |
| [nimbalyst](https://github.com/nimbalyst/nimbalyst) | 1434 | MIT | TypeScript | Claude Code, Codex, OpenCode | local-first | medium | Visual workspace pairing parallel worktree sessions with kanban and direct visual editi… |
| [Ouijit](https://github.com/ouijit/ouijit) | 141 | AGPL-3.0 | TypeScript | Claude Code, Codex, OpenCode, Pi | local-first | medium | Kanban board and terminals wired together by lifecycle hooks, scripts, and a session-aw… |
| [Proliferate](https://github.com/proliferate-ai/proliferate) | 161 | AGPL-3.0 | TypeScript | — | local-first | medium | Agent IDE that runs sessions locally or in the cloud and lets you build reusable workfl… |
| [clave](https://github.com/codika-io/clave) | 45 | MIT | TypeScript | Claude Code | local-first | medium | Native macOS app with split and grid layouts, session groups, SSH remote sessions, and … |
| [GraphCode](https://github.com/scgopi/GraphCode) | 12 | NOASSERTION | Swift | Claude Code, Codex, Copilot | local-first | medium | macOS app that wires agent sessions into a graph: each node is a live terminal you can … |
| [CodeNomad](https://github.com/NeuralNomadsAI/CodeNomad) | 2454 | MIT | TypeScript | OpenCode | local-first | low | Desktop and web workspace around the OpenCode CLI whose SideCars embed local tools like… |
| [IM.codes](https://github.com/im4codes/imcodes) | 1085 | MIT | TypeScript | Claude Code, Codex, Gemini | local-first | low | Mobile and web control layer built for away-from-desk continuation, with terminal acces… |
| [supacode](https://github.com/supabitapp/supacode) | 2252 | NOASSERTION | Swift | — | local-first | low | Native macOS command center for worktree-per-agent development. |
| [Traycer](https://github.com/traycerai/traycer) | 1140 | MIT | TypeScript | — | mostly local | low | Bring-your-own-agent workspace running many sessions in parallel with context shared ac… |
| [takopi](https://github.com/banteg/takopi) | 1038 | MIT | Python | Claude Code, Codex, OpenCode, Pi | mostly local | low | Telegram bridge that puts Codex, Claude Code, OpenCode, and Pi sessions in a chat thread. |
| [kandev](https://github.com/kdlbs/kandev) | 558 | AGPL-3.0 | Go | — | local-first | low | Kanban workbench whose multi-step workflows assign a different agent per step behind hu… |
| [aizen](https://github.com/vivy-company/aizen) | 295 | GPL-3.0 | Swift | — | local-first | low | macOS workspace that organizes worktrees, environments, and agent sessions per project. |
| [jat](https://github.com/joewinke/jat) | 248 | MIT | Svelte | — | mostly local | low | Visual dashboard combining live sessions, task management, code editor, and terminal, w… |
| [ivy-tendril](https://github.com/Ivy-Interactive/Ivy-Tendril) | 170 | NOASSERTION | C# | Claude Code, Codex, OpenCode, Copilot, Ant… | mostly local | low | Drives agents through a plan-based lifecycle with verification gates, self-improving me… |
| [t3code](https://github.com/pingdotgg/t3code) | 17454 | MIT | TypeScript | Claude Code, Codex, OpenCode, Cursor, Grok | local-first | low | Harness control surface available as web, mobile, and desktop app. Claude Code, Codex, … |
| [synara](https://github.com/Emanuele-web04/synara) | 1516 | MIT | TypeScript | — | local-first | low | GUI desktop workspace for running and managing agents across local projects. |
| [jean](https://github.com/coollabsio/jean) | 1177 | Apache-2.0 | TypeScript | Claude Code, Codex, OpenCode | local-first | low | Desktop and web app for orchestrating agents across multiple projects and their git wor… |
| [parallel-code](https://github.com/johannesjo/parallel-code) | 922 | MIT | TypeScript | Claude Code, Codex, Gemini | local-first | low | Desktop app running Claude Code, Codex, and Gemini CLI side by side in isolated worktre… |
| [Fletch](https://github.com/fwdai/fletch) | 15 | AGPL-3.0 | Rust | Claude Code, Codex, OpenCode, Cursor | mostly local | low | Native macOS IDE that seals each agent in its own repo clone under Seatbelt or Docker, … |
| [automaker](https://github.com/AutoMaker-Org/automaker) | 3212 | NOASSERTION | TypeScript | — | local-first | low | Describe features on a Kanban board and agents implement them in isolated worktrees, ru… |
| [dorothy](https://github.com/Charlie85270/Dorothy) | 333 | MIT | TypeScript | — | local-first | low | Desktop app combining agent orchestration with automations, Kanban management, and MCP … |
| [Orca](https://github.com/stablyai/orca) | 40437 | MIT | TypeScript | — | local-first | low | Agentic development environment for running a fleet on your own subscription, available… |
| [Aperant](https://github.com/AndyMik90/Aperant) | 14505 | AGPL-3.0 | TypeScript | — | mostly local | low | Runs up to 12 agent terminals with a self-validating QA loop and automatic conflict res… |
| [superset](https://github.com/superset-sh/superset) | 12829 | NOASSERTION | TypeScript | — | local-first | low | Code editor built around running many agents on your machine at once. |
| [qm](https://github.com/yc-software/qm) | 12620 | MIT | TypeScript | — | mostly local | low | Multiplayer harness where each teammate gets an isolated workspace to run agents indepe… |
| [humanlayer](https://github.com/humanlayer/humanlayer) | 11222 | NOASSERTION | TypeScript | — | mostly local | low | Human-in-the-loop control for coding agents on hard problems; the repo notes its code i… |
| [agent-orchestrator](https://github.com/Untrivial-ai/agent-orchestrator) | 8974 | Apache-2.0 | Go | — | mostly local | low | Agent IDE for fleets that plans the work, spawns the agents, then fixes CI failures and… |
| [Emdash](https://github.com/generalaction/emdash) | 5369 | Apache-2.0 | TypeScript | — | mostly local | low | Agentic development environment running parallel agents against any model provider. |
| [collaborator](https://github.com/collabs-inc/collab-public) | 2850 | NOASSERTION | TypeScript | — | mostly local | low | Arranges terminals, editors, and files as tiles on an infinite pan-and-zoom canvas inst… |
| [mux](https://github.com/coder/mux) | 1965 | AGPL-3.0 | TypeScript | — | local-first | low | Desktop app for isolated, parallel agentic development. |
| [bb](https://github.com/get-bb/bb) | 1499 | MIT | TypeScript | Pi | local-first | low | Self-controlling agentic IDE that orchestrates multiple coding agents in live threads y… |
| [vibe-tree](https://github.com/sahithvibudhi/vibe-tree) | 263 | MIT | TypeScript | — | local-first | low | One git worktree per agent, delivered as desktop, web, and CLI. |
| [constellagent](https://github.com/owengretzinger/constellagent) | 214 | unknown | TypeScript | — | local-first | low | macOS app giving each agent its own terminal, editor, and git worktree in a single window. |
| [vibecraft](https://github.com/rayzhudev/vibecraft) | 31 | Apache-2.0 | TypeScript | — | mostly local | low | RTS-style workspace for commanding coding agents. |
| [AGX](https://github.com/ramarlina/agx) | 27 | unknown | TypeScript | — | mostly local | low | Wake-work-sleep checkpointing keeps a persistent agent team on long objectives, with hu… |
| [agent-squid](https://github.com/agent-squid/squid) | 10 | MIT | JavaScript | — | mostly local | low | Browser UI organized into named lanes (`#topic@agent`), with context shared across agen… |

### Multi-Agent Swarms

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [hcom](https://github.com/aannoo/hcom) | 429 | MIT | Rust | Claude Code, Codex, OpenCode, Cursor, Anti… | mostly local | medium | Lets agents message, watch, and spawn each other across terminals. Claude Code, Codex, … |
| [gastown](https://github.com/gastownhall/gastown) | 17523 | MIT | Go | — | mostly local | low | Scales to 20-30 agents with a coordinator, git-backed issue tracking, health watchdogs,… |
| [ClawTeam](https://github.com/HKUDS/ClawTeam) | 5482 | MIT | Python | — | local-first | low | Agents spawn and manage their own teammates from one command, coordinating through file… |
| [claude_codex_bridge](https://github.com/SeemSeam/claude_codex_bridge) | 3385 | NOASSERTION | Python | — | mostly local | low | Workspace for mixing different vendors' CLI agents in one visible collaboration session. |
| [Orkas](https://github.com/Orkas-AI/Orkas) | 1110 | MIT | TypeScript | Claude Code, Codex, OpenCode, Cline | mostly local | low | A commander agent decomposes goals and dispatches specialists with isolated skills and … |
| [NXTG-Forge Orchestrator](https://github.com/nxtg-ai/forge-orchestrator) | 134 | NOASSERTION | Rust | Claude Code, Codex, Gemini | mostly local | low | Coordinates Claude Code, Codex, and Gemini CLI on one shared repo through a research-pl… |
| [paperclip](https://github.com/paperclipai/paperclip) | 75962 | MIT | TypeScript | — | local-first | low | Self-hosted platform where agents wake on heartbeats to claim tickets, governed by org … |
| [buzz](https://github.com/block/buzz) | 25385 | Apache-2.0 | Rust | Claude Code, Codex, Goose | mostly local | low | Agents are first-class members of shared channels on a Nostr relay you own, with their … |
| [agentsmesh](https://github.com/AgentsMesh/AgentsMesh) | 2309 | NOASSERTION | Go | Claude Code, Codex, Gemini, OpenCode, Aider | local-first | low | Remote AI workstations with PTY sandboxes and worktree isolation, coordinating across c… |
| [agent-kanban](https://github.com/saltbo/agent-kanban) | 438 | NOASSERTION | TypeScript | Claude Code, Codex, Gemini | mostly local | low | Leader-worker task board with cryptographic agent identity. Claude Code, Codex, Gemini … |
| [ORCH](https://github.com/oxgeneral/ORCH) | 132 | MIT | TypeScript | Claude Code, Codex, Cursor | mostly local | low | CLI runtime managing agents as typed teams with an explicit state machine and goals. Cl… |
| [kodo](https://github.com/ikamensh/kodo) | 126 | MIT | Python | Claude Code, Codex, Gemini | mostly local | low | Directs agents through work cycles where a separate agent independently verifies each r… |
| [shire](https://github.com/victor36max/shire) | 37 | MIT | TypeScript | Claude Code, OpenCode, Pi | mostly local | low | Persistent team workspaces with inter-agent mailboxes and a shared drive. Claude Code, … |
| [Agent Teams](https://github.com/777genius/agent-teams-ai) | 1888 | AGPL-3.0 | TypeScript | — | local-first | low | Desktop app where teams take a high-level command and handle it themselves via inter-ag… |
| [Fusion](https://github.com/Runfusion/Fusion) | 1083 | MIT | TypeScript | — | local-first | low | Multi-node orchestrator with a kanban board, plan-review-execute gates, per-task worktr… |
| [Agon](https://github.com/AutoResearch-Factory/Agon) | 38 | MIT | Python | — | mostly local | low | Orchestrates scientist, coder, and auditor loops from research topic through proposal t… |
| [ruflo](https://github.com/ruvnet/ruflo) | 67433 | MIT | TypeScript | Claude Code | mostly local | low | Meta-harness for deploying coordinated swarms and conversational multi-agent workflows.… |
| [scion](https://github.com/GoogleCloudPlatform/scion) | 1665 | Apache-2.0 | Go | — | mostly local | low | Orchestration testbed running agents in parallel isolated containers with dynamic coord… |
| [multi-agent-shogun](https://github.com/yohey-w/multi-agent-shogun) | 1410 | MIT | Shell | Pi | local-first | low | Shogun to karo to ashigaru hierarchy running up to 10 agents over tmux with no coordina… |
| [loki-mode](https://github.com/asklokesh/loki-mode) | 1032 | NOASSERTION | Shell | — | mostly local | low | PRD-to-deployed-product SDLC with 41 agents in 8 swarms, nine quality gates, and blind … |
| [tutti](https://github.com/nutthouse/tutti) | 110 | MIT | Rust | — | local-first | low | Config-driven workflows passing typed artifacts between agents, each in its own worktree. |
| [CompanyHelm](https://github.com/CompanyHelm/companyhelm) | 73 | MIT | TypeScript | — | mostly local | low | Distributed orchestrator with task management and direct agent-to-agent conversations. |
| [5dive](https://github.com/5dive-ai/5dive) | 38 | MIT | Shell | Claude Code, Codex, OpenCode, Antigravity,… | mostly local | low | Named agents on a shared org chart and backlog hand work to each other and escalate to … |
| [orc](https://github.com/spencermarx/orc) | 22 | unknown | Shell | — | local-first | low | Lightweight framework that piggybacks your existing CLI setup for planning, task decomp… |

### Autonomous Loop Runners

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [ralph-tui](https://github.com/subsy/ralph-tui) | 2419 | MIT | TypeScript | — | local-first | medium | Drives an agent through a task list autonomously, with a TUI for watching the loop. |
| [ralphex](https://github.com/umputun/ralphex) | 1420 | MIT | Go | Claude Code, Codex | mostly local | medium | Executes an implementation plan autonomously with a fresh session per task, plus valida… |
| [ralph-claude-code](https://github.com/frankbria/ralph-claude-code) | 9594 | MIT | Shell | Claude Code | mostly local | low | Development loop for Claude Code with exit detection that recognizes when the work is a… |
| [LoopTroop](https://github.com/looptroop-ai/LoopTroop) | 116 | MIT | TypeScript | OpenCode | local-first | low | An LLM council plans the work, then Ralph-style loops retry failed units with fresh con… |
| [toryo](https://github.com/JesseRWeigel/toryo) | 12 | MIT | TypeScript | Claude Code, Gemini, Aider, Ollama | mostly local | low | Trust-based delegation with quality ratcheting that commits improvements and reverts re… |
| [Dex](https://github.com/francescoalemanno/dex) | 21 | MIT | Rust | — | mostly local | low | Human-gated planning, multi-reviewer code review, and dead-end-aware research loops, sh… |
| [ralph-orchestrator](https://github.com/mikeyobrien/ralph-orchestrator) | 3095 | MIT | Rust | — | mostly local | low | Hat-based orchestration that keeps agents looping until done, as a fuller implementatio… |
| [bernstein](https://github.com/sipyourdrink-ltd/bernstein) | 812 | Apache-2.0 | Python | — | mostly local | low | Keeps no model in the coordination loop, so orchestration costs zero tokens. Verifies w… |
| [fractal](https://github.com/plasma-ai/fractal) | 682 | Apache-2.0 | Python | — | mostly local | low | Loops that recursively delegate separable subtasks to child agents, bounded by configur… |
| [MartinLoop](https://github.com/Keesan12/martin-loop) | 43 | Apache-2.0 | TypeScript | — | mostly local | low | Caps spend, enforces policy, verifies output, and rolls back failures, leaving inspecta… |

### Autonomous Task Runners

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [cyrus](https://github.com/cyrusagents/cyrus) | 752 | Apache-2.0 | TypeScript | Claude Code, Codex, Gemini, Cursor | local-first | medium | Watches Linear, GitHub, GitLab, and Slack issues assigned to it, spinning up an isolate… |
| [Contrabass](https://github.com/junhoyeo/contrabass) | 212 | Apache-2.0 | Go | — | local-first | medium | Terminal-first orchestrator for issue-driven agent runs, pulling work from Linear, GitH… |
| [sortie](https://github.com/sortie-ai/sortie) | 120 | Apache-2.0 | Go | — | local-first | medium | Turns tracker tickets into agent sessions. Agent-agnostic and tracker-agnostic, as a si… |
| [OpenHands](https://github.com/OpenHands/OpenHands) | 83501 | MIT | TypeScript | Claude Code, Codex | local-first | low | Self-hostable control center running its own agent or driving Claude Code, Codex, and a… |
| [run-gemini-cli](https://github.com/google-github-actions/run-gemini-cli) | 2051 | Apache-2.0 | TypeScript | Gemini | cloud/hybrid | low | Google's official GitHub Action, running on event or schedule triggers or on demand via… |
| [background-agents](https://github.com/ColeMurray/background-agents) | 2642 | MIT | TypeScript | — | cloud/hybrid | low | Sessions trigger from a web UI, Slack, GitHub, Linear, webhooks, or cron, run in Modal,… |
| [remote-swe-agents](https://github.com/aws-samples/remote-swe-agents) | 241 | MIT-0 | TypeScript | — | cloud/hybrid | low | Serverless control plane on Lambda with a dedicated EC2 worker per session, triggered b… |
| [centaur](https://github.com/paradigmxyz/centaur) | 974 | NOASSERTION | Python | — | local-first | low | Multiplayer self-hosted agents with Slack-native conversations, Kubernetes sandboxes, s… |
| [aeon](https://github.com/aeonfun/aeon) | 623 | MIT | TypeScript | Claude Code, Codex, Pi, Grok | cloud/hybrid | low | Runs unattended on GitHub Actions; dispatches skills to six coding-agent harnesses behi… |
| [Factory](https://github.com/owainlewis/factory) | 150 | MIT | Go | Codex | mixed/cloud | low | Keeps coding agents working on a repository without making a human orchestrate every st… |
| [gh-aw](https://github.com/github/gh-aw) | 4896 | MIT | Go | Claude Code, Codex, Gemini, Copilot | cloud/hybrid | low | Compiles agentic workflows written in Markdown into GitHub Actions YAML. Read-only by d… |
| [codex-action](https://github.com/openai/codex-action) | 1163 | Apache-2.0 | TypeScript | Codex | cloud/hybrid | low | OpenAI's official GitHub Action, running Codex CLI headlessly under drop-sudo, unprivil… |
| [symphony](https://github.com/openai/symphony) | 26491 | Apache-2.0 | Elixir | — | mixed/cloud | low | Turns project work into isolated autonomous runs, so teams manage the work rather than … |
| [lalph](https://github.com/tim-smart/lalph) | 130 | MIT | TypeScript | — | mixed/cloud | low | Orchestrator driven by whichever source of issues you point it at. |
| [multica](https://github.com/multica-ai/multica) | 44853 | NOASSERTION | Go | — | cloud/hybrid | low | Managed agents platform where you assign tasks, track progress, and let agents compound… |
| [open-swe](https://github.com/langchain-ai/open-swe) | 10518 | MIT | Python | — | cloud/hybrid | low | Invoked from Slack, Linear, or GitHub comments; each task runs in its own cloud sandbox… |
| [claude-code-action](https://github.com/anthropics/claude-code-action) | 8565 | MIT | TypeScript | Pi | cloud/hybrid | low | Anthropic's official GitHub Action, detecting from context whether to answer, review, o… |

### Agent Infrastructure & Primitives

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [codecast](https://github.com/codecast-sh/codecast) | 21 | MIT | TypeScript | Claude Code, Codex, Gemini, Cursor | local-first | high | Watches your real local sessions and surfaces them in a live triage inbox, keeping a se… |
| [guild](https://github.com/mathomhaus/guild) | 310 | Apache-2.0 | Go | — | local-first | medium | Shared context, memory, and task coordination as a single Go binary over local SQLite w… |
| [LionClaw](https://github.com/moshthepitt/lionclaw) | 13 | MIT | Rust | — | local-first | medium | Local control plane running coding agents as durable, auditable workers with explicit s… |
| [handoff](https://github.com/dazuiba/handoff) | 82 | unknown | Python | Claude Code, Codex | mostly local | medium | Delegates a task to DeepSeek, Codex, or Claude from inside your current Claude Code or … |
| [neuralyzer](https://github.com/gintasz/neuralyzer) | 38 | MIT | TypeScript | — | mostly local | low | Lets an agent wipe its own session context and re-run the first message, making Ralph l… |
| [Archon](https://github.com/coleam00/Archon) | 23112 | MIT | TypeScript | Claude Code, Codex | local-first | low | Harness builder for deterministic AI coding workflows, combining agent steps with scrip… |
| [omnigent](https://github.com/omnigent-ai/omnigent) | 8415 | Apache-2.0 | Python | Claude Code, Codex, OpenCode, Cursor, Pi, … | mostly local | low | Meta-harness running Claude Code, Codex, Cursor, OpenCode, Hermes, Pi, or custom YAML a… |
| [agent-runbook](https://github.com/KnoxOps/agent-runbook) | 17 | Apache-2.0 | Python | Claude Code, Codex | mostly local | low | Compiles YAML runbooks with loops, branching, and parallelism into SKILL.md files for C… |
| [skillfold](https://github.com/byronxlg/skillfold) | 12 | MIT | TypeScript | Claude Code, Codex | mostly local | low | Declares skills in YAML and pins exact revisions in a lockfile so installs are reproduc… |
| [Agentlas OS](https://github.com/agentlas-ai/Agentlas-OS) | 1150 | Apache-2.0 | Python | — | mostly local | low | Keeps specialist agents in a hub and spins up a temporary orchestrator per task, with A… |
| [NemoClaw](https://github.com/NVIDIA/NemoClaw) | 22107 | Apache-2.0 | TypeScript | Hermes | cloud/hybrid | low | Runs Hermes, LangChain Deep Agents, and OpenClaw inside NVIDIA OpenShell with managed i… |
| [openfang](https://github.com/RightNow-AI/openfang) | 18089 | Apache-2.0 | Rust | — | mostly local | low | Open-source agent operating system. |
| [sandbox-agent](https://github.com/rivet-dev/sandbox-agent) | 1526 | Apache-2.0 | TypeScript | Pi | cloud/hybrid | low | Daemon, HTTP/SSE API, and TypeScript SDK for driving six coding agents inside E2B, Dayt… |
| [Claudexor](https://github.com/razzant/claudexor) | 382 | MIT | TypeScript | — | mostly local | low | Routes one coding thread across harnesses with quota-aware rotation between subscriptio… |
| [sub-agents-skills](https://github.com/shinpr/sub-agents-skills) | 76 | MIT | Python | — | mostly local | low | Portable Markdown definitions that route a task to a chosen backend, model, effort leve… |
| [agenttier](https://github.com/agenttier/agenttier) | 72 | Apache-2.0 | Go | Pi | cloud/hybrid | low | Kubernetes runtime giving each agent its own Pod and PVC sandbox behind a default-deny … |

### Personal Assistants

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [hermes-agent](https://github.com/NousResearch/hermes-agent) | 227669 | MIT | Python | — | mixed | low | Self-improving harness with persistent cross-session memory and auto-generated skill do… |
| [nanobot](https://github.com/HKUDS/nanobot) | 46783 | MIT | Python | — | local-first | low | Ultra-lightweight self-hosted assistant in Python with WebUI, tools, memory, MCP, and m… |
| [rho](https://github.com/mikeyobrien/rho) | 369 | MIT | TypeScript | — | mixed | low | Stays running, remembers across sessions, and checks in on its own. macOS, Linux, Android. |
| [iva](https://github.com/smixs/iva) | 137 | MIT | TypeScript | — | local-first | low | Telegram assistant that turns your messages, voice notes and photos into an Obsidian-co… |
| [lucinate](https://github.com/lucinate-ai/lucinate) | 11 | Apache-2.0 | Go | Ollama, Hermes | mixed | low | Terminal-native chat client for OpenClaw, Hermes, Ollama, and OpenAI-compatible provide… |
| [Cloudflare OS](https://github.com/cloudflare/cloudflare-os) | 6864 | Apache-2.0 | TypeScript | — | local-first | low | Self-hostable "company OS" on Cloudflare Workers: a chat UI where agents preloaded with… |
| [lemon](https://github.com/z80dev/lemon) | 130 | MIT | Elixir | — | local-first | low | Local-first assistant and coding agent runtime. |
| [rowboat](https://github.com/rowboatlabs/rowboat) | 17048 | Apache-2.0 | TypeScript | — | mixed | low | Open-source AI coworker with memory. |
| [lobsterai](https://github.com/netease-youdao/LobsterAI) | 5825 | MIT | TypeScript | — | local-first | low | Desktop-grade agent for data analysis, slides, docs, and web research. |
| [Ouroboros](https://github.com/razzant/ouroboros) | 1015 | MIT | Python | — | local-first | low | General-purpose agent with durable identity and memory, reviewed self-modification, mul… |
| [Hivekeep](https://github.com/MarlBurroW/hivekeep) | 42 | MIT | TypeScript | — | local-first | low | Self-hosted team of specialized agents with persistent memory that delegate and build t… |
| [openclaw](https://github.com/openclaw/openclaw) | 385616 | NOASSERTION | TypeScript | — | mixed | low | Your own personal AI assistant, on any OS and any platform. |
| [QwenPaw](https://github.com/agentscope-ai/QwenPaw) | 34257 | Apache-2.0 | Python | — | local-first | low | Personal assistant that deploys to your own machine or the cloud and plugs into multipl… |
| [zeroclaw](https://github.com/zeroclaw-labs/zeroclaw) | 32541 | Apache-2.0 | Rust | — | mixed | low | Fast, small, fully autonomous assistant infrastructure in Rust, deployable anywhere. |
| [picoclaw](https://github.com/sipeed/picoclaw) | 29840 | MIT | Go | — | mixed | low | Tiny and fast assistant deployable anywhere. |
| [ironclaw](https://github.com/nearai/ironclaw) | 12596 | Apache-2.0 | Rust | — | mixed | low | Agent OS in Rust focused on privacy, security, and extensibility. |
| [Coworker](https://github.com/accomplish-ai/coworker) | 10948 | MIT | TypeScript | — | local-first | low | Open source AI coworker that lives on your desktop. Formerly accomplish. |
| [nullclaw](https://github.com/nullclaw/nullclaw) | 8010 | MIT | Zig | — | mixed | low | Fully autonomous assistant infrastructure written in Zig. |
| [MetaClaw](https://github.com/aiming-lab/MetaClaw) | 3518 | MIT | Python | — | mixed | low | Assistant that learns and evolves from conversation alone. |
| [automata](https://github.com/sentientwave/automata) | 109 | NOASSERTION | Elixir | — | mixed | low | Matrix-native workspace where Temporal-backed durable workflows survive restarts and ke… |
| [ghostclaw](https://github.com/b1rdmania/ghostclaw) | 90 | MIT | TypeScript | — | mixed | low | An AI that lives on your computer and does things for you. |
| [assistant](https://github.com/kcosr/assistant) | 89 | unknown | TypeScript | — | mixed | low | Panel-based assistant whose plugins share one workspace of notes, lists, and objects. |
| [nanoclaw](https://github.com/nanocoai/nanoclaw) | 30473 | MIT | TypeScript | — | mixed | low | Lightweight OpenClaw alternative running in containers, connecting to WhatsApp, Telegra… |
| [leon](https://github.com/leon-ai/leon) | 17426 | MIT | TypeScript | — | mixed | low | Long-running open-source personal assistant with voice and text interfaces. |
| [zclaw](https://github.com/tnm/zclaw) | 2204 | MIT | C | — | mixed | low | Complete personal assistant in 888 KiB, running on an ESP32 with GPIO, cron, and custom… |
| [denchclaw](https://github.com/DenchHQ/DenchClaw) | 1642 | MIT | TypeScript | — | cloud/hybrid | low | Managed OpenClaw framework aimed at CRM, sales automation, and outreach. |

### Resting

| Name | Stars | License | Lang | Providers (from blurb) | Local? | Relevance | One-line |
|---|---:|---|---|---|---|---|---|
| [vibe-kanban](https://github.com/BloopAI/vibe-kanban) | 27713 | Apache-2.0 | Rust | — | unknown | medium | Kanban board for managing AI coding agents. _(last commit 2026-04)_ |
| [CodexMonitor](https://github.com/Dimillian/CodexMonitor) | 4221 | MIT | TypeScript | Codex | local-first | low | Orchestrate multiple Codex agents across local workspaces. _(last commit 2026-03)_ |
| [swarm-protocol](https://github.com/phuryn/swarm-protocol) | 53 | MIT | TypeScript | — | unknown | low | Headless coordination over MCP: claim work, detect file conflicts, heartbeat, and hand … |
| [1code](https://github.com/21st-dev/1code) | 5619 | Apache-2.0 | TypeScript | Claude Code, Codex | unknown | low | Orchestration layer for Claude Code and Codex. _(last commit 2026-03; archived)_ |
| [ralphy](https://github.com/michaelshimeles/ralphy) | 2958 | unknown | TypeScript | Claude Code, Codex, OpenCode, Cursor | unknown | low | Bash script that loops Claude Code, Codex, OpenCode, Cursor, Qwen, or Droid until the t… |
| [opengoat](https://github.com/marian2js/opengoat) | 376 | MIT | TypeScript | Claude Code, Codex, OpenCode, Cursor | unknown | low | Build organizations of OpenClaw agents coordinating across Codex, Claude Code, Cursor, … |
| [antfarm](https://github.com/snarktank/antfarm) | 2494 | MIT | TypeScript | — | unknown | low | Build your agent team in OpenClaw with one command. _(last commit 2026-02)_ |
| [cashclaw](https://github.com/moltlaunch/cashclaw) | 1092 | MIT | TypeScript | — | unknown | low | An autonomous agent that takes work, does work, gets paid, and gets better at it. _(las… |
| [clawe](https://github.com/getclawe/clawe) | 747 | AGPL-3.0 | TypeScript | — | unknown | low | Multi-agent coordination system: think Trello for OpenClaw agents. _(last commit 2026-02)_ |
| [ariana](https://github.com/ariana-dot-dev/ariana) | 351 | unknown | TypeScript | — | unknown | low | The IDE of the future. _(last commit 2026-03)_ |
| [subtask](https://github.com/zippoxer/subtask) | 338 | MIT | Go | Claude Code | local-first | low | Claude Skill that runs your tasks through subagents in git worktrees. _(last commit 202… |
| [lettabot](https://github.com/letta-ai/lettabot) | 327 | Apache-2.0 | TypeScript | — | unknown | low | Personal assistant that remembers everything. _(last commit 2026-05; archived, replaced… |
| [mercury](https://github.com/Michaelliv/mercury) | 145 | unknown | TypeScript | — | unknown | low | Personal AI assistant that lives where you chat. _(last commit 2026-03; archived)_ |
| [babyagi3](https://github.com/yoheinakajima/babyagi3) | 129 | MIT | Python | — | unknown | low | A minimal AI agent you configure once, then run through natural language. _(last commit… |
| [wreckit](https://github.com/mikehostetler/wreckit) | 129 | MIT | Elixir | — | unknown | low | Run the Ralph Wiggum loop over your roadmap. _(last commit 2026-04)_ |
| [gnap](https://github.com/farol-team/gnap) | 78 | MIT | ? | — | unknown | low | Git-native agent protocol coordinating agents through a shared repo as a task board, wi… |
| [wit](https://github.com/amaar-mc/wit) | 46 | MIT | TypeScript | — | unknown | low | Locks individual functions rather than files via Tree-sitter, warning agents of conflic… |

---

## 8. Per-entry notes (abbreviated by category)

Fields below are compact. For stars/license/lang see §7. “OS?” = open source with a standard permissive SPDX unless noted.

### 8.1 Parallel Coding Agents — Terminal

| Name | Does | Multi-provider | Memory/state | Local? | OS/License | Rel. |
|---|---|---|---|---|---|---|
| agent-console | Discover+resume Codex/Claude from own transcripts | Codex, Claude | Transcript discovery, summaries | Local | Apache-2.0 | **High** |
| agent-deck | TUI/web mission control | Claude, Codex, Gemini, OpenCode | Registry DB, forks, cost | Local | MIT | **High** |
| agent-of-empires | TUI + phone web | Many CLIs | Session reachability | Local | MIT | Med |
| agentbox | Per-agent sandbox VMs | Any via sandbox | Checkpoints | Hybrid | MIT | Low |
| agterm | macOS terminal workspaces | CLI agents | Dashboard/API | Local | MIT | Low |
| amux | Minimal worktree spawner | Configurable | Worktrees | Local | MIT | Low |
| claude-squad | Detached bg sessions + worktrees | Claude, Codex, OpenCode, Amp | Session persistence | Local | AGPL | Med |
| cmux | Ghostty tabs + notifications | Agent-aware | Tabs/panes | Local | Custom | Low |
| dmux | tmux + worktrees multiplexer | Coding agents | tmux state | Local | MIT | Low |
| herdr | Agent-aware mux + status | CLI agents | Persistent workspaces | Local | Apache-2.0 | Low |
| openkanban | Terminal kanban | Agents | Board state | Local | AGPL | Low |
| repomon | Multi-repo fleet TUI | tmux fleet | Durable sessions | Local | Apache-2.0 | Med |
| thurbox | SSH + messaging + review TUI | Any CLI | Inter-session msg | Local | MIT | Med |
| tmux-ide | `ide.yml` team layouts | Preset agents | Checked-in config | Local | MIT | Low |

### 8.2 Parallel Coding Agents — Desktop & Web (highlights)

Highest agentlog relevance already covered in §3. Additional notables:

| Name | Why glance | Rel. |
|---|---|---|
| Better Agent | FastAPI+React+WS; persistent local state; offline queue | **High** |
| octomux | Permission inbox, monitor grid, kanban, MIT local | **High** |
| diri | Daemon PTY ownership; status detection; MCP spawn | **High** |
| ai-maestro | Multi-machine + memory search + A2A | **High** |
| Tempest | Shared local code-knowledge graph | High |
| tlbx | Self-hosted real PTY in browser | High |
| clideck | Chat-app dashboard + phone | High |
| Garcon | Self-hosted diff/PR/mobile approvals | Med |
| t3code | Popular harness control surface (web/mobile/desktop) | Low* |
| Orca / Traycer / Proliferate | Fleet IDEs; more orchestrator than observer | Low–Med |
| vibe-kanban (Resting) | Category-defining kanban; **sunsetting** | Med (study UX only) |

\*Low for agentlog despite stars: control surface / launcher, not transcript analytics.

### 8.3 Multi-Agent Swarms (pattern value)

| Name | Pattern to note | Rel. |
|---|---|---|
| hcom | Cross-terminal message/watch/spawn | Med |
| paperclip | Heartbeat claim tickets, org charts, budgets | Low |
| Fusion / gastown / ClawTeam | Hierarchical missions, merge queues, file inboxes | Low |
| NXTG-Forge | Research→verify pipeline + file locking + knowledge capture | Med |
| buzz | Nostr relay agents with keys/audit | Low |
| gnap (Resting) | Git-only coordination protocol | Med (protocol) |
| swarm-protocol (Resting) | MCP intents/claims/conflicts | Med (protocol) |

### 8.4 Autonomous Loop / Task Runners

Mostly **out of scope** for agentlog (execution). Worth knowing:

- **Ralph family** (ralph-tui, ralphex, ralph-orchestrator, bernstein): verify-until-done; fresh context per iteration  
- **Issue-driven** (cyrus, Contrabass, sortie, Factory, OpenHands, symphony): tracker → worktree → PR  
- **GitHub Actions** (claude-code-action, codex-action, gh-aw, run-gemini-cli): CI-native agents  

agentlog could later *ingest* their run receipts / PR attributions as session sources — not reimplement them.

### 8.5 Agent Infrastructure & Primitives

| Name | Steal for agentlog? |
|---|---|
| codecast | **Yes** — product twin |
| guild | **Yes** — memory/MCP |
| LionClaw | Study control-plane dir + audit |
| Archon | Deterministic workflow builder — no |
| skillfold / sub-agents-skills / agent-runbook | Skills packaging patterns — yes for Skills page metadata |
| sandbox-agent / omnigent / agenttier | Execution sandboxes — no for v1 |
| neuralyzer | Context wipe for Ralph — no |
| handoff / Claudexor | Cross-harness routing — later |

### 8.6 Personal Assistants

OpenClaw / Hermes / nanobot / Hivekeep / etc. — **low relevance**. Shared idea only: persistent cross-session memory and channel integrations. Do not expand agentlog into a general assistant OS.

---

## 9. Recommendations for agentlog architecture

1. **Stay a lens.** Ingest provider on-disk state; never require spawn-through-agentlog for a session to appear.  
2. **Ship a harness capability matrix** in docs/UI (inspired by CCC/diri): transcript ✓ / live status ? / cost ✓ / skills ✓.  
3. **Inbox taxonomy on Overview:** Needs Input → Working → Idle → Error (codecast/CCC/diri).  
4. **SQLite + FTS first**, embeddings later (guild/codecast).  
5. **Session graph** from Codex spawn edges + Claude subagents (GraphCode metaphor).  
6. **Attribution path:** explore codecast-style blame joining Cursor AI tracking + git.  
7. **Stack confirmation:** Better Agent independently chose FastAPI + React + WebSocket — reinforces our dashboard-design.md choice.  
8. **Optional MCP read API** (guild-shaped) after the dashboard works — agents querying their own history is a moat.  
9. **Do not build:** worktree spawner, permission proxy, kanban task board, cloud sync (v1).  
10. **License hygiene:** prefer studying MIT/Apache projects for any code-level ideas; treat NOASSERTION/AGPL as design-inspiration only.

---

## 10. Evidence & limitations

- Awesome-list README is the inventory source of truth for *what is listed*.  
- GitHub stars/licenses via `gh api repos/...` on 2026-08-09; numbers drift.  
- Deep READMEs fetched for 35 repos; not every repo was cloned for source reading.  
- No runtime verification of claims (providers, telemetry, actual local-only behavior).  
- Temporary clone/analysis artifacts under `.tmp-awesome-orchestrators/` (safe to delete).  
- Security: many projects push `curl | sh` installers — treat as untrusted; this audit did not execute them.

---

## 11. Appendix — How to choose (from upstream)

Upstream framing (useful vocabulary for our docs):

- Several agents + review diffs → Terminal TUI or Desktop/Web parallel tools  
- Keep working while away → Autonomous Loop / Task Runners  
- Split a large job → Multi-Agent Swarms  
- Message an agent → Personal Assistants  
- Build your own → Infrastructure & Primitives  

**agentlog’s slot in that taxonomy:** a new hybrid — *Infrastructure-grade ingestion* + *Desktop/Web observability UI*, without the spawn/orchestration responsibilities of Parallel Coding Agents.

