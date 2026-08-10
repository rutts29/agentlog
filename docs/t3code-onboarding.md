# Onboarding the existing skills inventory to T3 Code

agentlog proposal, drafted 2026-08-09.

## Status

Nothing has been applied. agentlog created only this file. No harness config was
read-write opened, no symlink was created, no skill was copied, moved, or deleted,
and the T3 Code application was neither launched nor quit. Every command and UI
step below is for the user to run manually.

## Headline recommendation

Do not create a fourth copy of the inventory, and do not try to symlink anything
into `~/.t3` — there is no skills directory there to link into. T3 Code has no
skills store of its own; it asks each configured provider what skills that
provider can see, and each provider answers from its own real home directory.

The single highest-value action is to **enable the `codex` provider instance and
point its `binaryPath` at the Codex binary that ships inside ChatGPT.app**. That
one change makes T3 read the user's existing `~/.codex/skills` directly, with no
duplication. It is the only inventory root that reaches T3 today without a
filesystem change.

The two caveats that make this different from the obvious plan:

1. Enabling the `claudeAgent` provider will surface **zero** skills, because
   `~/.claude/skills` does not exist on this machine. All 44 Claude-side skills
   live inside plugin caches, and T3's Claude skill scanner does not look at
   plugin directories. There is also no `claude` binary anywhere on `PATH`, so
   the provider cannot start at all.
2. The Cursor driver has no skill discovery code whatsoever. The 20 skills in
   `~/.cursor/skills-cursor` can never reach T3 through the Cursor provider, no
   matter what is toggled.

## How T3 Code discovers skills

Everything in this section was read from the TypeScript sources that ship inside
`/Applications/T3 Code (Nightly).app/Contents/Resources/app.asar`. The bundle
embeds original sources in its sourcemaps, so they are readable with
`rg -a` against the asar without extracting or executing anything.

### There is no T3-owned skills directory

**VERIFIED.** `~/.t3` contains exactly three entries: `userdata/`, `caches/`,
`worktrees/`. Confirmed by `ls -1 ~/.t3`. There is no `skills`, `commands`,
`plugins`, or `rules` directory. `~/Library/Application Support/t3code/` is an
Electron/Chromium profile only.

### Claude skills: direct filesystem scan by T3

**VERIFIED**, module `src/provider/Drivers/ClaudeSkills.ts`. Its own docstring:

> ClaudeSkills — filesystem discovery of Claude Code skills for the `$` picker.
> Claude Code loads skills from `<config dir>/skills` (user scope) and
> `<cwd>/.claude/skills` (project scope), one directory per skill with a
> `SKILL.md` carrying YAML frontmatter. The Agent SDK init handshake surfaces
> skills only as slash commands without their filesystem paths, so the provider
> snapshot scans the same locations directly, mirroring how the Codex app-server
> reports its skills.

The config-dir precedence in `resolveClaudeConfigDirPath`, verbatim from source:

1. the provider instance's `homePath` setting, which
   `makeClaudeEnvironment` exports as `CLAUDE_CONFIG_DIR`
2. an existing `CLAUDE_CONFIG_DIR` in the process environment
3. `path.join(NodeOS.homedir(), ".claude")`

Then `discoverClaudeSkills` builds:

```
roots = [
  { directory: path.join(configDirPath, "skills"), scope: "user" },
  { directory: path.join(cwd, ".claude", "skills"), scope: "project" },
]
```

Per entry it reads `<root>/<entry>/SKILL.md`, parses YAML frontmatter for `name`
and `description`, and inserts into a `Map` keyed by name.

**VERIFIED behaviors, from the same source:**

- Unreadable root: `readDirectory(...).pipe(Effect.orElseSucceed(() => []))`.
  A missing directory yields an empty list, silently.
- Malformed frontmatter: the source comment reads "Malformed frontmatter means
  the skill won't load in Claude Code either — skip it rather than surfacing a
  broken entry under its directory name." The skill is silently dropped.
