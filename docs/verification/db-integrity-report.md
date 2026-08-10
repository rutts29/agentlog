# agentlog DB integrity report

**Generated:** 2026-08-09 13:27:24 UTC
**Database:** `~/.agentlog/agentlog.db` (opened `mode=ro`)
**Parser version in artifacts:** 7 (all 591 rows)
**Method:** compare live sources via ingest adapters (`ClaudeAdapter` / `CodexAdapter` / `CursorAdapter` / `WarpAdapter`) against SQLite; no DB or source writes.

## Executive verdict

The DB is **internally consistent** (no orphan FKs, no duplicate session keys, no `ended_at < started_at`, no partial `parsed_offset < size` markers). **Claude, Codex, and Warp match their sources** in coverage and in stratified deep samples.

**Cursor is behind live disk:** **15** transcript files have never been ingested, and **9** ingested sessions are stale (source files grew after `artifacts.size` was recorded). Together that is roughly **+580 messages** and **+1159 tool events** missing versus a fresh reparse. This is the primary integrity failure.

---

## 1. Coverage census

| Harness | Source units | `artifacts` rows | `sessions` rows | Messages | Tool events | Absent from DB | DB without source |
|--------:|-------------:|-----------------:|----------------:|---------:|------------:|---------------:|------------------:|
| claude | 84 jsonl | 84 | 84 | 7492 | 5083 | 0 | 0 |
| codex | 401 jsonl | 401 | 401 | 21224 | 45903 | 0 | 0 |
| cursor | 120 jsonl | 105 | 105 | 4435 | 6526 | **15 files** | 0 |
| warp | 15 conversations | 1 sqlite | 15 | 45 | 322 | 0 | 0 |
| **total** | — | **591** | **605** | **33196** | **57834** | **15** | **0** |

### Reproduce — coverage

```bash
.venv/bin/python - <<'PY'
import sqlite3
from agentlog.ingest.claude import ClaudeAdapter
from agentlog.ingest.codex import CodexAdapter
from agentlog.ingest.cursor import CursorAdapter
from agentlog.config import DEFAULT_DB_PATH
conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
for name, Adapter in [("claude", ClaudeAdapter), ("codex", CodexAdapter), ("cursor", CursorAdapter)]:
    disc = {str(p) for p in Adapter().discover()}
    art = {r[0] for r in conn.execute("SELECT path FROM artifacts WHERE harness=?", (name,))}
    print(name, "discover", len(disc), "artifacts", len(art), "missing", len(disc-art))
    for p in sorted(disc-art):
        print("  MISSING", p)
PY
```

### Source sessions absent from DB (Cursor)

15 files under `~/.cursor/projects/**/agent-transcripts/**/*.jsonl` have no `artifacts` / `sessions` row. Reparse yield if ingested:

