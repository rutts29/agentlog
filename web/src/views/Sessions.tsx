import { useOutletContext, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchSessions } from "@/lib/api";
import { Card, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/EmptyState";
import { harnessColor } from "@/lib/utils";

type Ctx = { range: string };

export function Sessions() {
  const { range } = useOutletContext<Ctx>();
  const [params] = useSearchParams();
  const model = params.get("model") ?? undefined;
  const harness = params.get("harness") ?? undefined;
  const q = useQuery({
    queryKey: ["sessions", range, model, harness],
    queryFn: () => fetchSessions(range, { model, harness }),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (q.isError) {
    return (
      <EmptyState
        title="Could not load sessions"
        body="The API did not return a sessions list. Is agentlog serve running?"
      />
    );
  }

  const items = q.data!.items;
  const modelFilter = params.get("model");

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold">Sessions</h1>
        <div className="tabular text-[13px] text-muted-foreground">
          {q.data!.total.toLocaleString()} in range
        </div>
      </div>
      {modelFilter ? (
        <div className="text-[12px] text-muted-foreground">
          Filtered context from overview: model={modelFilter}. Full facet
          filters land in a follow-up; table below is the range ledger.
        </div>
      ) : null}
      <Card className="p-0 overflow-hidden">
        <div className="border-b border-border px-4 py-3">
          <CardTitle>Ledger</CardTitle>
        </div>
        {items.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No sessions in this range"
              body="Widen the time range to see historical sessions."
            />
          </div>
        ) : (
          <table className="w-full text-left text-[13px]">
            <thead className="bg-muted text-[11px] uppercase tracking-wide text-faint-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Start</th>
                <th className="px-4 py-2 font-medium">Harness</th>
                <th className="px-4 py-2 font-medium">Model</th>
                <th className="px-4 py-2 font-medium">Project</th>
                <th className="px-4 py-2 font-medium">Msgs</th>
                <th className="px-4 py-2 font-medium">Tools</th>
                <th className="px-4 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className="border-t border-border/60">
                  <td className="px-4 py-2 tabular text-muted-foreground">
                    {s.started_at
                      ? new Date(s.started_at).toLocaleString()
                      : "—"}
                  </td>
                  <td className="px-4 py-2">
                    <span
                      className="mr-1.5 inline-block h-1.5 w-1.5 rounded-full"
                      style={{ background: harnessColor(s.harness) }}
                    />
                    {s.harness}
                  </td>
                  <td className="px-4 py-2 font-mono text-[12px]">{s.model}</td>
                  <td className="px-4 py-2">{s.project}</td>
                  <td className="px-4 py-2 tabular">{s.message_count}</td>
                  <td className="px-4 py-2 tabular">{s.tool_count}</td>
                  <td className="px-4 py-2 text-muted-foreground">{s.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