- Missing frontmatter `name`: falls back to `entry.trim()`, the directory name.
- Name collision: roots are iterated user-then-project and written into a single
  `Map`, so the project-scoped skill overwrites the user-scoped one.

**VERIFIED absence:** `ClaudeSkills.ts` contains no reference to
`~/.claude/plugins`, to marketplaces, or to plugin manifests. The only
`collectPluginSkillSearchDirs` code in the bundle sits in vendored third-party
code, not in any `src/` module of T3. Consequence: the 44 skills under
`~/.claude/plugins/cache/**` will not appear in T3's `$` picker.

**INFERRED, not verified:** when T3 spawns the real `claude` CLI, that CLI does
its own plugin loading from `~/.claude/plugins` and would still have those
skills available at runtime. Basis: `CLAUDE_CONFIG_DIR` points the CLI at the
real `~/.claude`, and the vendored Claude settings schema in the bundle
describes plugin-driven skill loading. agentlog could not confirm this without
running the app, which was out of scope.

### Codex skills: delegated to the Codex app-server

**VERIFIED**, module `src/provider/Drivers/CodexDriver.ts`. T3 issues an ACP
request and parses the reply:

```
client.request("skills/list", { cwds: [input.cwd] })
...
skills: parseCodexSkillsListResponse(skillsResponse, input.cwd)
```

T3 does no filesystem scanning for Codex. Whatever the Codex app-server reports
is what T3 shows, so Codex's own resolution of `$CODEX_HOME/skills` (and its
plugins) applies unchanged.

**VERIFIED**, `~/.t3/caches/codex.json` records
`continuation.groupKey = "codex:home:/Users/ruttanshbhatelia/.codex"`, so T3 is
targeting the real `~/.codex`, not a shadow copy.

**VERIFIED but unused:** the ACP schema defines a `skills/extraRoots/set` method
that would let a client add extra skill roots to Codex. Grepping the bundle
found only schema definitions and dispatch tables — no call site in any T3 `src/`
module. T3 does not currently use it, so it is not an available lever.

### Cursor skills: not discovered at all

**VERIFIED**, module `src/provider/Drivers/CursorDriver.ts`. Its docstring says
model catalog and capability refreshes happen via Cursor's
`list_available_models` extension method. There is no skill discovery code, and
`~/.t3/caches/cursor.json` reports `"skills": []` and `"slashCommands": []`
despite Cursor being the one enabled, installed, authenticated provider.

### Slash commands

**VERIFIED**, `src/provider/Drivers/ClaudeDriver.ts`:

```
const skills = yield* discoverClaudeSkills(claudeSettings, cwd, resolvedEnvironment);
const slashCommands = capabilities?.slashCommands ?? [];
const dedupedSlashCommands = dedupeSlashCommands(slashCommands);
```

Slash commands come from the provider's own capability handshake, not from a
filesystem scan, and are deduplicated. They are a separate channel from skills.
For Cursor the capability probe returns an empty list.

### Does T3 Code read AGENTS.md or CLAUDE.md?

**VERIFIED: no.** The strings appear 52 times in the bundle, but every occurrence
traces to vendored third-party code rather than T3's own logic:

- A vendored npm package `agent-install` (present as a real package entry in the
  asar header, with its own `bin/agent-install.mjs`), described as "Install
  SKILL.md files, MCP servers, and AGENTS.md guidance for any coding agent". It
  carries the `agents-md` CLI subcommands (`init`, `read`, `set-section`,
  `symlink-claude`) and the target table with `globalSkillsDir: join(claudeHome,
  "skills")` / `join(codexHome, "skills")`. It is imported by a bundled
  `react-grab` skill-installer CLI, not by the provider layer.
- The vendored Claude Agent SDK settings schema, which describes `claudeMd`,
  `claudeMdExcludes`, and plugin trust options. That is Claude Code's schema
  travelling with the SDK, not T3 configuration.