| external_id | lines | would-be msgs | would-be tools | path |
|---|---:|---:|---:|---|
| `Users-ruttanshbhatelia-side-projects-Plugin/9414a071-22c6-43f3-a78a-f902e6e737f9` | 32 | 31 | 92 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/9414a071-22c6-43f3-a78a-f902e6e737f9/9414a071-22c6-43f3-a78a-f902e6e737f9.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:2bbfc6b1-79ef-4b8c-a907-ea20c889a46b` | 1 | 1 | 0 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/2bbfc6b1-79ef-4b8c-a907-ea20c889a46b.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:3304a77e-e0c4-4d3b-8eee-ec2d5c916399` | 26 | 25 | 72 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/3304a77e-e0c4-4d3b-8eee-ec2d5c916399.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:468654bd-56ea-45fc-a2cf-10d801b41373` | 31 | 30 | 83 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/468654bd-56ea-45fc-a2cf-10d801b41373.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:71bf4086-96b2-4e9d-8e12-a2425ae45cb9` | 2 | 2 | 7 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/71bf4086-96b2-4e9d-8e12-a2425ae45cb9.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:9414a071-22c6-43f3-a78a-f902e6e737f9` | 29 | 28 | 92 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/9414a071-22c6-43f3-a78a-f902e6e737f9.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:995380cd-8e50-4864-9f95-d7828281c356` | 2 | 2 | 6 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/995380cd-8e50-4864-9f95-d7828281c356.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:b39c4319-d5fd-4b3d-9ae5-0121fcfbb33d` | 9 | 8 | 16 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/b39c4319-d5fd-4b3d-9ae5-0121fcfbb33d.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:b88b9beb-6142-4281-8252-024550643729` | 2 | 2 | 5 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/b88b9beb-6142-4281-8252-024550643729.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:bc5258bf-546b-457f-a7c3-ca07180b0483` | 2 | 2 | 2 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/bc5258bf-546b-457f-a7c3-ca07180b0483.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:da5033f7-e2ac-4989-b25a-17daea37e2ab` | 13 | 12 | 18 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/da5033f7-e2ac-4989-b25a-17daea37e2ab.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:f816bf64-4545-4da6-b69a-5267cce36c85` | 36 | 35 | 106 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/f816bf64-4545-4da6-b69a-5267cce36c85.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/subagent:fc08d954-e069-43e5-8207-9fe02b76fd1f` | 1 | 1 | 0 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/be6ee399-8665-4f22-8fdd-50ff020c71d8/subagents/fc08d954-e069-43e5-8207-9fe02b76fd1f.jsonl` |
| `Users-ruttanshbhatelia-side-projects-Plugin/eec60fa4-5415-428c-af91-2e593d90f968` | 41 | 40 | 90 | `/Users/ruttanshbhatelia/.cursor/projects/Users-ruttanshbhatelia-side-projects-Plugin/agent-transcripts/eec60fa4-5415-428c-af91-2e593d90f968/eec60fa4-5415-428c-af91-2e593d90f968.jsonl` |
| `empty-window/f816bf64-4545-4da6-b69a-5267cce36c85` | 39 | 38 | 107 | `/Users/ruttanshbhatelia/.cursor/projects/empty-window/agent-transcripts/f816bf64-4545-4da6-b69a-5267cce36c85/f816bf64-4545-4da6-b69a-5267cce36c85.jsonl` |

**Totals if ingested:** 257 messages, 696 tool events.

### DB sessions with no surviving source

**None.** Every `artifacts.path` exists on disk.

### Warp note

Warp ingest intentionally stores **user queries + ActionResult tool events only** (`ai_blocks` empty locally). Conversation count: source 15 = DB 15.

---

## 2. Deep sample (8 per harness, stratified)

Selection per harness: biggest (by artifact size), smallest, oldest `started_at`, newest, most/least messages, plus random fills. Compared by re-running the harness adapter against DB: message counts by role, tool-event counts, session model / timestamps, first & last message text prefixes (200 chars), token sums where present.

### Results summary

| Harness | Samples | Perfect matches | Mismatches |
|---------|--------:|----------------:|-----------:|
| claude | 8 | 8 | 0 |
| codex | 8 (+10 extra random) | 8 (+10) | 0 |
| cursor | 8 (+12 non-drift exact text) | 7 (+12) | **1 stale session** |
| warp | 8 | 8 | 0 |

Token spot-checks (reparse == DB):

- `codex:019e5f63-af08-79e1-86ee-eb583a4e28f0` — 6826 token rows; first/last input/output match.
- `claude:e94c2f3c-0fb8-4c77-8a0d-7147f7bf9709` — 346 rows; Σ input 2,183,250 / Σ output 659,820 match.

### Cursor deep-sample mismatch (stale)

**Session:** `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:31d4a785-79f4-485b-9f3e-9441f995fbee`

| Field | Source (reparse) | DB |
|-------|------------------|-----|
| message_count | 32 | 2 |
| roles | user:1, assistant:31 | user:1, assistant:1 |
| tool_event_count | 60 | 2 |
| session.model | `grok-4.5` | `cursor-grok-4.5-high-fast` |
| session.ended_at | `2026-08-09T12:41:36.132000+00:00` | `2026-08-09T12:33:59.603000+00:00` |
| last_msg.text_prefix | starts with `## Done` / Part A packets | starts with extraction-prompt ack |

