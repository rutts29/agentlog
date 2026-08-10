# t3 code transcript format

How agentlog reads t3 code (Homebrew cask `t3-code`, nightly channel).

Every claim below is marked **[verified]** (observed in the local install) or
**[inferred]** (read from the TypeScript sources that ship inside the app
bundle's sourcemaps, but not yet seen in real data). Nothing here came from
cloning, installing, or running the upstream repository.

## Install footprint

**[verified]** Homebrew cask `t3-code` 0.0.32, Caskroom at
`/opt/homebrew/Caskroom/t3-code/0.0.32/`. The cask artifact is a symlink
`T3 Code (Alpha).app -> /Applications/T3 Code (Alpha).app`, which is **broken**:
the app that actually exists is `/Applications/T3 Code (Nightly).app`. The app
self-updates on the nightly channel (`desktop-settings.json` has
`updateChannel: "nightly"`, `updateChannelConfiguredByUser: true`), so the
bundle name can change under Homebrew. There is no `t3` CLI on `PATH`; it is an
Electron app running a local server on `127.0.0.1:3773`.

**[verified]** State roots:

| Path | Contents |
| --- | --- |
| `~/.t3/userdata/state.sqlite` | The transcript store. WAL mode, 40 internal migrations applied. |
| `~/.t3/userdata/settings.json` | `providerInstances` map (driver, enabled, binaryPath, customModels). |
| `~/.t3/userdata/{client,desktop}-settings.json` | UI preferences, update channel. |
| `~/.t3/userdata/secrets/*.bin`, `clerk-tokens.json` | Auth material. Never read by agentlog. |
| `~/.t3/userdata/logs/{desktop,server}.trace.ndjson` | OpenTelemetry spans, not conversations. |
| `~/.t3/userdata/logs/{provider,terminals}/` | Empty on this install. |
| `~/.t3/caches/<instance>.json` | Per-provider capability snapshots incl. `models`, `skills`, `slashCommands`. |
| `~/Library/Application Support/t3code/` | Chromium profile only. No transcripts. |

There is no `~/.t3/skills`, `~/.t3/commands`, or `~/.t3/plugins`. t3 code has no
skill store of its own; it asks each provider driver, which reads that
provider's real home. See `docs/t3code-onboarding.md` for what that means for
making the existing skill inventory reachable.

## Orchestrator model

**[verified]** t3 code does not run a model itself. A thread is driven by a
*provider instance* (`cursor`, `codex`, `claudeAgent`, `grok`, `opencode`),
each of which shells out to that vendor's CLI. Consequently the adapter splits
identity three ways rather than stuffing a provider name into `model`:

- `agent_profile` — the t3 provider instance id (`cursor`, `claudeAgent`, ...)
- `provider` — the upstream vendor (`cursor`, `openai`, `anthropic`, `xai`, ...)
- `model` — the model slug only (`gpt-5.6-sol`, `claude-opus-5`, ...)

`model` may legitimately be the placeholder `default`, which
`resolve_model_identity` already declines to promote to a canonical model.

## Tables agentlog reads

All reads go through `open_sqlite_readonly`, which prefers a live read-only URI
(so WAL content is visible) and falls back to a temp copy of the db plus its
`-wal`/`-shm` sidecars.

- `projection_projects` — `workspace_root` supplies repo/cwd.
- `projection_threads` — one row per session. `thread_id` is the session
  identity. Also `branch`, `worktree_path`, `model_selection_json`.
- `projection_thread_messages` — `role` is one of `user` / `assistant` /
  `system` **[verified schema, inferred value set from the bundled
  `OrchestrationMessageRole` literal]**.
- `projection_thread_activities` — tool traffic. `tone` is one of `info`,
  `tool`, `approval`, `error`; `kind` is a free-form string (`tool-call`,
  `tool-result`, `command`, `reasoning`, ...).
- `projection_turns` — maps `turn_id` to the `assistant_message_id` (or
  `pending_message_id`) it produced. This is what links activities to messages.
- `projection_thread_sessions` — `provider_name`, `provider_instance_id`.
- `projection_thread_proposed_plans` — `implementation_thread_id` links a plan
  thread to the thread that implements it.
- `orchestration_events` — append-only log carrying `actor_kind`
  (`client` / `server` / `provider`) and per-turn `modelSelection`.

## modelSelection

**[verified]** shape, from a real row:
`{"instanceId": "cursor", "model": "default"}`.

**[inferred]** full shape from the bundled `ModelSelection` schema:
`{instanceId?, provider?, model, options?}`, where `options` may carry
`effort` (`low` | `medium` | `high` | `xhigh` | `max` | `ultracode` |
`ultrathink`), `fastMode`, and `contextWindow`. `provider` is a legacy alias
decoded into `instanceId`.

`ultracode` and `ultrathink` are not in agentlog's canonical effort set, so they
normalize to `unknown` with the raw string preserved in `effort_source`.

## Per-message model and effort

**[inferred]** `thread.turn-start-requested` carries both `messageId` and an
optional `modelSelection`, so a mid-session model switch is attributable to an
exact message. `thread.meta-updated` can also change the selection; the adapter
replays events in `sequence` order and applies the most recent selection at or
before each message's timestamp. Thread-level `model_selection_json` is the
fallback, then the project default.

## The four failure modes this adapter avoids

- **Tool-only turns polluting human counts.** `system` rows and empty-text
  streaming placeholders are marked `is_tool_plumbing`, so they never enter
  human-turn counts or exchange windows.
- **Orchestrator prompts counted as human.** A `thread.message-sent` event
  whose `actor_kind` is not `client` marks the message `authored_by_agent`.
  Separately, the seeding prompt of a plan-implementation thread is flagged,
  because the orchestrator wrote it.
- **Orphaned tool events.** Activities resolve to a message via
  `turn_id -> projection_turns -> assistant_message_id`, falling back to the
  nearest preceding message by timestamp, and finally to the last message.
- **Unstable session ids.** `external_id` is the t3 `thread_id` UUID. It is
  never derived from a filesystem path, so re-ingest cannot fork a session the
  way the Cursor path-derived ids once did.

Unrecognized message roles and unparseable payloads are surfaced as ingest
warnings rather than dropped.

## Discovery tolerance

The cask auto-updates nightly, so discovery globs several candidate roots
(`~/.t3`, `~/.t3code`, `~/.config/t3`, `~/.config/t3code`,
`~/Library/Application Support/t3code`) for `userdata/state.sqlite` or
`state.sqlite`. A missing directory yields no paths, which is a
"no data yet" state rather than an error. A SQLite file without
`projection_threads` is skipped with a log line, not a failure.

## Current data

**[verified]** As of the first ingest the store contains one project
(`ai_sec`), one `project.created` event, and zero threads, messages,
activities, and turns. t3 code has been launched but no conversation has been
started, so there is genuinely nothing to ingest yet. t3 code appears in
`/api/harnesses` with zero sessions rather than being omitted.