No module under `src/` in the bundle references `AGENTS.md` or `CLAUDE.md`.
T3's only instruction injection of its own is
`src/provider/CodexDeveloperInstructions.ts`, which appends a short
`<runtime_info>` block naming the harness and model. So `~/AGENTS.md` (on this
machine a symlink to `~/.claude/CLAUDE.md`, 71 lines) reaches an agent only
because the underlying provider CLI loads it, exactly as it does outside T3.

### MCP

**VERIFIED** that T3 ships `src/mcp/{McpHttpServer,installer,list,remove,agents,
constants,...}.ts` — it hosts an MCP server and installs that server into other
agents. **VERIFIED** that all `mcpServers` and `.mcp.json` handling found in the
bundle belongs to the vendored Claude Agent SDK and the vendored `agent-install`
target table (`configKey: "mcpServers"`, `projectConfigPath: ".mcp.json"`).
agentlog found no T3 `src/` module that reads a user MCP config. **INFERRED:**
MCP servers reach an agent through the provider CLI's own config, so the existing
`~/.cursor`, `~/.claude`, and `~/.codex` MCP setups keep working per provider and
need no T3-side change.

## Current state of the user's inventory

All counts verified by read-only `ls` / `find` and by parsing YAML frontmatter
with PyYAML from this project's venv.

| Root | Skill dirs | SKILL.md found | Frontmatter parses | Reaches T3 today |
| --- | --- | --- | --- | --- |
| `~/.cursor/skills-cursor/` | 20 | 20 | 20 of 20 | No — Cursor driver has no skill discovery |
| `~/.claude/skills/` | does not exist | 0 | n/a | No |
| `~/.claude/plugins/` (enabled plugins) | 18 plugins | 44 | 44 of 44 | No — T3 does not scan plugin dirs |
| `~/.codex/skills/` (user) | 4 | 4 | 4 of 4 | Yes, once `codex` is enabled |
| `~/.codex/skills/.system/` | 6 | 6 | 6 of 6 | Probably — see unverified section |
| `~/.agents/skills/` | 4 | 4 | 4 of 4 | No — no provider reads this root |
| `~/AGENTS.md` | symlink to `~/.claude/CLAUDE.md`, 71 lines | n/a | n/a | Only via the provider CLI |

Zero malformed-frontmatter skills were found anywhere, so the silent-skip
behavior is not currently biting. One name-vs-directory mismatch exists:
`~/.claude/plugins/cache/claude-plugins-official/hookify/unknown/skills/writing-rules/SKILL.md`
declares `name: writing-hookify-rules`. Harmless, but it means the picker label
will not match the directory.

Cursor also has 20 named plugin directories under
`~/.cursor/plugins/cache/cursor-public/` plus numeric aliases. None of them reach
T3 either.

### Outstanding items from the cleanup queue

Re-checked against live config rather than trusting
[`cleanup-queue.md`](./cleanup-queue.md):

- **Superpowers dual-install: resolved.** `~/.claude/settings.json` now has
  `enabledPlugins["superpowers@claude-plugins-official"] = false`, and Cursor
  holds the primary copy under `~/.cursor/plugins/cache/cursor-public/superpowers`.
  No UI action is still pending. Note that because Cursor is the surviving copy,
  Superpowers cannot reach T3 at all.
- **Firecrawl** is likewise disabled on the Claude side, with Cursor as source of
  truth. Same consequence.
- 18 of 20 Claude plugin entries are enabled.
- **Repo skill duplication** in `ai-challenge-loan-ref` remains unreconciled; the
  earlier execution record found genuine content divergence, so nothing was
  merged. Unrelated to T3, but still open.

The net effect of the earlier cleanup is worth stating plainly: choosing Cursor
as the source of truth for shared plugins was correct for token cost, and it is
also the choice that makes those skills invisible to T3. That is a real
trade-off, not a mistake, but it should be re-decided consciously if T3 becomes a
primary harness.

## The recommendation

### Step 1 — enable the Codex provider (recommended, no filesystem change)

