import type { ReactNode } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchInsights, type InsightCard } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Ctx = { range: string };

function signalLabel(card: InsightCard): string {
  if (card.theme) return card.theme.replace(/_/g, " ");
  return card.kind;
}

function insightTypeLabel(card: InsightCard): string {
  if (card.insight_type === "observed_instance") return "observed instance";
  if (card.insight_type === "corpus_pattern") {
    if (card.processing_coverage_state === "partial") return "sampled-run finding";
    if (card.proof_capability_caveat) return "evidence-limited finding";
    return "corpus pattern";
  }
  return "coach proposal";
}

function provenanceLabel(card: InsightCard): string {
  const provenance = card.provenance;
  const extractor = provenance.extractor
    ? `${provenance.extractor}${
        provenance.extractor_version ? `@${provenance.extractor_version}` : ""
      }`
    : provenance.derivation;
  return [
    extractor,
    provenance.source ? `source ${provenance.source}` : null,
    provenance.synthesis_model
      ? `synthesis ${provenance.synthesis_model}`
      : provenance.model
        ? `model ${provenance.model}`
        : null,
    provenance.review_model ? `review ${provenance.review_model}` : null,
    provenance.run_id ? `run ${provenance.run_id}` : null,
    provenance.review_id ? `review ${provenance.review_id}` : null,
    provenance.review_state,
  ]
    .filter(Boolean)
    .join(" · ");
}

function signalColor(card: InsightCard): string {
  const key = (card.theme || card.kind).toLowerCase();
  if (key.includes("miss") || key.includes("skip")) return "var(--status-error)";
  if (key.includes("follow") || key === "ok") return "var(--status-ok)";
  if (key.includes("skill")) return "var(--status-info)";
  if (key.includes("coach") || key.includes("process")) return "var(--accent-live)";
  if (key.includes("usage") || key.includes("repeat")) return "var(--status-warn)";
  return "var(--muted-foreground)";
}

function InsightCardView({ card }: { card: InsightCard }) {
  const href =
    card.href ||
    (card.source === "proposal" ? "/proposals" : null);
  const linkLabel =
    card.source === "proposal" || (href && href.startsWith("/proposals"))
      ? "review proposal"
      : "open evidence";
  const signal = signalLabel(card);
  const color = signalColor(card);
  const provenance = provenanceLabel(card);
  const hasRunCoverage =
    card.supporting_roots != null &&
    card.processed_roots != null &&
    card.eligible_roots != null &&
    card.coverage_state != null;

  return (
    <article
      className={cn(
        "elev-card flex flex-col overflow-hidden rounded-card border border-border bg-card",
      )}
      style={{
        borderLeftWidth: 3,
        borderLeftColor: color,
      }}
    >
      <div
        className="h-[3px] w-full opacity-70"
        style={{
          background: `linear-gradient(90deg, ${color}, transparent 70%)`,
        }}
        aria-hidden
      />
      <div className="flex flex-1 flex-col gap-2.5 p-4 pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className="microlabel inline-flex items-center gap-1.5 text-[10px]"
              style={{ color }}
            >
              <span
                className="inline-block h-[6px] w-[6px] rounded-full"
                style={{ background: color }}
                aria-hidden
              />
              {insightTypeLabel(card)}
            </span>
            <span className="text-[10px] text-faint-foreground">{signal}</span>
          </div>
          <span className="microlabel text-[10px] text-faint-foreground">
            {card.review_state} · support {card.confidence}
          </span>
        </div>

        <h2 className="text-[17px] font-semibold leading-[1.25] tracking-tight text-foreground">
          {card.title}
        </h2>

        <p className="text-[13px] leading-[1.5] text-muted-foreground">
          {card.body}
        </p>

        {hasRunCoverage ? (
          <div
            className={cn(
              "flex flex-wrap items-center justify-between gap-2 rounded-control border px-3 py-2 text-[11px]",
              card.coverage_state === "partial"
                ? "border-status-warn/40 bg-status-warn/5 text-status-warn"
                : "border-status-ok/30 bg-status-ok/5 text-muted-foreground",
            )}
          >
            <span className="tabular font-medium">
              {card.supporting_roots} supporting / {card.processed_roots}{" "}
              processed / {card.eligible_roots} eligible
            </span>
            <span className="font-mono">
              coverage={card.coverage_state}
              {card.processing_coverage_state !== card.coverage_state
                ? ` · processing=${card.processing_coverage_state}`
                : ""}
            </span>
          </div>
        ) : card.coverage ? (
          <p className="text-[11px] leading-relaxed text-faint-foreground">
            Coverage: {card.coverage}
          </p>
        ) : null}

        {card.selection_method ? (
          <p className="text-[11px] leading-relaxed text-faint-foreground">
            Selection: {card.selection_method.replace(/_/g, " ")}
          </p>
        ) : null}

        {card.selection_caveat ? (
          <p className="text-[11px] leading-relaxed text-status-warn">
            Selection caveat: {card.selection_caveat}
          </p>
        ) : null}

        {card.proof_capability_by_harness ? (
          <div className="flex flex-wrap gap-1.5 text-[10px] text-faint-foreground">
            {Object.entries(card.proof_capability_by_harness).map(
              ([harness, capability]) => (
                <span
                  key={harness}
                  className="rounded-control border border-border-faint px-2 py-1 font-mono"
                >
                  {harness}: {capability.level}
                  {capability.proof_capable_roots != null &&
                  capability.eligible_roots != null
                    ? ` · proof ${capability.proof_capable_roots}/${capability.eligible_roots}`
                    : ""}
                  {capability.processed_roots != null &&
                  capability.eligible_roots != null
                    ? ` · processed ${capability.processed_roots}/${capability.eligible_roots}`
                    : ""}
                </span>
              ),
            )}
          </div>
        ) : null}

        {card.proof_capability_caveat ? (
          <p className="text-[11px] leading-relaxed text-status-warn">
            Proof-capability caveat: {card.proof_capability_caveat}
          </p>
        ) : null}

        {card.sampling_gate ? (
          <p className="text-[11px] leading-relaxed text-faint-foreground">
            Calibrated sampling gate: {card.sampling_gate}
          </p>
        ) : null}

        <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5 text-[11px] text-faint-foreground">
          {!hasRunCoverage ? (
            <span className="tabular">
              n={card.sample_size}
              {card.denominator != null ? `/${card.denominator}` : ""}
            </span>
          ) : null}
          <span className="tabular">
            {card.evidence_count}{" "}
            {card.evidence_count === 1 ? "citation" : "citations"}
          </span>
          {href ? (
            <Link
              to={href}
              className="text-muted-foreground underline decoration-border hover:text-foreground"
            >
              {linkLabel}
            </Link>
          ) : null}
        </div>

        {card.suggested_instruction ? (
          <pre className="max-h-[120px] overflow-y-auto whitespace-pre-wrap break-words rounded-control border border-border-faint bg-stage px-3 py-2 font-mono text-[11px] leading-[1.5] text-muted-foreground">
            {card.suggested_instruction}
          </pre>
        ) : null}

        {card.does_not_prove ? (
          <p className="border-t border-border-faint pt-2 text-[11px] leading-relaxed text-faint-foreground">
            Caveat: {card.does_not_prove}
          </p>
        ) : null}

        {provenance ? (
          <p className="break-words font-mono text-[10px] leading-relaxed text-faint-foreground">
            provenance: {provenance}
          </p>
        ) : null}
      </div>
    </article>
  );
}

