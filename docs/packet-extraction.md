# Packet extraction (subagent handoff)

File-based UX extraction for Cursor Grok subagents — **no API key**. The orchestrating agent emits work packets, launches one subagent per packet with the prompt template, then ingests validated results.

This is separate from the direct API provider (`ApiExtractionProvider`), which remains available for unattended/nightly runs when `XAI_API_KEY` is set.

## Operating loop

```text
1. agentlog extract packets-emit --out .research/extraction-packets/<run>
2. For each pending packet (see packets-status):
     - Give the subagent:
         a) the prompt file in the run dir (copy of prompts/ux_extraction_subagent.md)
         b) packets/pkt_XXXX.json
     - Ask it to write results_inbox/pkt_XXXX.json
3. agentlog extract packets-ingest --run-dir .research/extraction-packets/<run>
4. agentlog extract packets-status --run-dir ...   # resume until all completed
```

Re-running `packets-emit` on an existing run directory is a no-op (manifest preserved). Re-running `packets-ingest` skips completed packets and is safe after interruption.

## Paths

| Path | Role |
|------|------|
| `run_dir/manifest.json` | Run metadata + per-packet status |
| `run_dir/ux_extraction_subagent.md` | Exact prompt given to subagents |
| `run_dir/packets/pkt_XXXX.json` | Triaged, truncated windows for one subagent |
| `run_dir/results_inbox/pkt_XXXX.json` | Drop subagent outputs here |
| `run_dir/results/pkt_XXXX.json` | Accepted copies after ingest |
| `run_dir/rejects/pkt_XXXX.json` | Validation failures (reported, not silent) |

Canonical prompt in repo: `src/agentlog/analysis/extractors/prompts/ux_extraction_subagent.md`.

## Packet sizing

Defaults: **4 windows/packet**, **28k chars/packet** budget, windows ≥12k chars become **singletons**.

After §6 truncation, median windows are ~1k chars and p90 ~4.5k; p99 outliers approach field caps. Packets are work units for one subagent context — not the API `batch_size`. The prompt requires **independent per-window judgments** so co-packaging does not imply joint labeling. Prior measurement found 60% label disagreement when the API batched windows into one completion (`batch_size>1`); that concern is about shared decoding contamination. Packets still use `batch_size=1` semantics in storage and instruct independence explicitly.

## Validation (hard reject)

Ingest rejects (and records) rather than coerces when:

- required fields are missing
- label enums are unknown (or deterministic turn kinds appear)
- `window_id` is not in the packet (or duplicates / missing ids)
- evidence `quote` is not a literal substring of the cited field

## Provenance

Each stored `ux_observations` row carries extractor name/version, prompt hash, model, provider=`packet`, and `packet_id` inside `ExtractorMeta` / `raw_json`.

## Commands

```bash
agentlog extract packets-emit --out DIR [--windows-per-packet 4] [--max-chars 28000]
agentlog extract packets-ingest --run-dir DIR [--results-dir DIR]
agentlog extract packets-status --run-dir DIR
```

## Hand labeling (audit gold)

```bash
agentlog extract label \
  --pack .research/extraction-verification/audit_pack_unlabeled.jsonl \
  --gold .research/extraction-verification/audit_pack_gold.jsonl
```

Gold JSONL matches `load_gold` / audit scoring (`label_status`, `labels.turn_kind`, stances). The UI never shows model predictions.
