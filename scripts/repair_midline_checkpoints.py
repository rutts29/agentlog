"""Rewind artifact checkpoints that sit inside an unterminated JSONL record.

A checkpoint written in the middle of a line (the H2 defect) makes append
ingestion resume after a partial record, so the completed record can never be
parsed. Rewinding to the start of that line lets normal incremental ingest
recover it. Metadata only: no session, message or label rows are touched.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agentlog.db.schema import connect
from agentlog.ingest.base import hash_prefix
from agentlog.ingest.pipeline import adapter_for

DEFAULT_DB = Path.home() / ".agentlog" / "agentlog.db"


def _line_start(path: Path, offset: int, *, lookback: int = 1 << 20) -> int:
    start = max(0, offset - lookback)
    with path.open("rb") as fh:
        fh.seek(start)
        chunk = fh.read(offset - start)
    nl = chunk.rfind(b"\n")
    return start + nl + 1 if nl != -1 else start


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    rows = conn.execute(
        "SELECT id, harness, path, size, parsed_offset, content_hash, "
        "parser_version, mtime_ns FROM artifacts"
    ).fetchall()

    repaired = 0
    for art in rows:
        adapter = adapter_for(str(art["harness"]))
        if adapter is None or not adapter.supports_byte_append:
            continue
        path = Path(str(art["path"]))
        offset = int(art["parsed_offset"])
        if offset <= 0 or not path.is_file():
            continue
        with path.open("rb") as fh:
            fh.seek(offset - 1)
            if fh.read(1) == b"\n":
                continue
        start = _line_start(path, offset)
        print(
            f"{art['harness']}: {path}\n"
            f"  parsed_offset {offset} -> {start} (rewind {offset - start} bytes)"
        )
        if not args.apply:
            continue
        conn.execute(
            "UPDATE artifacts SET parsed_offset = ?, content_hash = ? WHERE id = ?",
            (start, hash_prefix(path, start), int(art["id"])),
        )
        repaired += 1

    if args.apply:
        conn.commit()
    print(f"{'repaired' if args.apply else 'would repair'}: {repaired}")
    conn.close()


if __name__ == "__main__":
    main()
