from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentlog import __version__
from agentlog.analysis.deterministic import compute_stats
from agentlog.config import DEFAULT_DB_PATH, ensure_db_parent
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.pipeline import ingest_all

app = typer.Typer(
    name="agentlog",
    help="Local-first analysis of AI coding agent transcripts.",
    no_args_is_help=True,
)
session_app = typer.Typer(help="Inspect individual sessions.")
app.add_typer(session_app, name="session")

console = Console()


def _repo(db: Path) -> Repository:
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    return Repository(conn)


@app.callback()
def main(
    ctx: typer.Context,
    db: Path = typer.Option(
        DEFAULT_DB_PATH,
        "--db",
        help="SQLite database path",
        show_default=True,
    ),
) -> None:
    ctx.obj = {"db": db}


@app.command("ingest")
def ingest_cmd(ctx: typer.Context) -> None:
    """Parse all harness transcripts into the local database."""
    repo = _repo(ctx.obj["db"])
    console.print(Panel.fit(f"agentlog {__version__} ingest", border_style="cyan"))
    stats = ingest_all(repo, console=console)
    table = Table(title="Ingest summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Parsed (full)", str(stats.parsed))
    table.add_row("Appended", str(stats.appended))
    table.add_row("Skipped", str(stats.skipped))
    table.add_row("Failed", str(stats.failed))
    table.add_row("Sessions upserted", str(stats.sessions_upserted))
    table.add_row("Warnings", str(len(stats.warnings)))
    console.print(table)
    if stats.warnings:
        console.print(f"\nFirst warnings ({min(10, len(stats.warnings))}):")
        for w in stats.warnings[:10]:
            console.print(f"  - {w}")


@app.command("stats")
def stats_cmd(ctx: typer.Context) -> None:
    """Show counts by harness, date range, and model."""
    repo = _repo(ctx.obj["db"])
    data = compute_stats(repo)

    harness = Table(title="Sessions by harness")
    harness.add_column("Harness")
    harness.add_column("Sessions", justify="right")
    total = 0
    for row in data["by_harness"]:
        harness.add_row(row["harness"], str(row["sessions"]))
        total += int(row["sessions"])
    harness.add_row("total", str(total))
    console.print(harness)

    summary = Table(title="Totals")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("Artifacts", str(data["artifacts"]))
    summary.add_row("Messages", str(data["messages"]))
    summary.add_row("Tool events", str(data["tool_events"]))
    summary.add_row("Skill exposures", str(data["skill_exposures"]))
    summary.add_row("Exchange windows", str(data["exchange_windows"]))
    summary.add_row("First activity", str(data["first_at"] or "-"))
    summary.add_row("Last activity", str(data["last_at"] or "-"))
    console.print(summary)

    models = Table(title="Sessions by model (top 20)")
    models.add_column("Model")
    models.add_column("Sessions", justify="right")
    for row in data["by_model"]:
        models.add_row(str(row["model"]), str(row["sessions"]))
    console.print(models)


@app.command("sessions")
def sessions_cmd(
    ctx: typer.Context,
    harness: Optional[str] = typer.Option(None, "--harness", "-h", help="Filter by harness"),
    since: Optional[str] = typer.Option(
        None, "--since", help="ISO date/time lower bound for started_at"
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List sessions."""
    repo = _repo(ctx.obj["db"])
    rows = repo.list_sessions(harness=harness, since=since, limit=limit)
    table = Table(title="Sessions")
    table.add_column("ID")
    table.add_column("Harness")
    table.add_column("Started")
    table.add_column("Model")
    table.add_column("Msgs", justify="right")
    table.add_column("CWD")
    for row in rows:
        table.add_row(
            row["id"],
            row["harness"],
            str(row["started_at"] or "-"),
            str(row["model"] or "-"),
            str(row["message_count"]),
            str(row["cwd"] or "-")[:60],
        )
    console.print(table)


@session_app.command("show")
def session_show(ctx: typer.Context, session_id: str) -> None:
    """Show session details and recent messages."""
    repo = _repo(ctx.obj["db"])
    # Allow bare external ids by trying prefixes
    row = repo.get_session(session_id)
    if row is None and ":" not in session_id:
        for prefix in ("codex:", "claude:", "cursor:"):
            row = repo.get_session(prefix + session_id)
            if row is not None:
                session_id = prefix + session_id
                break
    if row is None:
        console.print(f"Session not found: {session_id}")
        raise typer.Exit(code=1)

    info = Table(title=f"Session {session_id}", show_header=False)
    info.add_column("Field", style="bold")
    info.add_column("Value")
    for key in (
        "harness",
        "external_id",
        "parent_session_id",
        "started_at",
        "ended_at",
        "cwd",
        "branch",
        "commit_sha",
        "model",
        "effort",
        "repo",
    ):
        info.add_row(key, str(row[key] or "-"))
    console.print(info)

    messages = repo.list_messages(session_id)
    msg_table = Table(title=f"Messages ({len(messages)})")
    msg_table.add_column("Seq", justify="right")
    msg_table.add_column("Role")
    msg_table.add_column("Time")
    msg_table.add_column("Model")
    msg_table.add_column("Text")
    for msg in messages[:40]:
        text = (msg["text"] or "").replace("\n", " ")
        if len(text) > 100:
            text = text[:97] + "..."
        msg_table.add_row(
            str(msg["seq"]),
            msg["role"],
            str(msg["timestamp"] or "-"),
            str(msg["model"] or "-"),
            text,
        )
    console.print(msg_table)
    if len(messages) > 40:
        console.print(f"... {len(messages) - 40} more messages")


@app.command("search")
def search_cmd(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="FTS5 query"),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Full-text search over message text."""
    repo = _repo(ctx.obj["db"])
    try:
        rows = repo.search_messages(query, limit=limit)
    except Exception as exc:  # noqa: BLE001
        console.print(f"Search failed: {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title=f"Search: {query}")
    table.add_column("Session")
    table.add_column("Harness")
    table.add_column("Role")
    table.add_column("Snippet")
    for row in rows:
        table.add_row(
            row["session_id"],
            row["harness"],
            row["role"],
            str(row["snippet"] or "").replace("\n", " "),
        )
    console.print(table)
    if not rows:
        console.print("No matches.")


if __name__ == "__main__":
    app()
