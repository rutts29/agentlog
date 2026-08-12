# Safe portfolio demo protocol

agentlog is designed around private local artifacts. A portfolio demonstration
must therefore prove the engineering without showing a real developer's
transcripts, paths, repositories, tokens, or database.

## What the repository provides today

The checked-in tests construct synthetic records in `TemporaryDirectory`
instances; they do not need `~/.agentlog` or a real coding-agent artifact.
Two useful, bounded examples are:

- `tests/test_source_reader.py`: exercises source-backed storage, blank stored
  text, append visibility, and fail-closed behavior after a rewritten source.
- `tests/test_descriptive_dashboard.py`: builds a temporary SQLite fixture and
  exercises dashboard API summaries, search, session detail, and lineage.

There is currently no committed standalone demo corpus or command that opens
the visual dashboard with fabricated data. Do not substitute a live
`agentlog ingest` or default `~/.agentlog/agentlog.db` for that missing asset.

## Safe, local proof today

After installing the project dependencies, run only the targeted synthetic
tests below. They create temporary test data and clean it up.

```bash
python -m unittest discover -s tests -p 'test_source_reader.py' -v
python -m unittest discover -s tests -p 'test_descriptive_dashboard.py' -v
python -m unittest discover -s tests -p 'test_privacy_boundary.py' -v
```

These are verification demonstrations, not a hosted or visual product demo.
They establish the intended privacy and local-API behaviors without reading
the user's real transcript store.

## Before recording screenshots or video

Create and independently review a dedicated fabricated demo fixture first.
Keep it in a clearly named demo path, use invented session IDs, projects,
models, file paths, text, and token counts, and scan the rendered output—not
just the fixture—for accidental private data. That fixture is intentionally
not claimed to exist yet.

Once a reviewed fixture exists, run the dashboard only against its explicit
database path:

```bash
agentlog --db /absolute/path/to/reviewed-demo.db serve
```

Do not pass a non-loopback host for a portfolio demo. The local API can return
transcript text and has proposal-review actions; it is protected as a local
service, not a public deployment target.

## Suggested 90-second narrative

1. Start with the privacy premise: coding-agent evidence stays local by
   default.
2. Show the normalized session/lineage, tool, model, and usage views using
   fabricated records.
3. Explain the `source_backed` distinction: metadata is durable while text is
   revalidated from its canonical source and withheld when integrity checks
   fail.
4. Show that optional remote extraction is explicitly gated and previewable,
   rather than an implicit analytics upload.
5. Close on the research angle: provenance-aware observations make evaluation
   claims inspectable instead of treating aggregate dashboards as ground truth.

Never display a local token, `~/.agentlog` path, transcript body, raw search
result, hidden browser tab, terminal history, or unreviewed generated file.
