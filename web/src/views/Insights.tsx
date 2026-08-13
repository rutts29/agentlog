import type { ReactNode } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchInsights, type InsightCard } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { rangeViewQueryOptions } from "@/lib/viewQueries";

type Ctx = { range: string };

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

function InsightCardView({ card }: { card: InsightCard }) {
  const href =
    card.href ||
    (card.source === "proposal" ? "/proposals" : null);
  const linkLabel =
    card.source === "proposal" || (href && href.startsWith("/proposals"))
      ? "Review the proposal"
      : "Open the session";

  return (
    <article className="elev-card relative overflow-hidden rounded-card border border-border bg-card px-6 py-6">
      <div
        aria-hidden
        className="absolute inset-y-0 left-0 w-[3px] bg-speaker-human/80"
      />
      <h2 className="text-[20px] font-semibold leading-[1.3] tracking-tight text-foreground">
        {card.title}
      </h2>
      <p className="mt-3 max-w-[62ch] whitespace-pre-wrap text-[15px] leading-[1.7] text-foreground/90">
        {card.body}
      </p>
      {card.does_not_prove ? <div className="mt-4"><Caveat text={card.does_not_prove} /></div> : null}
      {href ? (
        <Link
          to={href}
          className="mt-5 inline-flex text-[13px] text-speaker-human underline decoration-speaker-human/30 underline-offset-4 hover:decoration-speaker-human"
        >
          {linkLabel}
        </Link>
      ) : null}
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
    <section className="space-y-4">
      <h2 className="text-[12px] font-medium uppercase tracking-[0.08em] text-muted-foreground">
        {title}
        <span className="ml-2 tabular text-faint-foreground">{count}</span>
      </h2>
      {children}
    </section>
  );
}

export function Insights() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery(rangeViewQueryOptions({
    queryKey: ["insights", range],
    queryFn: (signal) => fetchInsights(range, signal),
  }));

  if (q.isLoading) {
    return <LoadingOrb label="Reading insights" />;
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
  const forYou = data.items.filter((c) => c.source !== "proposal");
  const forAgent = data.items.filter((c) => c.source === "proposal");

  return (
    <div className="mx-auto max-w-[680px] space-y-8">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Insights</h1>
        <p className="mt-1 max-w-[58ch] text-[13px] leading-relaxed text-muted-foreground">
          Notes for you — how you ran a session, and what to do differently next time.
        </p>
        {range !== "all" ? (
          <p className="mt-2 text-[12px] text-faint-foreground">
            Showing {range}. Standing notes often live under All.
          </p>
        ) : null}
      </div>

      {data.items.length === 0 ? (
        <EmptyState
          title={data.empty.title}
          body={
            range === "all"
              ? data.empty.body
              : `${data.empty.body} If you expected older notes, switch the range to All.`
          }
          missing={data.empty.missing}
          className="min-h-[180px]"
        />
      ) : (
        <>
          <Section title="For you" count={forYou.length}>
            <div className="flex flex-col gap-4">
              {forYou.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>
          <Section title="Also on the proposals board" count={forAgent.length}>
            <div className="flex flex-col gap-4">
              {forAgent.map((card) => (
                <InsightCardView key={card.id} card={card} />
              ))}
            </div>
          </Section>
        </>
      )}
    </div>
  );
}