This is the whole win. It exposes `~/.codex/skills` to T3 with no copy and no
link.

There is one prerequisite the user should know about: **there is no `codex`
binary on `PATH`.** Verified with `command -v codex` against the full login
`PATH`, and by checking `~/.local/bin`, `/opt/homebrew/bin`, `~/.bun/bin`, the
nvm node bin dir, and `~/.npm-global/bin`. The Codex CLI is instead bundled
inside the ChatGPT desktop app:

```
/Applications/ChatGPT.app/Contents/Resources/codex
```

Verified as `Mach-O 64-bit executable arm64`, mode `-rwxr-xr-x`, dated
2026-08-07. So `binaryPath: "codex"` as currently configured will not resolve.

UI path (user performs this):

1. Open T3 Code.
2. Settings, then Providers.
3. Select the **Codex** instance.
4. Set its binary path to `/Applications/ChatGPT.app/Contents/Resources/codex`.
5. Toggle the instance on.
6. Leave the home path field empty so it defaults to `~/.codex`.
7. Open a chat in a workspace and press `$` to confirm the skill picker lists the
   Codex skills.

Equivalent hand edit, shown for reference only — **agentlog will not apply this,
and the app may overwrite the file while it is running**, so quit T3 Code first
if the user chooses this route:

```diff
--- a/Users/ruttanshbhatelia/.t3/userdata/settings.json
+++ b/Users/ruttanshbhatelia/.t3/userdata/settings.json
@@
     "codex": {
       "driver": "codex",
-      "enabled": false,
+      "enabled": true,
       "config": {
         "enabled": true,
-        "binaryPath": "codex",
+        "binaryPath": "/Applications/ChatGPT.app/Contents/Resources/codex",
         "homePath": "",
         "shadowHomePath": "",
         "launchArgs": "",
         "customModels": []
       }
     },
```

Note the file has two nested `enabled` flags per instance. The top-level
`providerInstances.<id>.enabled` is the one currently `false` for Codex; the
inner `config.enabled` is already `true`. Both matter. As a cross-check, the
Cursor instance has the mirror-image state (`enabled: true` outside,
`enabled: false` inside), so the two fields are clearly independent.

To confirm afterwards without launching anything, read the capability cache:

```
cat ~/.t3/caches/codex.json
```

Expect `"installed": true`, `"status": "ready"`, and a non-empty `skills` array.

### Step 2 — decide about Claude, knowing it currently yields nothing

Enabling `claudeAgent` today would accomplish nothing:

- No `claude` binary exists on `PATH` or in any of the usual install locations.
  The provider cannot start.
- Even if it could, `~/.claude/skills` does not exist, so `discoverClaudeSkills`
  hits `Effect.orElseSucceed(() => [])` and returns zero user-scope skills.
- The 44 real skills live under `~/.claude/plugins/cache/**`, which T3 does not
  scan.

So the honest recommendation is to leave `claudeAgent` disabled until the Claude
Code CLI is actually installed. Reinstalling it is a separate decision with its
own cost, and agentlog is not recommending an install.

If the user does install the CLI later, `~/.claude/skills` still needs to exist
and contain real skill directories before T3's picker will show anything. That is
the point at which the linking question below becomes live.

### Step 3 — accept that Cursor-hosted skills cannot reach T3

There is no toggle, setting, or config edit that exposes `~/.cursor/skills-cursor`
or `~/.cursor/plugins/**` to T3, because the Cursor driver contains no discovery
code to configure. This is a code-level limitation of T3 Code 0.0.32-nightly, not
a misconfiguration. Most of that inventory is Cursor-specific anyway (see the
portability map), so the practical loss is small: roughly four skills are
genuinely portable, and three of those four have equivalents already present in
`~/.codex/skills/.system`.

## Alternative: symlinking (not recommended, not created)

agentlog did not create any symlink and is not proposing one. It is documented
only because it is the sole mechanism that could expose the portable Claude-side
and Cursor-side inventory to T3 without a second copy, and the user should be
able to weigh it.

