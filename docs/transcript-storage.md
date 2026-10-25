# Transcript storage

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
- A missing, rewritten, unstable, or mismatched source is reported as
  unavailable/changed. If indexed message rows still contain text, the
  dashboard can show that last indexed copy with an explicit warning. Full
  current transcript text and tool inputs/outputs still require the local
  canonical artifact.

This model reduces future materialization of transcript content. It does not
retroactively erase existing `legacy_materialized` data from a local database.