function Section({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  if (count === 0) return null;
  return (
    <section className="space-y-2.5">
      <div className="flex items-baseline gap-2">
        <h2 className="microlabel text-muted-foreground">{title}</h2>
        <span className="tabular text-[11px] text-faint-foreground">{count}</span>
      </div>
      {children}
    </section>
  );
}

export function Insights() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery({
    queryKey: ["insights", range],
    queryFn: () => fetchInsights(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (!q.data) {
    return (
      <EmptyState
        title="Could not load insights"
        body="The insights endpoint failed — refresh or check agentlog serve."
      />
    );
  }

  const data = q.data;
  const observedInstances = data.items.filter(
    (c) => c.insight_type === "observed_instance",
  );
  const completePatterns = data.items.filter(
    (c) =>
      c.insight_type === "corpus_pattern" && c.coverage_state === "complete",
  );
  const sampledFindings = data.items.filter(
    (c) =>
      c.insight_type === "corpus_pattern" &&
      c.processing_coverage_state === "partial",
  );
  const evidenceLimitedFindings = data.items.filter(
    (c) =>
      c.insight_type === "corpus_pattern" &&
      c.processing_coverage_state === "complete" &&
      c.coverage_state === "partial",
  );
  const otherCorpusFindings = data.items.filter(
    (c) => c.insight_type === "corpus_pattern" && c.coverage_state == null,
  );
  const coachProposals = data.items.filter(
    (c) => c.insight_type === "coach_proposal",
  );

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Insights</h1>
        <span className="max-w-md text-right text-[12px] text-faint-foreground">
          observed instances are evidence, not corpus-wide patterns
        </span>
      </div>

      {data.items.length === 0 ? (
        <EmptyState
          title={data.empty.title}
          body={data.empty.body}
          missing={data.empty.missing}
          className="min-h-[180px]"
        />
      ) : (
        <>
          <Section title="Observed instances" count={observedInstances.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {observedInstances.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Complete corpus patterns" count={completePatterns.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {completePatterns.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Sampled-run findings" count={sampledFindings.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {sampledFindings.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section
            title="Evidence-limited findings"
            count={evidenceLimitedFindings.length}
          >
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {evidenceLimitedFindings.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Other reviewed facts" count={otherCorpusFindings.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {otherCorpusFindings.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Coach proposals" count={coachProposals.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {coachProposals.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>
        </>
      )}

      <Card className="border-border-faint">
        <p className="max-w-2xl text-[12px] leading-relaxed text-faint-foreground">
          Descriptive only — no model rankings, no sentiment scores, nothing
          auto-applies to your configs. Every card exposes its review state,
          support boundary, caveat, provenance, and available transcript evidence.
        </p>
      </Card>
    </div>
  );
}