The shape it would take, if the Claude Code CLI were installed:

```
mkdir -p ~/.claude/skills
ln -s ~/.claude/plugins/cache/claude-plugins-official/vercel/0.45.1/skills/nextjs ~/.claude/skills/nextjs
```

One link per skill, into a `~/.claude/skills` directory that does not yet exist.

Why agentlog is not recommending it:

- It violates the user's explicit no-symlinks constraint.
- It hand-maintains a shadow index that drifts every time a plugin updates. The
  Vercel plugin alone contributed 30 skills and has already moved from `0.43.0`
  to `0.45.1` during earlier cleanup, which would have broken 30 version-pinned
  links.
- It re-creates the duplicate-injection problem the earlier cleanup solved: a
  plugin skill linked into `~/.claude/skills` is visible to Claude Code twice,
  once via the plugin loader and once via the skills directory.
- For Cursor-hosted skills it does not help at all, because no driver reads them.

## Risks

**Symlink traversal is untested.** `ClaudeSkills.ts` uses Effect's
`fileSystem.readDirectory` and `readFileString`. The bundled Node adapter calls
`fs.readdir(path, { withFileTypes: true })` and `fs.readFile`, both of which
follow symlinks on the path being opened, so a symlinked skill directory or a
symlinked `skills` root should resolve. **INFERRED from Node semantics and the
adapter source; not verified by execution**, because verifying it would require
running the app. `withFileTypes: true` means a symlinked entry reports as a link
rather than a directory, and agentlog did not trace whether any caller filters on
entry type — the discovery loop it read does not, it simply joins the name and
tries to read `SKILL.md`.

**The Homebrew cask and the installed app have diverged.** The cask
`t3-code@0.0.32` ships `T3 Code (Alpha).app -> /Applications/T3 Code (Alpha).app`,
and that target does not exist. The only installed app is
`/Applications/T3 Code (Nightly).app`, because
`~/.t3/userdata/desktop-settings.json` has `"updateChannel": "nightly"` with
`"updateChannelConfiguredByUser": true`. Verified by `ls -la` on the Caskroom
directory and on `/Applications`. Consequences: `brew upgrade` or `brew uninstall
--zap` on this cask operates on a stale, broken reference and could reinstall the
Alpha build alongside the Nightly one, or fail confusingly. Do not run brew write
commands against it without a plan. Note that the read-only checks in this
document did not run any brew command; `brew` is expected to fail with a 403 in
this sandbox anyway.

**Nightly auto-updates replace the app bundle, not the user data.** `~/.t3` is
user data outside the bundle and survives updates, so provider settings and
caches persist. But every claim in this document about T3's internals was read
from the current `app.asar`, and a nightly build can change
`ClaudeSkills.ts` or add Cursor skill discovery without warning. Treat the
mechanism section as accurate for the build dated 2026-08-09 and re-verify after
updates.

**Three incompatible skill layouts.** Claude expects `<dir>/SKILL.md` with YAML
frontmatter and is what T3's scanner assumes. Codex uses `$CODEX_HOME/skills`
with its own manifest handling and a dot-prefixed `.system` tier. Cursor uses
`~/.cursor/skills-cursor/<name>/SKILL.md` plus a separate plugin cache with
content-hash directories and numeric aliases. The `SKILL.md` bodies happen to be
close enough that content ports by hand, but the surrounding layout does not, and
skills that reference harness-specific tools break silently when moved.

**Malformed frontmatter is silently skipped.** A skill with a YAML syntax error
simply does not appear — no warning, no log line in the picker. Nothing in the
current inventory is affected (44 of 44 Claude plugin skills, 20 of 20 Cursor,
10 of 10 Codex, 4 of 4 agents all parse), but a future hand-edit could make a
skill vanish with no signal.

**Project scope silently overrides user scope.** A skill in
`<cwd>/.claude/skills/<name>` replaces a same-named user skill in the `Map`. In a
repo that vendors its own `.claude/skills`, the user's version is shadowed with
no indication in the picker.

