import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchInsights } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { Card } from "@/components/ui/card";

type Ctx = { range: string };

export function Insights() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery({
    queryKey: ["insights", range],
    queryFn: () => fetchInsights(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }

  const data = q.data!;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-[18px] font-semibold">Insights</h1>
        <p className="mt-1 max-w-3xl text-[13px] text-muted-foreground">
          Derived claims only — each card carries evidence, confidence, and
          confounder flags. Causal language appears only for a properly enrolled
          randomized experiment, scoped to that comparison.
        </p>
      </div>

      {data.items.length === 0 ? (
        <Card>
          <EmptyState
            title={data.empty.title}
            body={data.empty.body}
            missing={data.empty.missing}
          />
        </Card>
      ) : null}

      <Card className="border-status-info/30">
        <div className="text-[13px] font-medium text-foreground">
          Causal claims boundary
        </div>
        <p className="mt-2 text-[12px] leading-relaxed text-muted-foreground">
          This experiment surface (when present) randomizes one pre-registered
          comparison between two owner-chosen models on eligible tasks. It does
          not validate the full model × harness × effort × task matrix, and it
          does not make other historical cells causal.
        </p>
      </Card>
    </div>
  );
}
