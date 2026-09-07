# agentlog

**A local-first observability and evaluation system for AI coding agents.**

agentlog reads durable, on-device artifacts from supported AI coding harnesses,
normalizes them into a local SQLite evidence ledger, and makes the resulting
activity inspectable through a CLI, local dashboard, and read-only MCP tools.
It is designed for understanding how an AI-assisted engineering workflow is
actually behaving—not for controlling agents or sending transcripts to a
hosted analytics service.

Supported adapters currently cover Codex CLI, Claude Code, Cursor, Warp,
Hermes, T3 Code, and Grok.

## Why this exists

AI-assisted development creates useful operational evidence: session lineage,
tool use, model and token context, corrections, handoffs, and the outcomes of
experiments. That evidence is often private and scattered across vendor-local
formats. agentlog focuses on three engineering problems:

- Normalizing multiple local transcript formats into a provenance-aware
  evidence ledger.
- Making analyses inspectable: deterministic metrics, bounded retrieval,
  source references, and human-review boundaries for proposals.
- Keeping private transcript data local by default, with explicit safeguards
  around storage, serving, and any optional remote extraction.

## Architecture

```text
local harness artifacts
        │  read-only adapters
        ▼
SQLite evidence ledger ──► deterministic analysis / experiments / provenance
        │                                      │
        ├──► CLI and loopback dashboard         └──► reviewable proposals
        └──► read-only MCP tools
```

The ledger stores normalized sessions, message metadata, tool events, model
and token context, exchange windows, and derived observations. The dashboard
is a local FastAPI service with a React frontend; it is not a hosted product.

### Source-backed transcript storage

Transcript retention is deliberately forward-only:

- Existing rows remain `legacy_materialized`, whose message text is already in
  SQLite and searchable through FTS.
- Newly discovered session identities use `source_backed` storage. SQLite keeps
  identity and integrity metadata, content hashes, tool/token data, and
  derived windows, while stored message text is blank and excluded from FTS.
- When text is requested for a source-backed session, agentlog re-reads the
  canonical local artifact transiently. It verifies harness identity,
  checkpoint prefix, stable reads, session identity, and message metadata
  before returning text.
- A missing, rewritten, unstable, or mismatched source fails closed as
  unavailable/changed; it does not serve stale transcript text.

This model reduces future materialization of transcript content. It does not
retroactively erase existing `legacy_materialized` data from a local database.

## Privacy and trust boundaries

- Data defaults to `~/.agentlog/agentlog.db`; local agent artifacts are read,
  while agentlog writes its own working state.
- The default workflow has no cloud dependency and no transcript egress.
- Optional remote extraction is off by default. It requires an explicit
  per-process acknowledgement and has an egress-preview command. Payloads go
  through credential, home-path, and obvious-PII redaction first, but any
  transcript egress remains sensitive and should be treated as such.
- `agentlog serve` binds to loopback by default and requires a local bearer
  token. Its API can return transcript text and includes local proposal-review
  actions, so it must not be exposed as an unauthenticated public service.
- The project prevents writes to known harness configuration surfaces. Proposed
  configuration changes remain reviewable rather than being applied
  automatically to an agent's configuration.

## Install and use locally

Requires Python 3.11+.

```bash
python -m pip install -e .

agentlog --help
agentlog ingest
agentlog stats
agentlog sessions --harness codex
agentlog search "checkpoint"
agentlog serve
```

`agentlog ingest` discovers supported artifacts on the current machine.

For project-specific configuration proposals, set `AGENTLOG_REPO_ROOTS` to the
repository directories to inspect, separated by your operating system's path
separator (`:` on macOS/Linux, `;` on Windows). Global configuration discovery
does not require this setting.

Useful commands include:

```bash
agentlog derive
agentlog brief <session-id>
agentlog extract egress-preview
agentlog --db /path/to/another.db stats
```

## Development

After installing the Python dependencies, run the test suite:

```bash
python -m unittest discover -s tests -q
```

To build the dashboard:

```bash
cd web
npm ci
npm run build
```

No license is currently included.