**Exposing harness-specific skills to the wrong harness is actively harmful.**
`update-cursor-settings` edits `~/.cursor/settings.json`; `create-hook` writes
Cursor `hooks.json`; `update-cli-config` writes `~/.cursor/cli-config.json`;
`canvas` and `loop` depend on Cursor UI affordances that do not exist in T3.
Surfacing these in a T3 picker invites an agent to write to a harness it is not
running under, which is exactly the class of change agentlog refuses to make
itself. Any future porting should be an explicit allowlist, never a bulk link.

## Portability map

Cursor-specific skills are the ones that manipulate Cursor itself or depend on
Cursor UI surfaces. Codex-specific ones orchestrate Codex or another CLI through
Codex. Classification is agentlog's judgment based on each skill's frontmatter
description, which was read directly.

### `~/.cursor/skills-cursor/` (20)

| Skill | Classification | Recommendation |
| --- | --- | --- |
| `automate` | Cursor-specific — creates Cursor Automations | Do not expose |
| `canvas` | Cursor-specific — Cursor Canvas UI surface | Do not expose |
| `create-hook` | Cursor-specific — writes Cursor `hooks.json` | Do not expose |
| `create-rule` | Cursor-specific — writes `.cursor/rules` | Do not expose |
| `create-subagent` | Cursor-specific — Cursor subagent config | Do not expose |
| `loop` | Cursor-specific — in-session `/loop` scheduling | Do not expose |
| `migrate-to-skills` | Cursor-specific — converts `.cursor/rules` and `.cursor/commands` | Do not expose |
| `onboard` | Cursor-specific — Cursor onboarding flow | Do not expose |
| `rename-chat` | Cursor-specific — renames the Cursor chat | Do not expose |
| `review` | Cursor-specific — dispatches Cursor Bugbot/Security subagents | Do not expose |
| `review-bugbot` | Cursor-specific — Bugbot subagent | Do not expose |
| `review-security` | Cursor-specific — Security Review subagent | Do not expose |
| `sdk` | Cursor-specific — guidance for the Cursor SDK | Do not expose |
| `shell` | Cursor-specific — `/shell` command semantics | Do not expose |
| `statusline` | Cursor-specific — Cursor CLI status line | Do not expose |
| `update-cli-config` | Cursor-specific — writes `~/.cursor/cli-config.json` | Do not expose |
| `update-cursor-settings` | Cursor-specific — writes Cursor settings | Do not expose. Highest blast radius if mis-fired |
| `autopilot` | Portable in principle — PR triage, conflicts, CI | Would need porting by hand; overlaps Codex `review-agent` |
| `split-to-prs` | Portable in principle — git/PR workflow | Would need porting by hand |
| `create-skill` | Portable concept, Cursor-flavored wording | Superseded by `~/.codex/skills/.system/skill-creator`; skip |

Net: 17 of 20 are Cursor-only. Three are portable in principle, and two of those
three already have Codex-side equivalents.

### `~/.claude/plugins/**` skills under enabled plugins (44)

| Group | Count | Classification | Recommendation |
| --- | --- | --- | --- |
| `vercel/*` (`nextjs`, `ai-sdk`, `shadcn`, `turbopack`, `workflow`, `vercel-cli`, and 24 more) | 30 | Portable — framework and platform guidance, no harness coupling | Highest-value group to port if T3 becomes primary. Cannot reach T3 without a `~/.claude/skills` tree and a working `claude` CLI |
| `chrome-devtools-mcp/*` (`chrome-devtools`, `chrome-devtools-cli`, `a11y-debugging`, `debug-optimize-lcp`, `memory-leak-debugging`, `troubleshooting`) | 6 | Portable, but MCP-coupled — depends on the Chrome DevTools MCP server being configured in the target harness | Port only alongside the MCP server config |
| `discord/*` (`access`, `configure`) | 2 | Portable, MCP-coupled and credential-bearing | Port only if the Discord MCP server is configured in T3's provider |
| `frontend-design` | 1 | Portable — pure design guidance | Safe to port |
| `playground` | 1 | Portable — generates standalone HTML | Safe to port |
| `skill-creator` | 1 | Portable, but duplicates the Codex `.system` version | Skip; prefer the Codex one |
| `claude-md-improver` | 1 | Harness-specific — audits `CLAUDE.md` files | Do not expose to a non-Claude harness |
| `claude-automation-recommender` | 1 | Harness-specific — recommends Claude Code hooks, subagents, plugins | Do not expose |
| `writing-rules` (declares `name: writing-hookify-rules`) | 1 | Harness-specific — hookify rule syntax for Claude Code | Do not expose |

