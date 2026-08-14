# agentlog operations (macOS)

User-level `launchd` keeps the transcript watcher and dashboard API running across
reboots and process deaths. No `sudo`, nothing system-wide.

## Install

From the project root (with `.venv` present and the package editable-installed):

```bash
.venv/bin/agentlog service install
.venv/bin/agentlog service status
```

This writes:

| LaunchAgent | Role |
|---|---|
| `~/Library/LaunchAgents/com.agentlog.watch.plist` | `python -m agentlog.watch` |
| `~/Library/LaunchAgents/com.agentlog.api.plist` | API on `http://127.0.0.1:3000` |

Both use the absolute `.venv` interpreter, `RunAtLoad`, `KeepAlive`,
`ProcessType=Background`, and a mild `Nice` so ingest stays out of the way.

Re-run `service install` after moving the project or recreating `.venv` so the
baked-in absolute paths stay correct.

## Uninstall

```bash
.venv/bin/agentlog service uninstall
```

## Day-to-day

```bash
.venv/bin/agentlog service status
.venv/bin/agentlog service stop
.venv/bin/agentlog service start
```

`status` reports loaded/not, PID, last exit status, log paths, watcher presence
freshness, and last ingest time.

## Logs

Primary rotating JSON/line logs (5 MB × 5 files):

- `~/.agentlog/logs/watch.log`
- `~/.agentlog/logs/api.log`

launchd also captures stdout/stderr next to those files (`*.stdout.log`,
`*.stderr.log`).

## Health

```bash
curl -s http://127.0.0.1:3000/api/health | python -m json.tool
```

`degraded: true` with a `reason` means the watcher heartbeat (`presence.json`) is
stale or missing, or the DB is unreachable. The API process can still answer
while the watcher is down.

## Recovery

1. `agentlog service status` — note PIDs and `reason`.
2. Check `~/.agentlog/logs/watch.log` / `api.log` for traceback.
3. `agentlog service stop` then `agentlog service start` (or `install` to rewrite plists).
4. If port 3000 is wedged: `lsof -i :3000`, stop the API service, free the port, start again.
5. Watcher catch-up runs on every start, so a gap while the Mac slept or the
   daemon was dead is filled without waiting for a new file event.

## What reboot means

`RunAtLoad` + user LaunchAgents start at login. A full reboot is not required to
validate: confirm services are loaded, kill a PID and watch `KeepAlive` replace
it, and check `/api/health` after stop/start.
