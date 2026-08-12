# Live agent presence

Contract for real-time “which agents are running right now” — watch daemon →
`presence.json` → HTTP/SSE. Ephemeral; no DB tables. Frontend (Observatory)
consumes this after the current rebuild.

## Detection

The watch daemon (`python -m agentlog.watch`) updates an in-memory presence map
on every transcript file change **before** ingest. Ingest waits for a 30-second
quiet period but is queued after at most 120 seconds of continuous changes.
Each harness has one independent ingest worker, so discovery, parsing, and
window preparation proceed independently; later changes coalesce into one
follow-up pass. Short SQLite write transactions remain serialized and may
briefly queue another writer. Deterministic derivation runs on its own single
coalescing worker after ingest:

- A session is **active** while its source `.jsonl` was touched within the last
  `active_seconds` (default **90s**, tunable via
  `agentlog.config.PRESENCE_ACTIVE_SECONDS`).
- Session key = adapter `external_id` derived from the path (same helpers as
  ingest: `codex` / `claude` / `cursor` `external_id_from_path`).
- **Current state** is a cheap tail peek (last ~16KB, last complete JSON lines):
  - `streaming` — assistant producing text
  - `tool_running` — tool/function call in flight
  - `waiting` — last event is user / turn ended / task complete
  - `unknown` — unreadable or empty tail
- Malformed tail lines are skipped; peeks never crash the daemon.
- State file: `~/.agentlog/presence.json` (beside the DB), rewritten on every
  presence change and on a **15s** heartbeat (expiry + freshness).

## `GET /api/live`

Polling fallback. Reads the presence file and re-links rows against the DB.

```json
{
  "ts": "2026-08-09T13:20:00.123456+00:00",
  "generation": 4,
  "active_seconds": 90.0,
  "path": "/Users/you/.agentlog/presence.json",
  "sessions": [
    {
      "harness": "cursor",
      "external_id": "Users-…-Plugin/be6ee399-…",
      "session_id": "cursor:Users-…-Plugin/be6ee399-…",
      "source_path": "/Users/you/.cursor/projects/…/be6ee399-….jsonl",
      "state": "tool_running",
      "last_activity_at": "2026-08-09T13:19:48+00:00",
      "age_seconds": 12.4,
      "pending_ingest": false,
      "title": "Add live presence to the watch daemon",
      "repo": "Users-ruttanshbhatelia-side-projects-Plugin"
    }
  ]
}
```

| Field | Notes |
|---|---|
| `session_id` | DB id when ingested; else `null` |
| `pending_ingest` | `true` until a matching `sessions` row exists |
| `title` / `repo` | From DB when linked; title may be a tail hint while pending |
| `age_seconds` | Wall-clock age of last source activity |

Recommend poll interval **2–5s** if SSE is unavailable.

## SSE — `GET /api/events/stream`

Same stream as ingest events. Presence frames:

```
event: presence
data: {"ts":"…","generation":5,"sessions":[…],"transitions":[{"action":"active","key":"cursor:…"},{"action":"idle","key":"codex:…"}]}
```

- `sessions` — full active snapshot (same shape as `/api/live`).
- `transitions` — keys that became `active` or `idle` since the previous frame
  (or since stream connect).
- Ingest frames remain `event: ingest` with the existing payload.

## UI treatment (Observatory)

Use reserved live cyan `#22D3EE` (`--accent-live`) only for “happening now”:

1. **Active-now rail (primary)** — flanking column beside the graph stage (never
   overlays nodes). Each live session row is anchored by a thinking-orb **loader**
   in `--accent-live`: continuous animation while `streaming` / `tool_running`
   (work in progress); settled open-C treatment for `waiting` (needs the human;
   no loader loop). Click centers/selects the graph node when linked.
2. **Graph nodes (secondary)** — matching live nodes get a findable cyan halo;
   loader sweep may echo on the node while work is in flight. Rail remains the
   glance surface. Honor `prefers-reduced-motion` → static rings, no sweep.
3. **Pending ingest** — rail entry shows “ingest pending”; promote to a graph
   link once `pending_ingest` flips false (next poll/SSE).
4. Do not reuse live cyan for harness identity (cursor teal stays distinct).
   Idle screens: zero ambient motion. Working loaders are not ambient — see
   `docs/dashboard-redesign-v2.md` §4 rule 4.
