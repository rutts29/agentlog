# Owner insights coverage — 2026-08-14

Snapshot used: coach run `.research/grok46-review-20260814/`
(`corpus_snapshot_hash` `d15d0fc0dcca628a677e07a2`, prepare
`coach_20260813T215231Z_5f29a6ee`).

## What was read

| Layer | Count |
|---|---|
| Sessions in the live DB at write time | 972 |
| Root conversations in the prepare snapshot | 100 |
| Roots packetized and read | 59 |
| Eligible windows packetized | 1,387 of 4,357 |
| Distinct session ids inside those packets | 191 |

The 59 roots are every **eligible** root from that prepare, not all 972
sessions and not all 100 roots.

## What was not read (backfill)

- **16 excluded roots** (listed below).
- **25 further roots** in the 100 that never became eligible packets
  (no eligible windows, or outside the eligibility commitment). Warp
  sessions in the live DB were not in this packet set.
- **Grok Build TUI** chats are not ingested, so they are not in the
  100.

Official Coach Luna/Terra ran on the 59 packets and was discarded: the
7-kind compiler is the wrong tool for owner Insights. The live board
came from a later owner-facing read of the same packets.

Live board at write time: 8 approved Insights, 2 pending Proposals.

## Backfill

1. Fix unreadable source-backed / ledger-mismatch sessions, then
   `agentlog coach prepare` again.
2. `agentlog insights-extract --packets <run>/packets --out facts.json`
   (add `--model` to call the LLM, or fill `facts.json` by hand).
3. `agentlog insights-import --approve --model <model> facts.json`.

Do not use `agentlog coach synthesize` / `materialize` to fill Insights.

## Excluded root ids

```
codex:019dc223-82e0-7010-8798-a202a19e6079
codex:019dc2f1-7000-7b53-9a16-42fbf0b6a590
codex:019dc63d-51a0-7ec0-9f00-c90b628a43f1
codex:019dd5a9-31b8-7ff3-af79-d0108d84ef97
codex:019de822-b73a-7331-b101-d52ad00dd416
codex:019deadc-a35d-72a2-9def-41561c710564
codex:019e224f-5da6-77c2-b9ec-0d9ed4fc07d2
codex:019e2267-3422-7121-8a43-003c9ff4e535
codex:019e6027-9779-7711-9af4-0e86b8a3096d
codex:019e645b-20dd-7780-bd09-0f6d2d55633d
codex:019e6473-0074-71b3-b452-68f543a326df
codex:019e83a5-00fe-78c2-8b5a-93cf89b847da
codex:019e8917-690d-7730-a6cb-3d362449ecae
codex:019eeaf1-bc84-7d40-99ae-41593d6b17d3
codex:019f1e41-3bc7-7093-97fc-25ee0a22130c
codex:019f387b-8b4b-7fe1-bebf-4ab0905ac2c6
```