### `~/.codex/skills/` user tier (4)

| Skill | Classification | Recommendation |
| --- | --- | --- |
| `playwright` | Portable — terminal browser automation | Keep; reaches T3 automatically once Codex is enabled |
| `codex-claude-communication` | Codex-specific — drives Claude Code from Codex via tmux | Keep in place; it is meaningful only under Codex, and Codex is where T3 will read it |
| `claude-dynamic-workflows` | Codex-specific — same coupling | Same |
| `migrate-to-codex` | Codex-specific — writes Codex config | Same. Note it is a config-writing skill, so it should not be ported to any other harness |

### `~/.codex/skills/.system/` platform tier (6)

`imagegen`, `openai-docs`, `plugin-creator`, `review-agent`, `skill-creator`,
`skill-installer`. Platform-managed; the earlier cleanup already decided to leave
these alone and that decision still holds. `review-agent` and `skill-creator` are
generically useful and are the reason the Cursor equivalents can be skipped.
`openai-docs`, `plugin-creator`, and `skill-installer` are Codex-specific.

### `~/.agents/skills/` (4)

`source-command-continual-learning`, `source-command-dream`,
`source-command-learn`, `source-command-recap`. Each is a thin wrapper described
as "Run the migrated source command `<x>`". No provider driver in T3 reads
`~/.agents/skills`; the only code in the bundle that knows about that path is the
vendored `agent-install` package's canonical-directory helper, which T3's
provider layer does not call. These cannot reach T3 through any supported route.

## What agentlog could not verify

- **Whether T3's Codex skill list includes the `.system` tier.** T3 forwards
  whatever `skills/list` returns; whether the Codex app-server reports
  dot-prefixed `.system` entries was not determined, because that would require
  running Codex or T3.
- **Whether the picker actually resolves symlinked skill directories.** The Node
  adapter reads directories with `withFileTypes: true`, and agentlog did not
  trace every consumer of that result for an entry-type filter. The discovery
  loop it did read does not filter, but this was not executed.
- **Whether plugin-provided skills reach the model at runtime under T3.** They
  will not appear in the `$` picker — that is verified from source. Whether the
  spawned `claude` CLI still loads them from `~/.claude/plugins` is a reasonable
  inference from `CLAUDE_CONFIG_DIR` pointing at the real home, but it was not
  observed, and the `claude` CLI is not installed to test with.
- **The exact Settings UI labels in T3 Code.** The app was not launched, per the
  constraints. The UI path in Step 1 is described from the settings schema
  (`providerInstances`, `binaryPath`, `homePath`), so the field names are right
  but the menu wording may differ.
- **The public repository's source.** `https://github.com/pingdotgg/t3code` was
  confirmed to exist and be public (name `t3code`, last pushed 2026-08-09T12:55Z,
  read-only via `gh repo view`), but no source was fetched, cloned, installed, or
  executed. Every code claim in this document comes from the locally installed
  app bundle instead.
- **Whether `claude` was ever installed on this machine and how it was removed.**
  `~/.claude` is a large, active state directory with 20 plugin entries and a
  71-line `CLAUDE.md`, yet no binary exists in any searched location. agentlog
  did not investigate further.
