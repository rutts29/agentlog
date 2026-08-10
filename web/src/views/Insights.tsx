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
      ? "proposals"
      : "session";
  const signal = signalLabel(card);
  const color = signalColor(card);

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
        <div className="flex items-center justify-between gap-2">
          <span
            className="microlabel inline-flex items-center gap-1.5 text-[10px]"
            style={{ color }}
          >
            <span
              className="inline-block h-[6px] w-[6px] rounded-full"
              style={{ background: color }}
              aria-hidden
            />
            {signal}
          </span>
          {card.confidence !== "ok" ? (
            <span className="microlabel text-[10px] text-status-warn">
              {card.confidence}
            </span>
          ) : null}
        </div>

        <h2 className="text-[17px] font-semibold leading-[1.25] tracking-tight text-foreground">
          {card.title}
        </h2>

        <p className="text-[13px] leading-[1.5] text-muted-foreground">
          {card.body}
        </p>

        <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 pt-0.5 text-[11px] text-faint-foreground">
          {card.sample_size > 1 ? (
            <span className="tabular">n={card.sample_size}</span>
          ) : null}
          {href ? (
            <Link
              to={href}
              className="text-muted-foreground underline decoration-border hover:text-foreground"
            >
              open {linkLabel}
            </Link>
          ) : null}
        </div>

        {card.suggested_instruction ? (
          <pre className="max-h-[120px] overflow-y-auto whitespace-pre-wrap break-words rounded-control border border-border-faint bg-stage px-3 py-2 font-mono text-[11px] leading-[1.5] text-muted-foreground">
            {card.suggested_instruction}
          </pre>
        ) : null}

        {card.does_not_prove ? (
          <details className="group">
            <summary className="cursor-pointer list-none text-[11px] text-faint-foreground hover:text-muted-foreground [&::-webkit-details-marker]:hidden">
              <span className="underline decoration-border">caveat</span>
            </summary>
            <p className="mt-1.5 text-[11px] leading-relaxed text-faint-foreground">
              Does not prove: {card.does_not_prove}
            </p>
          </details>
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
  const sessionFacts = data.items.filter((c) => c.origin === "session");
  const coach = data.items.filter((c) => c.kind === "coach");
  const other = data.items.filter(
    (c) => c.origin !== "session" && c.kind !== "coach",
  );

  return (
    <div className="space-y-5">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-[18px] font-semibold tracking-tight">Insights</h1>
        <span className="max-w-md text-right text-[12px] text-faint-foreground">
          session facts first — caveats tucked under each card
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
          <Section title="From sessions" count={sessionFacts.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {sessionFacts.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Coach suggestions" count={coach.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {coach.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>

          <Section title="Corpus facts" count={other.length}>
            <div className="grid grid-cols-1 gap-3 min-[900px]:grid-cols-2">
              {other.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>
        </>
      )}

      <Card className="border-border-faint">
        <p className="max-w-2xl text-[12px] leading-relaxed text-faint-foreground">
          Descriptive only — no model rankings, no sentiment scores, nothing
          auto-applies to your configs. Expand “caveat” on a card when you want
          the honesty clause.
        </p>
      </Card>
    </div>
  );
}
