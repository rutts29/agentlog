import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchProposals,
  postProposalDecision,
  type ProposalClaim,
  type ProposalDecision,
  type ProposalRow,
} from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { CopyPath } from "@/components/CopyPath";
import { cn } from "@/lib/utils";

const FILTERS = ["pending", "accepted", "deferred", "rejected", "all"] as const;
type Filter = (typeof FILTERS)[number];

const DECISION_LABEL: Record<ProposalDecision, string> = {
  accepted: "Accept",
  rejected: "Reject",
  deferred: "Defer",
};

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
        "rounded-control border border-border px-2.5 py-1.5 text-[12px]",
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
      return [{ ev, key: `${claim.id}:${idx}` }];
    }),
  );
  if (rows.length === 0) {
    return (
      <div className="text-[13px] text-muted-foreground">
        No quotes stored for this proposal.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-3">
      {rows.slice(0, 12).map(({ ev, key }) => (
        <li key={key} className="border-l border-border pl-3">
          {ev.quote ? (
            <p className="text-[13px] leading-relaxed text-foreground/90">
              “{ev.quote}”
            </p>
          ) : null}
          {ev.session_id ? (
            <Link
              to={{
                pathname: `/sessions/${encodeURIComponent(ev.session_id)}`,
                search: ev.message_id
                  ? `?msg=${encodeURIComponent(ev.message_id)}`
                  : "",
              }}
              className="mt-1 inline-flex text-[12px] text-speaker-human underline decoration-speaker-human/30 underline-offset-4 hover:decoration-speaker-human"
            >
              Open the session
            </Link>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function proposalInstruction(proposal: ProposalRow): string {
  return (
    proposal.suggested_instruction ||
    proposal.proposed_content ||
    ""
  ).trim();
}

function Caveat({ text }: { text: string }) {
  return (
    <aside className="border-l-2 border-speaker-human bg-speaker-human-dim px-3.5 py-3">
      <div className="text-[12px] font-medium text-speaker-human">
        Hold this lightly
      </div>
      <p className="mt-1 text-[13.5px] leading-relaxed text-foreground">{text}</p>
    </aside>
  );
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
  const instruction = proposalInstruction(proposal);
  const detailsId = `proposal-${proposal.id}-details`;

  return (
    <article className="elev-card flex flex-col gap-4 rounded-card border border-border bg-card px-6 py-6">
      <div className="flex items-start justify-between gap-3">
        <h2 className="min-w-0 text-[20px] font-semibold leading-[1.3] tracking-tight text-foreground">
          {proposal.title}
        </h2>
        <span className="shrink-0 pt-1 text-[12px] text-muted-foreground">
          {proposal.status === "pending" ? "Waiting for you" : proposal.status}
        </span>
      </div>

      <p className="max-w-[62ch] whitespace-pre-wrap text-[15px] leading-[1.7] text-foreground/90">
        {proposal.rationale}
      </p>

      {proposal.does_not_prove ? <Caveat text={proposal.does_not_prove} /> : null}

      {instruction ? (
        <div className="rounded-control border border-border bg-stage px-4 py-3.5">
          <div className="text-[12px] font-medium text-muted-foreground">
            Add this to the agent
          </div>
          <p className="mt-1.5 whitespace-pre-wrap text-[14px] leading-[1.6] text-foreground">
            {instruction}
          </p>
        </div>
      ) : null}

      <div className="min-w-0">
        <div className="text-[12px] text-muted-foreground">Would write to</div>
        <CopyPath path={proposal.target_path} className="mt-1" />
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={detailsId}
        className="flex w-full items-center justify-between rounded-control border border-border bg-muted/40 px-3.5 py-2.5 text-left text-[13px] text-foreground hover:bg-muted"
      >
        <span>{open ? "Hide the file change" : "Show the file change"}</span>
        <span aria-hidden className="text-muted-foreground">{open ? "▴" : "▾"}</span>
      </button>

      {open ? (
        <div id={detailsId} className="flex flex-col gap-4 border-t border-border pt-4">
          <DiffBlock diff={proposal.unified_diff} />
          <div>
            <div className="pb-2 text-[12px] text-muted-foreground">From the session</div>
            <EvidenceList claims={proposal.claims} />
          </div>
          <div className="flex flex-wrap gap-2">
            <CopyButton label="Copy diff" value={proposal.unified_diff} />
            <CopyButton label="Copy proposed text" value={proposal.proposed_content} />
          </div>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2 pt-1">
        {(["accepted", "deferred", "rejected"] as ProposalDecision[]).map(
          (decision) => (
            <button
              key={decision}
              type="button"
              disabled={pending || proposal.status === decision}
              onClick={() => onDecide(decision)}
              className={cn(
                "rounded-control border px-3 py-1.5 text-[12px]",
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
      </div>
    </article>
  );
}

export function Proposals() {
  const [filter, setFilter] = useState<Filter>("pending");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const q = useQuery({
    queryKey: ["proposals", filter],
    queryFn: ({ signal }) => fetchProposals(filter, signal),
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
    <div className="mx-auto max-w-[720px] space-y-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Proposals</h1>
        <p className="mt-1 max-w-[58ch] text-[13px] leading-relaxed text-muted-foreground">
          Standing rules for the agent. Nothing is written until you copy it in yourself.
        </p>
      </div>

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
            {f === "pending" ? "waiting" : f}
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
          title={filter === "pending" ? "Nothing waiting" : `No ${filter} proposals`}
          body={
            filter === "pending"
              ? "When a standing agent rule turns up in a session, it lands here for you to accept or skip."
              : "Switch the filter to see other decisions."
          }
          className="min-h-[180px]"
        />
      ) : (
        <div className="flex flex-col gap-4">
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
