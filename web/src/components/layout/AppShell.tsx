import { NavLink, Outlet, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { fetchMeta, type RangeKey } from "@/lib/api";
import { cn } from "@/lib/utils";

const NAV: Array<{ to: string; label: string; end?: boolean }> = [
  { to: "/", label: "Overview", end: true },
  { to: "/sessions", label: "Sessions" },
  { to: "/models", label: "Models" },
  { to: "/skills", label: "Skills" },
  { to: "/insights", label: "Insights" },
];

const RANGES: RangeKey[] = ["7d", "30d", "90d", "all"];

export function AppShell() {
  const [params, setParams] = useSearchParams();
  const range = (params.get("range") as RangeKey) || "30d";
  const meta = useQuery({ queryKey: ["meta"], queryFn: fetchMeta });

  function setRange(next: RangeKey) {
    const copy = new URLSearchParams(params);
    copy.set("range", next);
    setParams(copy, { replace: true });
  }

  const lastAt = meta.data?.freshness.last_at;
  const synced = lastAt
    ? formatDistanceToNow(new Date(lastAt), { addSuffix: true })
    : "—";

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-[200px] shrink-0 flex-col border-r border-border bg-card">
        <div className="px-4 py-4 text-[15px] font-semibold tracking-tight">
          agentlog
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 px-2">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={{ pathname: item.to, search: params.toString() }}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  "rounded-control px-3 py-2 text-[13px] text-muted-foreground hover:text-foreground",
                  isActive && "bg-muted text-foreground",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-border px-4 py-3 text-[12px] text-muted-foreground">
          <div className="tabular text-foreground">
            {meta.data?.freshness.sessions.toLocaleString() ?? "—"}
          </div>
          <div>sessions synced</div>
          <div className="mt-1 text-faint-foreground">{synced}</div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 items-center justify-between border-b border-border px-5">
          <div className="text-[13px] text-muted-foreground">
            Descriptive usage profile · observational history
          </div>
          <div className="flex items-center gap-1 rounded-control border border-border p-0.5">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setRange(r)}
                className={cn(
                  "rounded-[5px] px-2.5 py-1 text-[12px] text-muted-foreground",
                  range === r && "bg-muted text-foreground",
                )}
              >
                {r === "all" ? "All" : r}
              </button>
            ))}
          </div>
        </header>
        <main className="flex-1 overflow-auto p-5">
          <Outlet context={{ range }} />
        </main>
      </div>
    </div>
  );
}
