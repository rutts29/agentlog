from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentlog import __version__
from agentlog.analysis.deterministic import compute_stats
from agentlog.config import API_TOKEN_ENV_VAR, DEFAULT_DB_PATH, ensure_db_parent
from agentlog.db.repository import Repository
from agentlog.db.schema import connect, init_db
from agentlog.ingest.pipeline import ingest_all

app = typer.Typer(
    name="agentlog",
    help="Local-first analysis of AI coding agent transcripts.",
    no_args_is_help=True,
)
session_app = typer.Typer(help="Inspect individual sessions.")
extract_app = typer.Typer(help="Semantic extraction (derivations, not evidence).")
experiment_app = typer.Typer(
    help="Prospective coin-flip model comparison (opt-in randomization)."
)
service_app = typer.Typer(help="Manage launchd background services (macOS).")
propose_app = typer.Typer(
    help="Reviewable LLM proposals (packet subagents; never auto-applied).",
    invoke_without_command=True,
)
app.add_typer(session_app, name="session")
app.add_typer(extract_app, name="extract")
app.add_typer(experiment_app, name="experiment")
app.add_typer(service_app, name="service")
app.add_typer(propose_app, name="propose")

console = Console()

ASSIGNMENT_CARD_PATH = Path.home() / ".agentlog" / "current_assignment.json"


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
    from agentlog.analysis.derive import run_derive

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
    derive = run_derive(repo.conn)
    console.print(
        f"Derive: updated={derive.windows_updated} "
        f"classified={derive.windows_classified}/{derive.windows_total} "
        f"skipped={derive.skipped}"
    )


