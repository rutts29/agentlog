# Agentlog Grok workflows

## Owner Insights

`owner-insights-review.rhai` is the manual review path for exports produced by
`agentlog insights-extract`. It reads `manifest.json`, reuses only validated
batch results, fans out one bounded `grok-4.6` senior-advisor worker per missing
batch, and uses one final reviewer to write the importer-compatible Agentlog
facts packet. Batch workers emit Insights plus optional unbound
`proposal_signals`; only the final reviewer reads `proposal_targets.json`,
binds an opaque target ID/base-hash pair, and emits importable proposals. The
workers receive redacted packets as untrusted evidence and may not edit the
repository, database, `AGENTS.md`, or skills.

Run it manually with `export_dir` and `facts_output` arguments. Named
`/workflow` launches use the default 128-agent budget. The workflow reserves
one slot for final synthesis, so it reviews at most 126 missing batches after
manifest discovery. If more are pending, it writes that bounded chunk, exits
without synthesis, and tells you to run the same workflow again. Validated
result files are reused on each run; the final reviewer starts only once every
manifest batch has a valid result. The workflow never imports Insights or
applies Proposals; inspect the resulting facts packet and run `agentlog
insights-import` separately when ready. Existing `owner_insight_targets` in the
facts template are preserved.

Preview and export the canonical corpus:

```sh
.venv/bin/python -m agentlog insights-extract --range all --out .research/owner-facts.json
.venv/bin/python -m agentlog insights-extract --range all --out .research/owner-facts.json \
  --confirm-external-review i-understand-redacted-transcript-and-config-text-will-be-shared-manually
```

Launch the review inside Grok Build, repeating the same command when a run
reports `review_chunk_complete`:

```text
/workflow owner-insights-review {"export_dir":".research/owner-facts.owner-insights","facts_output":".research/owner-facts.json","run_id":"owner-insights-manual"}
```

After inspecting the completed packet, import approved Insights and pending
Proposals explicitly:

```sh
.venv/bin/python -m agentlog insights-import --model grok-4.6 --approve .research/owner-facts.json
```

Older Coach packet workflows are intentionally excluded from this directory.
Their closed taxonomy and proof-arc contract are not the owner-facing analysis
path and must not be used to populate new Insights.
