import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchModelMix } from "@/lib/api";
import { AggregatePanel } from "@/components/AggregatePanel";
import { EmptyState } from "@/components/EmptyState";
import { Card, CardTitle } from "@/components/ui/card";

type Ctx = { range: string };

export function Models() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery({
    queryKey: ["models-page", range],
    queryFn: () => fetchModelMix(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }

  const data = q.data!;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-[18px] font-semibold">{data.title}</h1>
        <p className="mt-1 max-w-3xl text-[13px] text-muted-foreground">
          {data.subtitle}
        </p>
      </div>

      <Card>
        <CardTitle>Estimated spend</CardTitle>
        <div className="mt-3">
          <EmptyState
            title="Cost not available"
            body="Token and cost fields are not yet normalized in the ledger. No estimated spend chart is shown."
            missing={["normalized token usage", "versioned pricing table"]}
          />
        </div>
      </Card>

      <div className="space-y-3">
        {data.items.slice(0, 12).map((item) => (
          <Card key={`${item.harness}-${item.model}`}>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <div className="font-mono text-[13px]">{item.model}</div>
                <div className="mt-1 text-[12px] text-muted-foreground">
                  {item.harness} · {item.sessions} sessions ·{" "}
                  {(item.share * 100).toFixed(1)}% share
                </div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {item.flags.map((f) => (
                    <span
                      key={f}
                      className="rounded-control border border-border px-1.5 py-0.5 text-[11px] text-status-warn"
                    >
                      {f}
                    </span>
                  ))}
                </div>
              </div>
              <div className="min-w-[280px] flex-1">
                <AggregatePanel
                  title="Redirect / brake (interaction style)"
                  cell={item.interaction_style}
                />
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