Non-drifted Cursor samples matched exactly.

---

## 3. Half-ingestion hunt

### 3a. Sessions with 0 messages — info (by design)

8 sessions, all Claude `skill-injections.jsonl` (`external_id` like `skills:…`). Adapter emits skill_exposures only. Each has 4–144 skill rows.

```sql
SELECT s.id, COUNT(k.id) AS skills, COUNT(m.id) AS msgs
FROM sessions s
LEFT JOIN skill_exposures k ON k.session_id = s.id
LEFT JOIN messages m ON m.session_id = s.id
WHERE s.external_id LIKE 'skills:%'
GROUP BY s.id;
```

### 3b. ended_at < started_at — none

```sql
SELECT COUNT(*) FROM sessions
WHERE started_at IS NOT NULL AND ended_at IS NOT NULL AND ended_at < started_at;
-- 0
```

### 3c. Empty text not flagged tool plumbing — warn (classification)

1,656 messages (claude 113, cursor 1543), all role=assistant.

- **Cursor:** adapter never sets `is_tool_plumbing`; empty assistant turns are typically tool_use-only. Reparse agrees on empty text.
- **Claude:** empty turns include `server_tool_use`, which `content_is_tool_plumbing` does not treat as plumbing.

```sql
SELECT COUNT(*) FROM messages
WHERE (text IS NULL OR trim(text) = '') AND COALESCE(is_tool_plumbing, 0) = 0;
```

### 3d. Message count far below source — critical (Cursor stale)

9 Cursor artifacts where live file size != `artifacts.size`. Aggregate deficit: +323 messages, +463 tool events.

| session_id | DB msgs → source msgs | DB tools → source tools | size delta (bytes) |
|---|---:|---:|---:|
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:49659492-0cb2-496b-852f-bbdb531e7823` | 2 → 66 | 3 → 66 | +25863 |
| `cursor:Users-ruttanshbhatelia-side-projects-jito-mcp/be6ee399-8665-4f22-8fdd-50ff020c71d8` | 336 → 382 | 216 → 236 | +67202 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:e4a4e2a2-925f-4408-bbb8-4b1f263a6a36` | 2 → 47 | 2 → 59 | +262853 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:c146605b-e3fe-4d9b-a59b-96bbedb9dbc1` | 2 → 41 | 2 → 65 | +276262 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:eec60fa4-5415-428c-af91-2e593d90f968` | 2 → 38 | 5 → 90 | +88908 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:b648818c-87b3-402a-a070-d5188e4e6e91` | 2 → 33 | 2 → 52 | +211685 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:31d4a785-79f4-485b-9f3e-9441f995fbee` | 2 → 32 | 2 → 60 | +213383 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/subagent:213bd220-947b-4e46-ad4f-cbad2f62223c` | 2 → 24 | 6 → 68 | +87188 |
| `cursor:Users-ruttanshbhatelia-side-projects-Plugin/be6ee399-8665-4f22-8fdd-50ff020c71d8` | 207 → 217 | 120 → 125 | +35337 |

```sql
SELECT s.id, a.path, a.size, a.parsed_offset,
       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS db_msgs
