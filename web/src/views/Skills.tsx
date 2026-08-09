import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchSkills } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { Card, CardTitle } from "@/components/ui/card";

type Ctx = { range: string };

export function Skills() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery({
    queryKey: ["skills", range],
    queryFn: () => fetchSkills(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }

  const data = q.data!;
  const items = data.items as Array<{
    skill: string;
    fires: number;
    sessions: number;
    last_fired: string | null;
  }>;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-[18px] font-semibold">Skills</h1>
        <p className="mt-1 max-w-3xl text-[13px] text-muted-foreground">
          {data.note}
        </p>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Card>
          <CardTitle>Activations</CardTitle>
          <div className="mt-2 tabular text-2xl font-semibold">
            {data.activations.toLocaleString()}
          </div>
        </Card>
        <Card>
          <CardTitle>Distinct fired</CardTitle>
          <div className="mt-2 tabular text-2xl font-semibold">
            {data.distinct_fired}
          </div>
        </Card>
        <Card>
          <CardTitle>With / without contrast</CardTitle>
          <div className="mt-2">
            <EmptyState
              title="Withheld"
              body="Interaction-style contrasts for skills require populated ux_observations and a passing precision gate. No correction or redirect with/without rates are shown."
              className="min-h-0 border-0 bg-transparent p-0"
            />
          </div>
        </Card>
      </div>

      <Card className="p-0 overflow-hidden">
        <div className="border-b border-border px-4 py-3">
          <CardTitle>Activation table</CardTitle>
        </div>
        {items.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No skill exposures in range"
              body="Skill rows appear after ingest records skill_exposures for sessions in this window."
            />
          </div>
        ) : (
          <table className="w-full text-left text-[13px]">
            <thead className="bg-muted text-[11px] uppercase text-faint-foreground">
              <tr>
                <th className="px-4 py-2 font-medium">Skill</th>
                <th className="px-4 py-2 font-medium">Fires</th>
                <th className="px-4 py-2 font-medium">Sessions</th>
                <th className="px-4 py-2 font-medium">Last fired</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.skill} className="border-t border-border/60">
                  <td className="px-4 py-2 font-mono text-[12px]">{s.skill}</td>
                  <td className="px-4 py-2 tabular">{s.fires}</td>
                  <td className="px-4 py-2 tabular">{s.sessions}</td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {s.last_fired
                      ? new Date(s.last_fired).toLocaleDateString()
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