@app.command("derive")
def derive_cmd(
    ctx: typer.Context,
    force: bool = typer.Option(
        False,
        "--force",
        help="Reclassify all windows even when the watermark matches",
    ),
) -> None:
    """Refresh deterministic derived layers (classifications, skill index)."""
    from agentlog.analysis.derive import run_derive

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    result = run_derive(conn, force=force)
    console.print(Panel.fit("agentlog derive", border_style="cyan"))
    table = Table(title="Derive summary")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Skipped", str(result.skipped))
    table.add_row("Windows total", str(result.windows_total))
    table.add_row("Windows classified", str(result.windows_classified))
    table.add_row("Windows updated", str(result.windows_updated))
    table.add_row("Run id", result.run_id or "-")
    console.print(table)
    if result.request_kind_counts:
        kinds = Table(title="Request kinds (this pass)")
        kinds.add_column("Kind")
        kinds.add_column("Count", justify="right")
        for kind, count in sorted(
            result.request_kind_counts.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            kinds.add_row(kind, str(count))
        console.print(kinds)
    for note in result.notes:
        console.print(note)


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


@app.command("brief")
def brief_cmd(
    ctx: typer.Context,
    session_id: str = typer.Argument(..., help="Session id (harness:external or bare)"),
) -> None:
    """Print a deterministic Markdown handoff brief for a session."""
    from agentlog.analysis.briefs import (
        build_session_brief,
        render_brief_markdown,
        resolve_session,
    )

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    row = resolve_session(conn, session_id)
    if row is None:
        console.print(f"Session not found: {session_id}")
        raise typer.Exit(code=1)
    brief = build_session_brief(conn, str(row["id"]))
    if brief is None:
        console.print(f"Session not found: {session_id}")
        raise typer.Exit(code=1)
    # Plain stdout for paste-into-session use (no Rich wrapping).
    typer.echo(render_brief_markdown(brief), nl=False)


@app.command("claims")
def claims_cmd(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(
        None,
        "--status",
        help="Filter: candidate|approved|rejected|published|superseded (default: all)",
    ),
    kind: Optional[str] = typer.Option(None, "--kind"),
    derivation: Optional[str] = typer.Option(
        None, "--derivation", help="deterministic|llm_derived"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-derive claims and proposals before listing"
    ),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List evidence-backed config learnings (claims). Never writes config files."""
    from agentlog.analysis.claims import list_claims, refresh_learnings

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    if refresh:
        stats = refresh_learnings(conn)
        conn.commit()
        console.print(
            Panel.fit(
                f"claims={stats['claims_total']} proposals={stats['proposals_total']}",
                border_style="cyan",
            )
        )
        console.print(stats["by_kind"])
    items = list_claims(
        conn,
        status=status,
        kind=kind,
        derivation=derivation,
        include_evidence=False,
        limit=limit,
    )
    table = Table(title="Claims")
    table.add_column("Kind")
    table.add_column("Subject")
    table.add_column("Derivation")
    table.add_column("Support")
    table.add_column("n", justify="right")
    table.add_column("Rate")
    for c in items:
        rate = f"{c.rate:.4f}" if c.rate is not None else "-"
        table.add_row(
            c.kind,
            c.subject[:40],
            c.derivation,
            c.support_status,
            str(c.sample_size),
            rate,
        )
    console.print(table)
    console.print(
        "Language: observational only. LLM-derived claims need adjudication."
    )


@app.command("insights-import")
def insights_import_cmd(
    ctx: typer.Context,
    packet: Path = typer.Argument(..., help="Validated session-fact JSON packet"),
    model: str = typer.Option(..., "--model", help="Model that authored the facts"),
) -> None:
    """Import evidence-linked LLM session facts into the local claims ledger."""
    from agentlog.analysis.insights import import_session_fact_packet

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        stats = import_session_fact_packet(conn, packet, model=model)
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        console.print(f"[red]Import rejected:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Imported {stats['claims']} evidence-verified session facts "
        f"from {stats['run_id']} ({stats['model']})."
    )


@propose_app.callback(invoke_without_command=True)
def propose_cmd(
    ctx: typer.Context,
    refresh: bool = typer.Option(
        True, "--refresh/--no-refresh", help="Re-derive claims; prune static board spam"
    ),
    status: Optional[str] = typer.Option(
        "pending",
        "--status",
        help="pending|accepted|rejected|deferred|superseded",
    ),
    show: Optional[str] = typer.Option(
        None, "--show", help="Print full rationale+diff for a proposal id"
    ),
    accept: Optional[str] = typer.Option(
        None, "--accept", help="Mark proposal id Accepted (records a decision only)"
    ),
    reject: Optional[str] = typer.Option(None, "--reject", help="Reject proposal id"),
    defer: Optional[str] = typer.Option(None, "--defer", help="Defer proposal id"),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Reviewable config proposals. agentlog never writes a config file.

    Board cards come from LLM packet ingest
    (``propose packets-emit`` → Cursor subagents → ``propose packets-ingest``),
    not static archive/usage templates.
    """
    if ctx.invoked_subcommand is not None:
        return

    from agentlog.analysis.claims import (
        get_proposal,
        list_proposals,
        refresh_learnings,
        set_proposal_status,
    )

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")

    decisions = [("accepted", accept), ("rejected", reject), ("deferred", defer)]
    chosen = [(name, pid) for name, pid in decisions if pid]

    if refresh and not show and not chosen:
        stats = refresh_learnings(conn)
        conn.commit()
        console.print(
            f"Refreshed: claims={stats['claims_total']} "
            f"static_proposals={stats['proposals_total']} "
            f"pruned={stats['proposals_pruned']}"
        )
        if stats.get("empty_board_hint"):
            console.print(f"[dim]{stats['empty_board_hint']}[/dim]")

    if chosen:
        for name, pid in chosen:
            prop = set_proposal_status(conn, pid, name)  # type: ignore[arg-type]
            conn.commit()
            console.print(f"{name.title()} {prop.id}: {prop.title}")
        return
    if show:
        prop = get_proposal(conn, show, include_claims=True)
        if prop is None:
            console.print(f"Proposal not found: {show}")
            raise typer.Exit(code=1)
        console.print(Panel.fit(prop.title, border_style="cyan"))
        console.print(f"status={prop.status} action={prop.action}")
        console.print(f"target={prop.target_path}")
        console.print(f"sample_size={prop.sample_size}")
        if prop.model or prop.run_id:
            console.print(
                f"provenance model={prop.model} run_id={prop.run_id} "
                f"pack={prop.evidence_pack_hash}"
            )
        console.print("\n## Rationale\n")
        console.print(prop.rationale)
        console.print("\n## Diff\n")
        console.print(prop.unified_diff)
        return

    items = list_proposals(conn, status=status, include_claims=False, limit=limit)
    table = Table(title="Proposals")
    table.add_column("Id")
    table.add_column("Status")
    table.add_column("Action")
    table.add_column("Title")
    table.add_column("Target")
    table.add_column("n", justify="right")
    for p in items:
        table.add_row(
            p.id,
            p.status,
            p.action,
            p.title[:48],
            Path(p.target_path).name,
            str(p.sample_size),
        )
    console.print(table)
    if not items and status == "pending":
        console.print(
            "[dim]No proposals met evidence gates. "
            "Run: agentlog propose packets-emit --run-dir .research/proposals-run-001[/dim]"
        )
    console.print(
        "Inspect: agentlog propose --show ID · Decide: --accept / --reject / "
        "--defer ID. agentlog proposes; you edit the file yourself."
    )


@propose_app.command("packets-emit")
def propose_packets_emit_cmd(
    ctx: typer.Context,
    run_dir: Path = typer.Option(
        Path(".research/proposals-run-001"),
        "--run-dir",
        help="Directory for packets/manifest (created if missing)",
    ),
    model: str = typer.Option(
        "cursor-grok-4.5-high-fast",
        "--model",
        help="Model hint recorded in packets (Cursor subagent slug)",
    ),
    windows_per_theme: int = typer.Option(
        12, "--windows-per-theme", help="Max stratified windows per theme packet"
    ),
    no_resume: bool = typer.Option(
        False, "--no-resume", help="Rewrite run dir even if manifest exists"
    ),
) -> None:
    """Emit stratified evidence packets for Cursor subagent proposal authors."""
    from agentlog.analysis.claims.packets import emit_proposal_packet_run

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    manifest = emit_proposal_packet_run(
        conn,
        run_dir,
        model=model,
        windows_per_theme=windows_per_theme,
        resume=not no_resume,
    )
    console.print(
        f"Emitted {manifest['packet_count']} packets / "
        f"{manifest['window_count']} windows → {run_dir}"
    )
    console.print(
        "Next: run Cursor subagents on packets/*.json using "
        f"{run_dir / 'proposal_subagent.md'}, write results/*.json, then "
        "agentlog propose packets-ingest"
    )


@propose_app.command("packets-ingest")
def propose_packets_ingest_cmd(
    ctx: typer.Context,
    run_dir: Path = typer.Option(
        Path(".research/proposals-run-001"),
        "--run-dir",
        help="Run directory with packets/ and results/",
    ),
) -> None:
    """Validate subagent result JSON and publish LLM proposals to the board."""
    from agentlog.analysis.claims.packets import (
        packet_run_status,
        publish_llm_proposals_from_run,
    )
    from agentlog.analysis.config_ledger import backup_agentlog_db

    db = ctx.obj["db"]
    ensure_db_parent(db)
    bak = backup_agentlog_db(db, reason="proposal_packets_ingest")
    console.print(f"DB backup: {bak}")
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    stats = publish_llm_proposals_from_run(conn, run_dir)
    console.print_json(
        __import__("json").dumps(
            {k: v for k, v in stats.items() if k != "results"},
            default=str,
        )
    )
    for r in stats.get("results") or []:
        console.print(
            f"  {r['packet_id']}: {r['status']} "
            f"proposals={r.get('proposals', 0)} "
            f"failures={len(r.get('failures') or [])}"
        )
    console.print_json(
        __import__("json").dumps(packet_run_status(run_dir), default=str)
    )


@propose_app.command("packets-status")
def propose_packets_status_cmd(
    run_dir: Path = typer.Option(
        Path(".research/proposals-run-001"),
        "--run-dir",
        help="Proposal packet run directory",
    ),
) -> None:
    """Show per-packet progress for a proposal packet run."""
    from agentlog.analysis.claims.packets import packet_run_status

    status = packet_run_status(run_dir)
    console.print_json(__import__("json").dumps(status, default=str))


@app.command("config-ledger")
def config_ledger_cmd(
    ctx: typer.Context,
    refresh: bool = typer.Option(
        True, "--refresh/--summary", help="Scan/backfill or just print summary"
    ),
    no_git: bool = typer.Option(
        False, "--no-git", help="Skip git history backfill; live scan only"
    ),
    path: Optional[str] = typer.Option(
        None, "--path", help="List snapshots for one config path"
    ),
    limit: int = typer.Option(30, "--limit", "-n"),
) -> None:
    """Snapshot AGENTS.md / CLAUDE.md / rules / skills history (read-only on sources)."""
    from agentlog.analysis.config_ledger import (
        backup_agentlog_db,
        ledger_summary,
        list_snapshots,
        refresh_config_ledger,
    )

    db = ctx.obj["db"]
    ensure_db_parent(db)
    bak = backup_agentlog_db(db, reason="config_ledger")
    console.print(f"DB backup: {bak}")
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    if refresh:
        stats = refresh_config_ledger(conn, include_git_history=not no_git)
        conn.commit()
        console.print(Panel.fit("config ledger refresh", border_style="cyan"))
        console.print(stats)
    summary = ledger_summary(conn)
    console.print(
        f"Tracked paths={summary['paths']} snapshots={summary['snapshots']} "
        f"oldest={summary['oldest']} newest={summary['newest']}"
    )
    console.print(f"by_source={summary['by_source']}")
    rows = list_snapshots(conn, path=path, limit=limit)
    if rows:
        table = Table(title="Recent snapshots")
        table.add_column("When")
        table.add_column("Source")
        table.add_column("Kind")
        table.add_column("Path")
        table.add_column("Hash")
        for r in rows:
            table.add_row(
                str(r.get("observed_at") or "")[:19],
                str(r.get("source") or ""),
                str(r.get("path_kind") or ""),
                Path(str(r.get("path") or "")).name,
                str(r.get("content_hash") or "")[:12],
            )
        console.print(table)


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


@extract_app.command("deterministic")
def extract_deterministic_cmd(ctx: typer.Context) -> None:
    """Run deterministic classification + triage over all exchange windows."""
    import json

    from agentlog.analysis.derive import run_derive

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    result = run_derive(conn, force=True, index_skill_inventory=False)
    console.print(
        Panel.fit(
            f"deterministic run {result.run_id or 'skipped'}",
            border_style="cyan",
        )
    )
    table = Table(title="Request kinds")
    table.add_column("Kind")
    table.add_column("Count", justify="right")
    for kind, count in sorted(
        result.request_kind_counts.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        table.add_row(kind, str(count))
    console.print(table)
    console.print(
        f"Classified {result.windows_classified}/{result.windows_total} "
        f"(updated {result.windows_updated})"
    )
    console.print_json(json.dumps(result.to_dict()))


REMOTE_EGRESS_FLAG = "--allow-remote-egress"
REMOTE_EGRESS_ACK_FLAG = "--egress-acknowledgement"


def _enable_remote_egress_or_exit(
    *, allow: bool, acknowledgement: Optional[str], endpoint: str
) -> None:
    """Turn the process-wide egress gate on, or explain why we will not."""
    from agentlog.safety.egress import (
        ACKNOWLEDGEMENT,
        EGRESS_DISCLOSURE,
        EgressBlocked,
        enable_remote_extraction,
    )

    if not allow:
        return
    console.print(
        Panel.fit(
            f"REMOTE EXTRACTION REQUESTED\n\n{EGRESS_DISCLOSURE}\n\nEndpoint: {endpoint}",
            border_style="red",
        )
    )
    if acknowledgement != ACKNOWLEDGEMENT:
        console.print(
            f"Refusing: {REMOTE_EGRESS_FLAG} also requires "
            f'{REMOTE_EGRESS_ACK_FLAG} "{ACKNOWLEDGEMENT}".'
        )
        console.print(
            "Run 'agentlog extract egress-preview' first to see the exact payload."
        )
        raise typer.Exit(code=2)
    try:
        enable_remote_extraction(
            endpoint=endpoint, acknowledgement=acknowledgement or ""
        )
    except EgressBlocked as exc:
        console.print(str(exc))
        raise typer.Exit(code=2) from exc


@extract_app.command("egress-preview")
def extract_egress_preview_cmd(
    ctx: typer.Context,
    out: Optional[Path] = typer.Option(
        None, "--out", help="Write the full outbound payload here for review"
    ),
    limit: int = typer.Option(5, "--limit", help="Windows to preview (0 = all)"),
    model: str = typer.Option("grok-4.5", "--model"),
    batch_size: int = typer.Option(1, "--batch-size"),
) -> None:
    """Show exactly what remote extraction would transmit. Sends nothing."""
    import json

    from agentlog.analysis.extractors.egress_preview import (
        build_egress_preview,
        preview_summary,
        write_egress_preview,
    )

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    preview = build_egress_preview(
        conn, limit=limit, model=model, batch_size=batch_size
    )
    console.print(
        Panel.fit(
            "No network request was made. This is a dry render of the payload.",
            border_style="cyan",
        )
    )
    console.print_json(json.dumps(preview_summary(preview), default=str))
    if out is not None:
        path = write_egress_preview(preview, out)
        console.print(f"Full payload written to {path}")


@extract_app.command("audit-pack")
def extract_audit_pack_cmd(
    ctx: typer.Context,
    out: Path = typer.Option(
        Path("audit_pack.jsonl"),
        "--out",
        help="Path for unlabeled reviewable JSONL",
    ),
    n: int = typer.Option(100, "--n", help="Sample size"),
    seed: int = typer.Option(42, "--seed"),
) -> None:
    """Emit a stratified UX audit pack for hand labeling."""
    from agentlog.analysis.extractors.pipeline import build_audit_pack

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    ids = build_audit_pack(conn, out, n=n, seed=seed, ux_only=True)
    console.print(f"Wrote {len(ids)} windows to {out}")


@extract_app.command("audit-run")
def extract_audit_run_cmd(
    ctx: typer.Context,
    pack: Path = typer.Option(..., "--pack", help="Audit pack JSONL"),
    gold: Optional[Path] = typer.Option(
        None, "--gold", help="Completed hand labels JSONL"
    ),
    model: str = typer.Option("grok-4.5", "--model"),
    batch_size: int = typer.Option(8, "--compare-batch-size"),
    allow_remote_egress: bool = typer.Option(
        False,
        REMOTE_EGRESS_FLAG,
        help="Send window text to the remote extraction API (off by default)",
    ),
    egress_acknowledgement: Optional[str] = typer.Option(
        None,
        REMOTE_EGRESS_ACK_FLAG,
        help="Exact acknowledgement string required alongside " + REMOTE_EGRESS_FLAG,
    ),
    endpoint: str = typer.Option(
        "https://api.x.ai/v1", "--endpoint", help="Remote extraction base URL"
    ),
) -> None:
    """Run UX extractor on audit pack, score gold, compare batch vs single."""
    import json

    from agentlog.analysis.extractors.pipeline import run_audit_phase

    _enable_remote_egress_or_exit(
        allow=allow_remote_egress,
        acknowledgement=egress_acknowledgement,
        endpoint=endpoint,
    )
    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    gate, run_id, meta = run_audit_phase(
        conn,
        audit_pack=pack,
        gold_path=gold,
        model=model,
        compare_batch_size=batch_size,
    )
    console.print(Panel.fit(f"audit run {run_id}", border_style="cyan"))
    console.print(f"Gate passed: {gate.passed}")
    console.print(f"Recommended batch size: {gate.recommended_batch_size}")
    if gate.batch_disagreement_rate is not None:
        console.print(f"Batch disagreement rate: {gate.batch_disagreement_rate:.4f}")
    if gate.failures:
        console.print("Failures:")
        for f in gate.failures:
            console.print(f"  - {f}")
    if gate.per_label:
        table = Table(title="Precision / recall")
        table.add_column("Label")
        table.add_column("P")
        table.add_column("R")
        table.add_column("TP", justify="right")
        table.add_column("FP", justify="right")
        table.add_column("FN", justify="right")
        for lab, sc in gate.per_label.items():
            table.add_row(
                lab,
                "-" if sc.precision is None else f"{sc.precision:.3f}",
                "-" if sc.recall is None else f"{sc.recall:.3f}",
                str(sc.tp),
                str(sc.fp),
                str(sc.fn),
            )
        console.print(table)
    console.print_json(json.dumps(meta, default=str))


@extract_app.command("packets-emit")
def extract_packets_emit_cmd(
    ctx: typer.Context,
    out: Path = typer.Option(
        ...,
        "--out",
        help="Run directory for packets/manifest (created if missing)",
    ),
    windows_per_packet: int = typer.Option(
        4, "--windows-per-packet", help="Max windows per subagent packet"
    ),
    max_chars: int = typer.Option(
        28_000, "--max-chars", help="Approx char budget per packet"
    ),
    model: str = typer.Option("grok-4.5", "--model", help="Model hint for provenance"),
    skip_labeled: bool = typer.Option(
        False,
        "--skip-labeled/--all-windows",
        help="Exclude windows that already have a linked ux_observations row",
    ),
) -> None:
    """Emit triaged UX work packets for subagent labeling (no API call)."""
    import json

    from agentlog.analysis.extractors.packets import emit_packet_run

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    manifest = emit_packet_run(
        conn,
        out,
        windows_per_packet=windows_per_packet,
        max_chars_per_packet=max_chars,
        model=model,
        ux_only=True,
        skip_labeled=skip_labeled,
        resume=True,
    )
    console.print(
        f"Packet run {manifest['run_id']}: "
        f"{manifest['packet_count']} packets / {manifest['window_count']} windows → {out}"
    )
    console.print_json(json.dumps(manifest, default=str))


@extract_app.command("packets-ingest")
def extract_packets_ingest_cmd(
    ctx: typer.Context,
    run_dir: Path = typer.Option(..., "--run-dir", help="Packet run directory"),
    results_dir: Optional[Path] = typer.Option(
        None,
        "--results-dir",
        help="Optional inbox of pkt_XXXX.json results (default: run_dir/results_inbox)",
    ),
    model: str = typer.Option("grok-4.5", "--model"),
) -> None:
    """Validate subagent result files and write ux_observations (hard reject)."""
    import json

    from agentlog.analysis.extractors.packets import ingest_packet_results, packet_run_status

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    conn.execute("PRAGMA busy_timeout = 30000")
    results = ingest_packet_results(
        conn, run_dir, results_dir=results_dir, model=model
    )
    rejected = [r for r in results if r.status == "rejected"]
    completed = [r for r in results if r.status == "completed" and r.accepted]
    console.print(
        f"Ingested: {len(completed)} newly completed, {len(rejected)} rejected, "
        f"{len(results)} examined"
    )
    for r in rejected:
        console.print(f"  REJECT {r.packet_id}:")
        for f in r.failures:
            console.print(f"    - {f.reason}" + (f" ({f.window_id})" if f.window_id else ""))
    console.print_json(json.dumps(packet_run_status(run_dir), default=str))


@extract_app.command("restore-labels")
def extract_restore_labels_cmd(
    ctx: typer.Context,
    run_dir: Path = typer.Option(
        ...,
        "--run-dir",
        help="Extraction run dir with packets/ and results/",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Match only; do not write ux_observations"
    ),
) -> None:
    """Rebuild ux_observations from on-disk packet results (durable content match)."""
    import json

    from agentlog.analysis.extractors.restore_labels import restore_from_run_dir

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    census = restore_from_run_dir(conn, run_dir, dry_run=dry_run)
    console.print_json(json.dumps(census.to_dict(), default=str))


@extract_app.command("packets-status")
def extract_packets_status_cmd(
    run_dir: Path = typer.Option(..., "--run-dir", help="Packet run directory"),
) -> None:
    """Show per-packet progress for a packet extraction run."""
    import json

    from agentlog.analysis.extractors.packets import packet_run_status

    status = packet_run_status(run_dir)
    table = Table(title=f"Packet run {status.get('run_id')}")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for st, n in sorted((status.get("status_counts") or {}).items()):
        table.add_row(st, str(n))
    console.print(table)
    pending = [
        pid
        for pid, meta in (status.get("packets") or {}).items()
        if meta.get("status") == "pending"
    ]
    if pending:
        console.print(f"Pending packets: {', '.join(pending)}")
    console.print_json(json.dumps(status, default=str))


@extract_app.command("label")
def extract_label_cmd(
    pack: Path = typer.Option(..., "--pack", help="Unlabeled audit pack JSONL"),
    gold: Path = typer.Option(..., "--gold", help="Gold labels JSONL (created/updated)"),
) -> None:
    """Fast terminal hand-labeler for the audit gate (stdlib UI, no model shown)."""
    from agentlog.analysis.extractors.labeling import run_labeling_loop

    run_labeling_loop(pack, gold)


@extract_app.command("ux-full")
def extract_ux_full_cmd(
    ctx: typer.Context,
    authorize: bool = typer.Option(
        False,
        "--authorize",
        help="Owner authorization required in addition to audit gate pass",
    ),
    gate_json: Optional[Path] = typer.Option(
        None, "--gate-json", help="Prior audit gate result JSON"
    ),
    model: str = typer.Option("grok-4.5", "--model"),
    batch_size: int = typer.Option(1, "--batch-size"),
    allow_remote_egress: bool = typer.Option(
        False,
        REMOTE_EGRESS_FLAG,
        help="Send window text to the remote extraction API (off by default)",
    ),
    egress_acknowledgement: Optional[str] = typer.Option(
        None,
        REMOTE_EGRESS_ACK_FLAG,
        help="Exact acknowledgement string required alongside " + REMOTE_EGRESS_FLAG,
    ),
    endpoint: str = typer.Option(
        "https://api.x.ai/v1", "--endpoint", help="Remote extraction base URL"
    ),
) -> None:
    """Full-corpus UX LLM extract — blocked unless audit gate passed and authorized."""
    import json

    from agentlog.analysis.extractors.audit import AuditGateResult, LabelScore
    from agentlog.analysis.extractors.pipeline import run_full_ux_extract

    _enable_remote_egress_or_exit(
        allow=allow_remote_egress,
        acknowledgement=egress_acknowledgement,
        endpoint=endpoint,
    )
    if not authorize:
        console.print("Refusing: pass --authorize after audit gate passes.")
        raise typer.Exit(code=2)
    if gate_json is None or not gate_json.exists():
        console.print("Refusing: provide --gate-json from a passing audit-run.")
        raise typer.Exit(code=2)
    raw = json.loads(gate_json.read_text())
    per_label = {
        k: LabelScore(
            label=k,
            tp=int(v.get("tp", 0)),
            fp=int(v.get("fp", 0)),
            fn=int(v.get("fn", 0)),
        )
        for k, v in (raw.get("per_label") or {}).items()
    }
    gate = AuditGateResult(
        passed=bool(raw.get("passed")),
        per_label=per_label,
        failures=list(raw.get("failures") or []),
        batch_disagreement_rate=raw.get("batch_disagreement_rate"),
        recommended_batch_size=int(raw.get("recommended_batch_size") or 1),
    )
    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    try:
        run_id = run_full_ux_extract(
            conn,
            model=model,
            batch_size=batch_size,
            owner_authorized=authorize,
            gate=gate,
        )
    except RuntimeError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print(f"Full UX run completed: {run_id}")


def _experiment_service(db: Path):
    from agentlog.analysis.performance.experiments import ExperimentService

    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    return ExperimentService(conn)


@experiment_app.command("register")
def experiment_register_cmd(
    ctx: typer.Context,
    model_a: str = typer.Option(..., "--model-a", help="First shortlist model"),
    model_b: str = typer.Option(..., "--model-b", help="Second shortlist model"),
    harness: str = typer.Option(..., "--harness", help="Harness for the experiment"),
    tasks: str = typer.Option(
        "debug,feature_existing,refactor",
        "--tasks",
        help="Comma-separated eligible primary tasks",
    ),
    target_n: int = typer.Option(16, "--target-n", help="Target root sessions per arm"),
    supersedes: Optional[str] = typer.Option(
        None, "--supersedes", help="Prior experiment id when versioning a protocol"
    ),
) -> None:
    """Freeze a pre-registration before any coin flips."""
    from agentlog.analysis.performance.outcomes import (
        DIRECTIONAL_LICENSE_NOTE,
        PRIMARY_OUTCOME,
        SCOPE_LIMITATION,
    )

    svc = _experiment_service(ctx.obj["db"])
    eligible = [t.strip() for t in tasks.split(",") if t.strip()]
    exp = svc.register(
        model_a=model_a,
        model_b=model_b,
        harness=harness,
        eligible_tasks=eligible,
        target_n_per_arm=target_n,
        supersedes_id=supersedes,
    )
    console.print(Panel.fit("Experiment pre-registered", border_style="cyan"))
    console.print(f"id: {exp['id']}")
    console.print(f"pre_registration_hash: {exp['pre_registration_hash']}")
    console.print(f"primary: {PRIMARY_OUTCOME.name} ({PRIMARY_OUTCOME.direction})")
    console.print(f"license: {exp['primary_metric_license']}")
    console.print(SCOPE_LIMITATION)
    console.print(DIRECTIONAL_LICENSE_NOTE)


@experiment_app.command("flip")
def experiment_flip_cmd(
    ctx: typer.Context,
    task: str = typer.Option(..., "--task", help="Primary task label for this root"),
    affirm: bool = typer.Option(
        False,
        "--affirm",
        help="Affirm the task is comparable on either shortlist model (required)",
    ),
    experiment_id: Optional[str] = typer.Option(
        None, "--experiment", help="Experiment id (defaults to active)"
    ),
    harness: Optional[str] = typer.Option(
        None, "--harness", help="Override harness (defaults to experiment harness)"
    ),
) -> None:
    """Coin-flip an eligible task and print which model to use."""
    from agentlog.analysis.performance.outcomes import SCOPE_LIMITATION

    svc = _experiment_service(ctx.obj["db"])
    exp = svc.get_experiment(experiment_id) if experiment_id else svc.active_experiment()
    if exp is None:
        console.print("No open experiment. Run: agentlog experiment register ...")
        raise typer.Exit(code=1)
    if not affirm:
        console.print(
            "Refusing: pass --affirm after confirming either shortlist model is acceptable."
        )
        raise typer.Exit(code=2)

    result = svc.enroll_and_assign(
        experiment_id=str(exp["id"]),
        primary_task=task,
        harness=harness or str(exp["harness"]),
        owner_affirm_comparable=True,
        both_models_available=True,
    )
    if not result["enrolled"]:
        console.print(Panel.fit("Not enrolled", border_style="yellow"))
        console.print(f"reasons: {', '.join(result['eligibility']['reasons'])}")
        raise typer.Exit(code=1)

    card = svc.write_assignment_card(result, ASSIGNMENT_CARD_PATH)
    console.print(Panel.fit("Coin flip assignment", border_style="green"))
    console.print(f"USE MODEL: {result['assigned_model']}")
    console.print(f"assignment_id: {result['assignment_id']}")
    console.print(result["instruction"])
    console.print(f"Assignment card: {card}")
    console.print(SCOPE_LIMITATION)


@experiment_app.command("link")
def experiment_link_cmd(
    ctx: typer.Context,
    assignment_id: str = typer.Option(..., "--assignment"),
    session_id: str = typer.Option(..., "--session"),
) -> None:
    """Bind an assignment to the root session that actually ran."""
    svc = _experiment_service(ctx.obj["db"])
    try:
        asg = svc.link_session(assignment_id=assignment_id, root_session_id=session_id)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print(f"Linked. compliance_status={asg['compliance_status']}")
    if asg.get("as_treated_model"):
        console.print(f"as_treated_model={asg['as_treated_model']}")


@experiment_app.command("sync-compliance")
def experiment_sync_compliance_cmd(
    ctx: typer.Context,
    experiment_id: Optional[str] = typer.Option(None, "--experiment"),
) -> None:
    """Detect compliance from transcripts (not self-report)."""
    svc = _experiment_service(ctx.obj["db"])
    exp = svc.get_experiment(experiment_id) if experiment_id else svc.active_experiment()
    if exp is None:
        console.print("No experiment found.")
        raise typer.Exit(code=1)
    rows = svc.sync_all_compliance(str(exp["id"]))
    table = Table(title="Compliance")
    table.add_column("Assignment")
    table.add_column("Assigned")
    table.add_column("As treated")
    table.add_column("Status")
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row["assigned_model"]),
            str(row.get("as_treated_model") or "-"),
            str(row["compliance_status"]),
        )
    console.print(table)


@experiment_app.command("status")
def experiment_status_cmd(
    ctx: typer.Context,
    experiment_id: Optional[str] = typer.Option(None, "--experiment"),
) -> None:
    """Show enrollment progress toward the pre-registered target."""
    svc = _experiment_service(ctx.obj["db"])
    exp = svc.get_experiment(experiment_id) if experiment_id else svc.active_experiment()
    if exp is None:
        console.print("No experiment found.")
        raise typer.Exit(code=1)
    progress = svc.enrollment_progress(str(exp["id"]))
    console.print(Panel.fit("Experiment status", border_style="cyan"))
    console.print(f"id: {progress['experiment_id']}")
    console.print(f"status: {progress['status']}")
    console.print(
        f"primary: {progress['primary_metric']} "
        f"({progress['primary_metric_direction']}; "
        f"license={progress['primary_metric_license']})"
    )
    for model, n in progress["counts"].items():
        console.print(f"  {model}: {n} / {progress['target_n_per_arm']}")
    console.print(progress["scope_limitation"])
    console.print(progress["directional_license_note"])
    if not progress["reached_target"]:
        console.print("No causal claim yet — under target enrollment.")


@experiment_app.command("analyze")
def experiment_analyze_cmd(
    ctx: typer.Context,
    experiment_id: Optional[str] = typer.Option(None, "--experiment"),
) -> None:
    """Report ITT (primary) and per-protocol (secondary) for the frozen primary outcome."""
    import json

    svc = _experiment_service(ctx.obj["db"])
    exp = svc.get_experiment(experiment_id) if experiment_id else svc.active_experiment()
    if exp is None:
        console.print("No experiment found.")
        raise typer.Exit(code=1)
    report = svc.analyze(str(exp["id"]))
    console.print(Panel.fit(f"Analysis ({report.claim_status})", border_style="cyan"))
    console.print(report.claim_language)
    console.print(report.scope_limitation)
    console.print(report.directional_license_note)
    console.print(report.per_protocol_bias_note)
    console.print_json(json.dumps(report.to_dict(), default=str))


@app.command("serve")
def serve_cmd(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8722, "--port"),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Rotating log file (default: AGENTLOG_LOG_FILE or stderr)",
    ),
    allow_remote_access: bool = typer.Option(
        False,
        "--allow-remote-access",
        help="Permit a non-loopback bind. Requires a token; exposes transcripts.",
    ),
    token: Optional[str] = typer.Option(
        None,
        "--token",
        help=f"API token for this process (or set {API_TOKEN_ENV_VAR})",
    ),
    rotate_token: bool = typer.Option(
        False,
        "--rotate-token",
        help="Regenerate ~/.agentlog/api_token before serving",
    ),
) -> None:
    """Serve the dashboard API on loopback.

    The API returns full transcript text and has mutating endpoints; it is not
    read-only. A local token is always required (auto-created under
    ~/.agentlog/api_token). Non-loopback binds also need --allow-remote-access.
    """
    import os

    import uvicorn

    from agentlog.api.app import create_app
    from agentlog.api.local_token import resolve_serve_token
    from agentlog.api.security import BindPolicyViolation, resolve_bind
    from agentlog.service.logging_setup import configure_daemon_logging, log_file_from_env

    db_path = Path(ctx.obj["db"])
    import logging

    serve_token = resolve_serve_token(
        cli_token=token,
        env_token=os.environ.get(API_TOKEN_ENV_VAR),
        rotate=rotate_token,
    )
    try:
        decision = resolve_bind(
            host=host,
            port=port,
            allow_remote_access=allow_remote_access,
            token=serve_token.token,
        )
    except BindPolicyViolation as exc:
        console.print(Panel.fit(str(exc), border_style="red"))
        raise typer.Exit(code=2) from exc

    configure_daemon_logging(log_file or log_file_from_env())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    for warning in decision.warnings:
        console.print(Panel.fit(f"WARNING\n\n{warning}", border_style="red"))
    if serve_token.path is not None:
        auth = f"token file {serve_token.path} ({serve_token.source})"
    else:
        auth = f"token from {serve_token.source}"
    console.print(
        Panel.fit(
            f"agentlog serve  http://{host}:{port}\n"
            f"db (read-only): {db_path}\n"
            f"access: {auth}\n"
            "SPA injects the token; curl needs Authorization: Bearer …",
            border_style="cyan",
        )
    )
    uvicorn.run(
        create_app(db_path, security=decision.security()),
        host=host,
        port=port,
        log_level="info",
    )


