import { useEffect, useState } from "react";
import {
  NavLink,
  Outlet,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { fetchMeta, type RangeKey } from "@/lib/api";
import { CommandPalette } from "@/components/CommandPalette";
import { cn } from "@/lib/utils";
import { isEditable } from "@/lib/keyboard";

const NAV_SECTIONS: Array<{
  title: string;
  items: Array<{ to: string; label: string; end?: boolean }>;
}> = [
  {
    title: "Activity",
    items: [
      { to: "/", label: "Overview", end: true },
      { to: "/sessions", label: "Sessions" },
      { to: "/search", label: "Search" },
    ],
  },
  {
    title: "Analysis",
    items: [
      { to: "/models", label: "Models" },
      { to: "/orchestration", label: "Orchestration" },
      { to: "/auto-review", label: "Auto-review" },
      { to: "/skills", label: "Skills" },
      { to: "/insights", label: "Insights" },
      { to: "/proposals", label: "Proposals" },
      { to: "/adjudicate", label: "Adjudicate" },
    ],
  },
];

const RANGES: RangeKey[] = ["7d", "30d", "90d", "all"];

export function AppShell() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const range = (params.get("range") as RangeKey) || "all";
  const [paletteOpen, setPaletteOpen] = useState(false);
  const meta = useQuery({ queryKey: ["meta"], queryFn: fetchMeta });

  function setRange(next: RangeKey) {
    const copy = new URLSearchParams(params);
    copy.set("range", next);
    copy.delete("cursor");
    setParams(copy, { replace: true });
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((v) => !v);
        return;
      }
      if (paletteOpen || isEditable(e.target) || e.metaKey || e.ctrlKey || e.altKey)
        return;
      /* View-local shortcuts (Adjudicate `]`, etc.) win via capture-phase
         handlers; also skip here so a missed stopImmediatePropagation cannot
         silently mutate the global range. */
      if (e.key === "[" || e.key === "]") {
        if (location.pathname.startsWith("/adjudicate")) return;
        e.preventDefault();
        const idx = RANGES.indexOf(range);
        const next =
          e.key === "["
            ? RANGES[(idx - 1 + RANGES.length) % RANGES.length]
            : RANGES[(idx + 1) % RANGES.length];
        setRange(next);
        return;
      }
      if (e.key === "Escape") {
        const m = location.pathname.match(/^\/sessions\/.+/);
        if (m) {
          const p = new URLSearchParams(params);
          p.delete("msg");
          navigate({ pathname: "/sessions", search: p.toString() });
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, paletteOpen, location.pathname, params]);

  const lastAt = meta.data?.freshness.last_at;
  const synced = lastAt
    ? formatDistanceToNow(new Date(lastAt), { addSuffix: true })
    : "—";

  return (
    <div className="flex h-screen overflow-hidden">
      <aside className="flex w-[200px] shrink-0 flex-col border-r border-border bg-card">
        <div className="flex items-center gap-2 px-4 pb-3 pt-4">
          <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden>
            <rect x="0.5" y="0.5" width="13" height="13" rx="2" fill="none" stroke="var(--muted-foreground)" />
            <rect x="3" y="7.5" width="2" height="3.5" fill="var(--harness-codex)" />
            <rect x="6" y="5" width="2" height="6" fill="var(--harness-claude)" />
            <rect x="9" y="3" width="2" height="8" fill="var(--harness-cursor)" />
          </svg>
          <span className="text-[14px] font-semibold tracking-tight">agentlog</span>
        </div>
        <nav className="flex flex-1 flex-col gap-4 overflow-y-auto px-2 pt-1">
          {NAV_SECTIONS.map((section) => (
            <div key={section.title}>
              <div className="microlabel px-3 pb-1 text-[10px] text-faint-foreground">
                {section.title}
              </div>
              <div className="flex flex-col gap-px">
                {section.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={{ pathname: item.to, search: params.toString() }}
                    end={item.end}
                    className={({ isActive }) =>
                      cn(
                        "relative rounded-control px-3 py-1.5 text-[13px] text-muted-foreground hover:text-foreground",
                        isActive &&
                          "bg-muted text-foreground before:absolute before:left-0 before:top-[7px] before:h-[12px] before:w-[2px] before:rounded-full before:bg-primary",
                      )
                    }
                  >
                    {item.label}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="border-t border-border px-4 py-3">
          <div className="flex items-baseline gap-1.5">
            <span className="tabular text-[15px] font-semibold text-foreground">
              {meta.data?.freshness.sessions.toLocaleString() ?? "—"}
            </span>
            <span className="text-[11px] text-muted-foreground">sessions</span>
          </div>
          <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-faint-foreground">
            <span
              aria-hidden
              className="inline-block h-[5px] w-[5px] rounded-full"
              style={{ background: lastAt ? "var(--status-ok)" : "var(--faint-foreground)" }}
            />
            synced {synced}
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-3 border-b border-border px-5">
          <div className="text-[12px] text-faint-foreground">
            Descriptive usage ledger · observational history
          </div>
          <div className="ml-auto flex items-center gap-3">
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="flex items-center gap-2 rounded-control border border-border px-2.5 py-1 text-[12px] text-muted-foreground hover:text-foreground"
            >
              go to
              <kbd>⌘K</kbd>
            </button>
            <div className="flex items-center rounded-control border border-border p-0.5">
              {RANGES.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRange(r)}
                  className={cn(
                    "tabular rounded-[5px] px-2.5 py-1 text-[12px] text-muted-foreground hover:text-foreground",
                    range === r && "bg-muted text-foreground",
                  )}
                >
                  {r === "all" ? "All" : r}
                </button>
              ))}
            </div>
          </div>
        </header>
        <main className="flex-1 overflow-auto p-5">
          <Outlet context={{ range }} />
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        range={range}
      />
    </div>
  );
}
