import { useOutletContext } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { fetchSkills } from "@/lib/api";
import { EmptyState } from "@/components/EmptyState";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { MicroBars } from "@/components/ui/spark";
import { formatDay } from "@/lib/utils";

type Ctx = { range: string };

/* Gated metric marker: outlined warn chip with a dashed border (§3.5). */
function GatedChip() {
  return (
    <span
      className="microlabel rounded-[4px] border border-dashed px-1.5 py-[1px] text-[9px]"
      style={{
        borderColor: "color-mix(in srgb, var(--status-warn) 55%, transparent)",
        color: "var(--status-warn)",
      }}
    >
      gated
    </span>
  );
}

export function Skills() {
  const { range } = useOutletContext<Ctx>();
  const q = useQuery({
    queryKey: ["skills", range],
    queryFn: () => fetchSkills(range),
  });

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (!q.data) {
    return (
      <EmptyState
        title="Could not load skills"
        body="The skills endpoint failed — refresh or check agentlog serve."
      />
    );
  }

  const data = q.data;
  const items = data.items;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h1 className="text-[18px] font-semibold tracking-tight">Skills</h1>
        <span className="text-[12px] text-faint-foreground">
          exposure counts are descriptive — not effectiveness scores
        </span>
      </div>

      <div className="grid grid-cols-4 gap-3">
        <Card>
          <CardTitle>Activations</CardTitle>
          <div className="mt-1.5 tabular text-[24px] font-semibold leading-[1.2]">
            {data.activations.toLocaleString()}
          </div>
          <div className="mt-1 text-[12px] text-faint-foreground">
            skill exposures in range
          </div>
        </Card>
        <Card>
          <CardTitle>Distinct skills</CardTitle>
          <div className="mt-1.5 tabular text-[24px] font-semibold leading-[1.2]">
            {data.distinct_fired}
          </div>
          <div className="mt-1 text-[12px] text-faint-foreground">
            fired at least once
          </div>
        </Card>
        <Card title="With/without correction contrasts require ux_observations and a passing precision gate.">
          <div className="flex items-baseline justify-between">
            <CardTitle>Effectiveness</CardTitle>
            <GatedChip />
          </div>
          <div className="mt-1.5 text-[24px] font-semibold leading-[1.2] text-faint-foreground">
            —
          </div>
          <div className="mt-1 text-[12px] text-faint-foreground">
            pending ux_observations
          </div>
        </Card>
        <Card title="Dead-skill overhead needs the full skill inventory, which is not ingested yet.">
          <div className="flex items-baseline justify-between">
            <CardTitle>Never fired</CardTitle>
            <GatedChip />
          </div>
          <div className="mt-1.5 text-[24px] font-semibold leading-[1.2] text-faint-foreground">
            —
          </div>
          <div className="mt-1 text-[12px] text-faint-foreground">
            needs installed-skill inventory
          </div>
        </Card>
      </div>

      <PanelCard title="Activation table" aside={`${items.length} skills`}>
        {items.length === 0 ? (
          <div className="p-4">
            <EmptyState
              title="No skill exposures in range"
              body="Rows appear when ingest records skill_exposures for sessions in this window. Widen the time range to see historical activations."
              missing={["skill_exposures"]}
            />
          </div>
        ) : (
          <table className="w-full text-left text-[12px]">
            <thead>
              <tr className="microlabel border-b border-border text-[10px] text-faint-foreground">
                <th className="px-4 py-2 font-medium">Skill</th>
                <th className="py-2 text-right font-medium">Fires</th>
                <th className="py-2 text-right font-medium">Sessions</th>
                <th className="py-2 pl-8 font-medium">Trend, 8w</th>
                <th className="py-2 font-medium">Last fired</th>
                <th className="px-4 py-2 text-right font-medium">Corr% w/ · w/o</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr
                  key={s.skill}
                  className="border-b border-border-faint last:border-0 hover:bg-muted/40"
                >
                  <td className="px-4 py-1.5 font-mono text-[12px] text-muted-foreground">
                    {s.skill}
                  </td>
                  <td className="tabular py-1.5 text-right">{s.fires}</td>
                  <td className="tabular py-1.5 text-right text-muted-foreground">
                    {s.sessions}
                  </td>
                  <td className="py-1.5 pl-8">
                    <MicroBars values={s.sparkline ?? []} width={72} height={14} />
                  </td>
                  <td className="tabular py-1.5 text-muted-foreground">
                    {formatDay(s.last_fired)}
                  </td>
                  <td
                    className="tabular px-4 py-1.5 text-right text-faint-foreground"
                    title="Correction contrasts appear only after semantic extraction clears its precision gate — never faked."
                  >
                    — · —
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </PanelCard>

      <p className="max-w-3xl text-[11px] leading-relaxed text-faint-foreground">
        {data.note}
      </p>
    </div>
  );
}