@app.command("api-token")
def api_token_cmd(
    rotate: bool = typer.Option(
        False, "--rotate", help="Regenerate ~/.agentlog/api_token (0600)"
    ),
    show: bool = typer.Option(
        False, "--show", help="Print the token value (sensitive)"
    ),
) -> None:
    """Manage the local dashboard API token file."""
    from agentlog.api.local_token import ensure_token_file

    token, path, created = ensure_token_file(rotate=rotate)
    action = "rotated" if rotate else ("created" if created else "existing")
    console.print(f"{action}: {path} (mode 0600)")
    if show:
        console.print(token)
    else:
        console.print("pass --show to print the secret; SPA/Vite pick it up automatically")


@service_app.command("install")
def service_install_cmd(ctx: typer.Context) -> None:
    """Install and start user LaunchAgents for watch + API (port 8787)."""
    from agentlog.service.launchd import install_services

    try:
        result = install_services(db_path=ctx.obj["db"])
    except FileNotFoundError as exc:
        console.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print(Panel.fit("agentlog services installed", border_style="green"))
    console.print(f"project: {result['project_root']}")
    console.print(f"python:  {result['python']}")
    console.print(f"db:      {result['db']}")
    console.print(f"logs:    {result['log_dir']}")
    for label, plist in result["plists"].items():
        console.print(f"  {label}: {plist}")
    if result["errors"]:
        console.print("Warnings:")
        for err in result["errors"]:
            console.print(f"  - {err}")
        raise typer.Exit(code=1)


