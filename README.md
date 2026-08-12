# agentlog

**A local-first observability and evaluation system for AI coding agents.**

agentlog reads durable, on-device artifacts from supported AI coding harnesses,
normalizes them into a local SQLite evidence ledger, and makes the resulting
activity inspectable through a CLI, local dashboard, and read-only MCP tools.
It is designed for understanding how an AI-assisted engineering workflow is
actually behaving—not for controlling agents or sending transcripts to a
hosted analytics service.

Supported adapters currently cover Codex CLI, Claude Code, Cursor, Warp,
Hermes, and T3 Code.

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

`agentlog ingest` discovers artifacts from the current machine. Do not use it
with personal or client data when recording a public demo. See
[the safe portfolio-demo protocol](docs/portfolio-demo.md) instead.

Useful commands include:

```bash
agentlog derive
agentlog brief <session-id>
agentlog extract egress-preview
agentlog --db /path/to/another.db stats
```

## Verification status

The repository contains 67 Python test files covering adapters, storage,
privacy boundaries, APIs, evaluation utilities, and local service behavior.

In the current checkout, the following non-mutating checks passed:

```bash
(cd web && npx tsc --noEmit)
npm --prefix web ci --dry-run --ignore-scripts
```

The full Python suite passed in this checkout with the project virtual
environment:

```bash
.venv/bin/python -m unittest discover -s tests -q
# Ran 675 tests in 31.956s — OK
```

That run emitted FastAPI deprecation warnings and SQLite `ResourceWarning`s;
it was not warning-free. A clean clone still needs its dependencies installed
before these commands can run. For privacy-focused, targeted synthetic checks,
see [docs/portfolio-demo.md](docs/portfolio-demo.md).

## Public-release warning

This repository is not safe to publish merely because code is present. It
operates on private developer activity, and repository history, research
notes, screenshots, raw diffs, and examples all need a human privacy review.

Before any public release:

1. Scan the current tree **and Git history** for secrets, personal data,
   client context, transcripts, databases, and generated artifacts.
2. Exclude `~/.agentlog/`, live transcript sources, research output, and
   unreviewed screenshots. Never record a demo against real local data.
3. Use only a deliberately fabricated, reviewed synthetic dataset for public
   screenshots or video.
4. Add a release license only after the owner selects one. No license is
   currently included.

Historical planning and audit documents may be useful context, but source and
tests are the authoritative record of current behavior.
