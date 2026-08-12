import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchProposals,
  postProposalDecision,
  type ProposalClaim,
  type ProposalDecision,
  type ProposalRow,
  type ProposalSupport,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { Card } from "@/components/ui/card";
import { CopyPath } from "@/components/CopyPath";
import { StatusDot } from "@/components/ui/badges";
import { cn } from "@/lib/utils";

const FILTERS = ["pending", "accepted", "deferred", "rejected", "all"] as const;
type Filter = (typeof FILTERS)[number];

const DECISION_LABEL: Record<ProposalDecision, string> = {
  accepted: "Accept",
  rejected: "Reject",
  deferred: "Defer",
};

const SUPPORT_TONE: Record<
  ProposalSupport["tier"],
  "ok" | "warn" | "error" | "info" | "neutral"
> = {
  ok: "ok",
  insufficient: "warn",
  abstain: "neutral",
  unsupported: "neutral",
};

function shortTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toISOString().slice(0, 16).replace("T", " ");
}

function CopyButton({
  label,
  value,
  disabled,
}: {
  label: string;
  value: string | null | undefined;
  disabled?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  const inert = disabled || !value;
  return (
    <button
      type="button"
      disabled={inert}
      onClick={async () => {
        if (!value) return;
        try {
          await navigator.clipboard.writeText(value);
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        } catch {
          setCopied(false);
        }
      }}
      className={cn(
        "rounded-control border border-border px-2 py-1 text-[11px]",
        inert
          ? "text-faint-foreground"
          : "text-muted-foreground hover:text-foreground",
      )}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

function DiffBlock({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  if (lines.length === 1 && lines[0] === "") {
    return (
      <div className="rounded-control border border-dashed border-border bg-stage px-3 py-4 text-center font-mono text-[11px] text-faint-foreground">
        No diff stored for this proposal
      </div>
    );
  }
  return (
    <pre className="max-h-[320px] max-w-full overflow-x-hidden overflow-y-auto whitespace-pre-wrap break-words rounded-control border border-border bg-stage px-3 py-2 font-mono text-[11px] leading-[1.55]">
      {lines.map((line, i) => {
        let color = "var(--muted-foreground)";
        if (line.startsWith("+++") || line.startsWith("---")) {
          color = "var(--faint-foreground)";
        } else if (line.startsWith("@@")) {
          color = "var(--status-info)";
        } else if (line.startsWith("+")) {
          color = "var(--status-ok)";
        } else if (line.startsWith("-")) {
          color = "var(--status-error)";
        }
        return (
          <div key={i} className="min-w-0 break-words [overflow-wrap:anywhere]" style={{ color }}>
            {line === "" ? " " : line}
          </div>
        );
      })}
    </pre>
  );
}

function EvidenceList({ claims }: { claims: ProposalClaim[] }) {
  const seen = new Set<string>();
  const rows = claims.flatMap((claim) =>
    claim.evidence.flatMap((ev, idx) => {
      const identity = [ev.session_id, ev.window_id, ev.message_id, ev.quote].join("\0");
      if (seen.has(identity)) return [];
      seen.add(identity);
      return [{ claim, ev, key: `${claim.id}:${idx}` }];
    }),
  );
  if (rows.length === 0) {
    return (
      <div className="text-[12px] text-faint-foreground">
        No verbatim spans stored for the linked claims.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {rows.slice(0, 12).map(({ claim, ev, key }) => (
        <li key={key} className="border-l border-border pl-3">
          <div className="flex flex-wrap items-baseline gap-2 text-[11px] text-faint-foreground">
            <span className="tabular">{shortTime(ev.timestamp)}</span>
            {ev.harness ? <span>{ev.harness}</span> : null}
            <span className="font-mono">{claim.kind}</span>
            {ev.session_id ? (
              <Link
                to={{
                  pathname: `/sessions/${encodeURIComponent(ev.session_id)}`,
                  search: ev.message_id
                    ? `?msg=${encodeURIComponent(ev.message_id)}`
                    : "",
                }}
                className="text-muted-foreground underline decoration-border hover:text-foreground"
              >
                open transcript
              </Link>
            ) : null}
            {ev.window_id ? (
              <span className="font-mono">window {ev.window_id.slice(0, 12)}</span>
            ) : null}
          </div>
          {ev.quote ? (
            <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">
              “{ev.quote}”
            </p>
          ) : null}
        </li>
      ))}
      {rows.length > 12 ? (
        <li className="text-[11px] text-faint-foreground">
          {rows.length - 12} further citations not shown
        </li>
      ) : null}
    </ul>
  );
}

function SupportLine({ support }: { support: ProposalSupport }) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
      <StatusDot
        tone={SUPPORT_TONE[support.tier]}
        label={`support: ${support.tier}`}
      />
      <span className="tabular text-[11px] text-muted-foreground">
        n={support.n} · processed={support.processed ?? "—"} · eligible={support.eligible ?? "—"} · citations={support.citations}
      </span>
      <span className="text-[12px] text-faint-foreground">
        {support.derivations.join(", ") || "no derivation recorded"}
      </span>
    </div>
  );
}

function ProvenanceLine({ proposal }: { proposal: ProposalRow }) {
  const source = proposal.provenance_summary;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint-foreground">
      <StatusDot
        tone={source.kind === "llm_derived" ? "info" : source.kind === "legacy_unverified" ? "warn" : "neutral"}
        label={source.kind === "llm_derived" ? "LLM-derived" : source.kind === "legacy_unverified" ? "legacy / unverified" : "deterministic"}
      />
      {source.synthesis_model || source.model ? (
        <span className="font-mono">synthesis {source.synthesis_model || source.model}</span>
      ) : null}
      {source.review_model ? <span className="font-mono">review {source.review_model}</span> : null}
      {source.run_id ? <span>run {source.run_id}</span> : null}
      {source.packet_id ? <span>packet {source.packet_id}</span> : null}
      {source.catalog_id ? <span>catalog {source.catalog_id}</span> : null}
      {source.review_id ? <span>review {source.review_id}</span> : null}
      <span>{source.review_state}</span>
      {proposal.coalesced_duplicate_count > 1 ? (
        <span className="text-muted-foreground">
          {proposal.coalesced_duplicate_count} exact versions coalesced
        </span>
      ) : null}
    </div>
  );
}

function InstructionBlock({ proposal }: { proposal: ProposalRow }) {
  const instruction = proposal.suggested_instruction;
  return (
    <div className="rounded-control border border-border-faint bg-stage px-3 py-2">
      <div className="microlabel text-[10px] text-faint-foreground">
        Atomic suggested instruction
      </div>
      <p className="mt-1 whitespace-pre-wrap text-[12px] leading-relaxed text-foreground">
        {instruction || "See the proposed file content below; no atomic instruction was stored."}
      </p>
    </div>
  );
}

function TargetStateLine({ proposal }: { proposal: ProposalRow }) {
  const st = proposal.target_state;
  if (proposal.status !== "accepted") return null;
  if (!st.exists) {
    return (
      <StatusDot tone="neutral" label="target file does not exist yet" />
    );
  }
  if (st.matches_proposed) {
    return (
      <StatusDot
        tone="ok"
        label="file content now matches the proposed content"
      />
    );
  }
  if (st.changed_since_proposal) {
    return (
      <StatusDot
        tone="warn"
        label="file changed since the proposal, but does not match it"
      />
    );
  }
  return <StatusDot tone="neutral" label="file unchanged since the proposal" />;
}

function ProposalCard({
  proposal,
  onDecide,
  pending,
}: {
  proposal: ProposalRow;
  onDecide: (decision: ProposalDecision) => void;
  pending: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Card className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls={`proposal-${proposal.id}-details`}
            className="text-left text-[13px] font-medium text-foreground hover:underline"
          >
            {proposal.title}
          </button>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] text-faint-foreground">
            <span className="microlabel">{proposal.action}</span>
            <span className="microlabel">{proposal.target_kind}</span>
            <span className="microlabel">{proposal.scope_type}</span>
            <span className="tabular">created {shortTime(proposal.created_at)}</span>
          </div>
        </div>
        <span className="microlabel shrink-0 text-[10px] text-muted-foreground">
          {proposal.status}
        </span>
      </div>

      <ProvenanceLine proposal={proposal} />
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
        <div className="min-w-0">
          <CopyPath path={proposal.target_path} />
          <div className="mt-1 text-[11px] text-faint-foreground">
            target scope: {proposal.scope_type}{proposal.scope_id ? ` / ${proposal.scope_id}` : ""}
          </div>
        </div>
        <TargetStateLine proposal={proposal} />
      </div>
      <InstructionBlock proposal={proposal} />
      <SupportLine support={proposal.support} />

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={`proposal-${proposal.id}-details`}
          className="rounded-control border border-border px-2.5 py-1 text-[11px] text-muted-foreground hover:text-foreground"
        >
          {open ? "Hide evidence & diff" : "Evidence & diff"}
        </button>
        <CopyButton label="Copy diff" value={proposal.unified_diff} />
        <CopyButton
          label="Copy proposed file"
          value={proposal.proposed_content}
        />
        <span className="ml-auto flex items-center gap-2">
          {(["accepted", "deferred", "rejected"] as ProposalDecision[]).map(
            (decision) => (
              <button
                key={decision}
                type="button"
                disabled={pending || proposal.status === decision}
                onClick={() => onDecide(decision)}
                className={cn(
                  "rounded-control border px-2.5 py-1 text-[11px]",
                  proposal.status === decision
                    ? "border-border bg-muted text-foreground"
                    : "border-border text-muted-foreground hover:text-foreground",
                  pending && "opacity-60",
                )}
              >
                {DECISION_LABEL[decision]}
              </button>
            ),
          )}
        </span>
      </div>

      {proposal.decision_note ? (
        <div className="text-[11px] text-faint-foreground">
          {proposal.decision_note.startsWith("auto-")
            ? `system-pruned, not your decision: ${proposal.decision_note}`
            : `note: ${proposal.decision_note}`}
        </div>
      ) : null}

      {open ? (
        <div id={`proposal-${proposal.id}-details`} className="flex flex-col gap-3 border-t border-border pt-3">
          <div>
            <div className="microlabel pb-1 text-[10px] text-faint-foreground">
              Proposed diff
            </div>
            <DiffBlock diff={proposal.unified_diff} />
          </div>
          <div>
            <div className="microlabel pb-1 text-[10px] text-faint-foreground">
              Rationale
            </div>
            <pre className="max-w-full overflow-x-hidden whitespace-pre-wrap break-words font-sans text-[12px] leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">
              {proposal.rationale}
            </pre>
          </div>
          <div>
            <div className="microlabel pb-1 text-[10px] text-faint-foreground">
              Evidence
            </div>
            <EvidenceList claims={proposal.claims} />
          </div>
          {proposal.does_not_prove ? (
            <div>
              <div className="microlabel pb-1 text-[10px] text-faint-foreground">
                What this does not prove
              </div>
              <p className="text-[12px] leading-relaxed text-muted-foreground">
                {proposal.does_not_prove}
              </p>
            </div>
          ) : null}
          {proposal.support.language ? (
            <p className="text-[11px] text-faint-foreground">
              Caveat: {proposal.support.language}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}

export function Proposals() {
  const [filter, setFilter] = useState<Filter>("pending");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const q = useQuery({
    queryKey: ["proposals", filter],
    queryFn: () => fetchProposals(filter),
  });

  const decide = useMutation({
    mutationFn: ({
      id,
      decision,
    }: {
      id: string;
      decision: ProposalDecision;
    }) => postProposalDecision(id, decision),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ["proposals"] });
    },
    onError: (err: unknown) =>
      setError(err instanceof Error ? err.message : "decision failed"),
  });

  const counts = q.data?.counts_by_status ?? {};
  const total = useMemo(
    () => Object.values(counts).reduce((a, b) => a + b, 0),
    [counts],
  );

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-[18px] font-semibold tracking-tight">Proposals</h1>
        <span className="max-w-xl text-right text-[12px] text-faint-foreground">
          agentlog proposes, you apply — nothing here edits your configuration
        </span>
      </div>

      <Card className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <div className="col-span-2 border-b border-border-faint pb-2 sm:col-span-5">
          <div className="flex items-baseline justify-between gap-3">
            <div className="text-[13px] font-medium text-foreground">Review queue</div>
            <span className="microlabel text-[10px]" style={{ color: "var(--status-info)" }}>
              manual application only
            </span>
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
          Provenance identifies packet-derived, deterministic, and legacy records. Nothing here writes AGENTS.md or skill files.
          </p>
        </div>
        {(["pending", "accepted", "deferred", "rejected"] as const).map((status) => (
          <div key={status}>
            <div className="microlabel text-[10px] text-faint-foreground">{status}</div>
            <div className="mt-1 tabular text-[20px] font-semibold text-foreground">{counts[status] ?? 0}</div>
          </div>
        ))}
        <div>
          <div className="microlabel text-[10px] text-faint-foreground">visible total</div>
          <div className="mt-1 tabular text-[20px] font-semibold text-foreground">{total}</div>
        </div>
      </Card>

      <div role="tablist" aria-label="Proposal status" className="flex flex-wrap items-center gap-1 rounded-control border border-border bg-card p-0.5">
        {FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            onClick={() => setFilter(f)}
            role="tab"
            aria-selected={filter === f}
            className={cn(
              "tabular rounded-[5px] px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground",
              filter === f && "bg-muted text-foreground shadow-sm",
            )}
          >
            {f}
            <span className="ml-1.5 text-faint-foreground">
              {f === "all" ? total : (counts[f] ?? 0)}
            </span>
          </button>
        ))}
      </div>

      {error ? (
        <div className="rounded-control border border-border px-3 py-2 text-[12px]"
          style={{ color: "var(--status-error)" }}
        >
          {error}
        </div>
      ) : null}

      {q.isLoading ? (
        <LoadingOrb label="Reading proposals" compact />
      ) : !q.data ? (
        <EmptyState
          title="Could not load proposals"
          body="The proposals endpoint failed — refresh or check agentlog serve."
        />
      ) : q.data.items.length === 0 ? (
        <EmptyState
          title={`No ${filter === "all" ? "" : filter} proposals`}
          body={
            filter === "pending"
            ? "No proposals met evidence gates. Cards come from transcript evidence packets, with provenance shown per record; they are not static unused-skill or usage-profile templates."
              : "Proposals appear after packet ingest clears evidence gates; provenance distinguishes reviewed packet records from legacy and deterministic records."
          }
          className="min-h-[180px]"
        />
      ) : (
        <div className="flex flex-col gap-3">
          {q.data.items.map((p) => (
            <ProposalCard
              key={p.id}
              proposal={p}
              pending={decide.isPending}
              onDecide={(decision) =>
                decide.mutate({ id: p.id, decision })
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}