@service_app.command("uninstall")
def service_uninstall_cmd() -> None:
    """Unload LaunchAgents and remove their plists."""
    from agentlog.service.launchd import uninstall_services

    result = uninstall_services()
    console.print(Panel.fit("agentlog services uninstalled", border_style="yellow"))
    if result["removed"]:
        for path in result["removed"]:
            console.print(f"  removed {path}")
    else:
        console.print("  (no plists were present)")


@service_app.command("start")
def service_start_cmd() -> None:
    """Start (or restart) installed LaunchAgents."""
    from agentlog.service.launchd import start_services

    result = start_services()
    console.print(Panel.fit("agentlog services start", border_style="cyan"))
    for label in result["started"]:
        console.print(f"  started {label}")
    if result["errors"]:
        for err in result["errors"]:
            console.print(f"  - {err}")
        raise typer.Exit(code=1)


@service_app.command("stop")
def service_stop_cmd() -> None:
    """Stop LaunchAgents (bootout; KeepAlive will not restart until start/install)."""
    from agentlog.service.launchd import stop_services

    result = stop_services()
    console.print(Panel.fit("agentlog services stopped", border_style="yellow"))
    for label in result["stopped"]:
        console.print(f"  stopped {label}")


@service_app.command("status")
def service_status_cmd(ctx: typer.Context) -> None:
    """Show load state, PIDs, logs, and watcher ingest freshness."""
    from agentlog.service.launchd import service_status

    status = service_status(db_path=ctx.obj["db"])
    console.print(Panel.fit("agentlog service status", border_style="cyan"))
    console.print(f"db:   {status['db']}")
    console.print(f"logs: {status['log_dir']}")
    for label, row in status["services"].items():
        table = Table(title=label)
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("loaded", "yes" if row.get("loaded") else "no")
        table.add_row("pid", str(row.get("pid") or "-"))
        table.add_row("last_exit_status", str(row.get("last_exit_status")))
        table.add_row("state", str(row.get("state") or "-"))
        table.add_row("plist", str(row.get("plist") or "-"))
        table.add_row("log_path", str(row.get("log_path") or "-"))
        if label.endswith(".watch"):
            table.add_row("watcher_alive", str(row.get("watcher_alive")))
            table.add_row("presence_fresh", str(row.get("presence_fresh")))
            table.add_row(
                "presence_age_seconds", str(row.get("presence_age_seconds"))
            )
            table.add_row("last_ingest_at", str(row.get("last_ingest_at") or "-"))
        else:
            table.add_row("port", str(row.get("port") or "-"))
        console.print(table)
    health = status["health"]
    console.print(
        f"health: degraded={health.get('degraded')} reason={health.get('reason')}"
    )


if __name__ == "__main__":
    app()
