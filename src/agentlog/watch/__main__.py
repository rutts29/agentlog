from __future__ import annotations

import argparse
import signal
from pathlib import Path

from agentlog.config import (
    DEFAULT_DB_PATH,
    WATCH_DEBOUNCE_SECONDS,
    WATCH_POLL_SECONDS,
)
from agentlog.service.logging_setup import configure_daemon_logging, log_file_from_env
from agentlog.watch.daemon import WatchDaemon
from agentlog.watch.sources import existing_watch_roots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m agentlog.watch",
        description="Watch agent transcript sources and keep agentlog.db current.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite database path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=WATCH_DEBOUNCE_SECONDS,
        help=f"Quiet period seconds per harness (default: {WATCH_DEBOUNCE_SECONDS})",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=WATCH_POLL_SECONDS,
        help=f"Poll interval for SQLite sources (default: {WATCH_POLL_SECONDS})",
    )
    parser.add_argument(
        "--no-watchdog",
        action="store_true",
        help="Disable FSEvents/watchdog; poll all sources",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Rotating log file (default: AGENTLOG_LOG_FILE or stdout)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    configure_daemon_logging(
        args.log_file or log_file_from_env(),
        verbose=args.verbose,
    )

    sources = existing_watch_roots()
    if args.no_watchdog:
        sources = [
            type(s)(harness=s.harness, path=s.path, poll=True) for s in sources
        ]

    daemon = WatchDaemon(
        db_path=args.db,
        sources=sources,
        debounce_seconds=args.debounce,
        poll_seconds=args.poll,
        use_watchdog=not args.no_watchdog,
    )

    def _stop(_signum: int, _frame: object) -> None:
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
