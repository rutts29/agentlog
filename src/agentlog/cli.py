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
extract_app = typer.Typer(help="Semantic extraction (derivations, not evidence).")
experiment_app = typer.Typer(
    help="Prospective coin-flip model comparison (opt-in randomization)."
)
app.add_typer(session_app, name="session")
app.add_typer(extract_app, name="extract")
app.add_typer(experiment_app, name="experiment")

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


@extract_app.command("deterministic")
def extract_deterministic_cmd(ctx: typer.Context) -> None:
    """Run deterministic classification + triage over all exchange windows."""
    import json

    from agentlog.analysis.extractors.pipeline import run_deterministic_phase

    db = ctx.obj["db"]
    ensure_db_parent(db)
    conn = connect(db)
    init_db(conn)
    report, run_id = run_deterministic_phase(conn)
    console.print(Panel.fit(f"deterministic run {run_id}", border_style="cyan"))
    data = report.to_dict()
    table = Table(title="Triage routes")
    table.add_column("Route")
    table.add_column("Count", justify="right")
    for route, count in data["route_counts"].items():
        table.add_row(route, str(count))
    console.print(table)

    rules = Table(title="Per-rule hits (a window may match multiple)")
    rules.add_column("Rule")
    rules.add_column("Hits", justify="right")
    for rule, count in data["rule_hits"].items():
        rules.add_row(rule, str(count))
    console.print(rules)

    harness = Table(title="By harness × route")
    harness.add_column("Harness")
    harness.add_column("Route")
    harness.add_column("Count", justify="right")
    for h, routes in data["by_harness_route"].items():
        for route, count in routes.items():
            harness.add_row(h, route, str(count))
    console.print(harness)
    console.print(f"UX-eligible: {data['ux_eligible']} / {data['total']}")
    console.print_json(json.dumps(data))


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
) -> None:
    """Run UX extractor on audit pack, score gold, compare batch vs single."""
    import json

    from agentlog.analysis.extractors.pipeline import run_audit_phase

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
) -> None:
    """Emit triaged UX work packets for subagent labeling (no API call)."""
    import json

    from agentlog.analysis.extractors.packets import emit_packet_run

    db = ctx.obj["db"]
    conn = connect(db)
    init_db(conn)
    manifest = emit_packet_run(
        conn,
        out,
        windows_per_packet=windows_per_packet,
        max_chars_per_packet=max_chars,
        model=model,
        ux_only=True,
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
) -> None:
    """Full-corpus UX LLM extract — blocked unless audit gate passed and authorized."""
    import json

    from agentlog.analysis.extractors.audit import AuditGateResult, LabelScore
    from agentlog.analysis.extractors.pipeline import run_full_ux_extract

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


if __name__ == "__main__":
    app()