FROM sessions s
JOIN artifacts a ON a.id = s.artifact_id
WHERE s.harness = 'cursor';
-- then compare a.size to pathlib.Path(a.path).stat().st_size
```

### 3e. Duplicate sessions — none

```sql
SELECT harness, external_id, COUNT(*) c FROM sessions
GROUP BY harness, external_id HAVING c > 1;
SELECT path, COUNT(*) c FROM artifacts GROUP BY path HAVING c > 1;
-- both 0 rows
```

### 3f. Orphaned child sessions — warn

3 Codex children reference parents absent from DB and from `~/.codex/sessions/**` path stems.

| child session_id | missing parent_session_id |
|---|---|
| `codex:019eeaf1-bc84-7d40-99ae-41593d6b17d3` | `019eeaef-3d36-7de1-8938-9e681bff1179` |
| `codex:019f1e41-3bc7-7093-97fc-25ee0a22130c` | `019f1e3d-77f2-7871-b1f5-b418b7ad0b87` |
| `codex:019f387b-8b4b-7fe1-bebf-4ab0905ac2c6` | `019f3879-0354-7e33-aea2-3ad585e4fd6e` |

```sql
SELECT s.id, s.parent_session_id FROM sessions s
WHERE s.parent_session_id IS NOT NULL
AND NOT EXISTS (
  SELECT 1 FROM sessions p
  WHERE p.id = s.parent_session_id
     OR p.external_id = s.parent_session_id
     OR p.id = s.harness || ':' || s.parent_session_id
);
```

### 3g. Partial parse markers — none

```sql
SELECT COUNT(*) FROM artifacts WHERE parsed_offset < size;  -- 0
SELECT COUNT(*) FROM artifacts WHERE parsed_offset > size;  -- 0
```

Stale Cursor files look fully parsed at an outdated size — silent lag, not a crash mid-file.

---

## 4. Cross-table consistency

| Check | Count | Status |
|-------|------:|--------|
| messages orphan session | 0 | pass |
| tool_events orphan session | 0 | pass |
| tool_events orphan message_id | 0 | pass |
| token_usage orphan session | 0 | pass |
| skill_exposures orphan session | 0 | pass |
| ux_observations orphan window | 0 | pass |
| session_commits orphan session | 0 | pass |
| exchange_windows bad request/response msg | 0 / 0 | pass |
| duplicate (session_id, seq) messages/tools | 0 / 0 | pass |

Auxiliary counts: token_usage=40038, skill_exposures=336, exchange_windows=3268, ux_observations=1837, session_commits=480.

---

## 5. Severity-ranked issues

### Critical

1. **Cursor transcripts never ingested (15 files / ~257 msgs / ~696 tools)** — `CursorAdapter.discover()`=120 vs artifacts=105. Paths in §1.
2. **Cursor half-ingested / stale sessions (9 files / +323 msgs / +463 tools)** — live size ≫ `artifacts.size` with `parsed_offset==size`; deep sample `.../subagent:31d4a785-...` is 2 vs 32 messages.

### Warn

3. **Codex orphan parent references (3)** — parents not in DB and not on disk (§3f).
4. **Empty assistant text without `is_tool_plumbing` (1,656)** — Cursor never flags plumbing; Claude misses `server_tool_use` (§3c).

### Info

5. **Eight Claude `skills:*` sessions have 0 messages** — expected; skill exposures present.
6. **Warp has no assistant message text** — expected given empty `ai_blocks`.
7. **Claude / Codex / Warp coverage and deep samples clean** — token sums match on spot checks.
8. **No DB sessions point at deleted artifact paths**; no duplicate path ingest.

---

## Protocol notes

- DB opened as `sqlite3.connect(f"file:{{path}}?mode=ro", uri=True)`; Cursor `state.vscdb` and Warp `warp.sqlite` also read-only.
- Coverage used each adapter `discover()` (Claude skips `journal.jsonl`; Cursor only `*/agent-transcripts/**/*.jsonl`).
- Deep samples used full-file reparse through production adapters.
- Half-ingestion checked 0-message sessions, timestamp inversion, empty text, parsed_offset vs size, live size drift, duplicates, orphan parents.
- Cross-table checked messages, tool_events, token_usage, skill_exposures, ux_observations, session_commits, exchange_windows.

**Remediation (out of scope):** re-run Cursor ingest (full reparse for drifted artifacts; discover picks up the 15 new files). Optionally tighten plumbing flags afterward.
