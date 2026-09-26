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
import {
  Activity,
  Bot,
  BrainCircuit,
  FileCheck2,
  Gauge,
  Lightbulb,
  Network,
  Radio,
  ScanLine,
  ScrollText,
  Search,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import { fetchMeta, type RangeKey } from "@/lib/api";
import { CommandPalette } from "@/components/CommandPalette";
import { cn } from "@/lib/utils";
import { isEditable } from "@/lib/keyboard";
import { preloadView } from "@/lib/routePreload";

const NAV_SECTIONS: Array<{
  title: string;
  items: Array<{ to: string; label: string; canonical: string; icon: LucideIcon; end?: boolean }>;
}> = [
  {
    title: "Command",
    items: [
      { to: "/", label: "Command deck", canonical: "Overview", icon: Gauge, end: true },
      { to: "/floor", label: "Command floor", canonical: "Live agents", icon: ScanLine },
      { to: "/sessions", label: "Active missions", canonical: "Sessions", icon: Activity },
      { to: "/search", label: "Trace search", canonical: "Search", icon: Search },
    ],
  },
  {
    title: "Operations",
    items: [
      { to: "/orchestration", label: "Sector map", canonical: "Orchestration", icon: Network },
      { to: "/models", label: "Model systems", canonical: "Models", icon: BrainCircuit },
      { to: "/auto-review", label: "Signal review", canonical: "Auto-review", icon: Radio },
      { to: "/skills", label: "Crew skills", canonical: "Skills", icon: Bot },
      { to: "/insights", label: "Intel reports", canonical: "Insights", icon: Lightbulb },
      { to: "/proposals", label: "Council proposals", canonical: "Proposals", icon: ScrollText },
      { to: "/adjudicate", label: "Council review", canonical: "Manual review", icon: FileCheck2 },
    ],
  },
];

const RANGES: RangeKey[] = ["24h", "7d", "30d", "all"];

export function AppShell() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const requestedRange = params.get("range") as RangeKey | null;
  const range = requestedRange && RANGES.includes(requestedRange) ? requestedRange : "24h";
  const [paletteOpen, setPaletteOpen] = useState(false);
  const meta = useQuery({ queryKey: ["meta"], queryFn: fetchMeta });
  const globalSearch = new URLSearchParams({ range }).toString();

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
      /* Active manual-review shortcuts win via capture-phase handlers; skip
         here if a review handler misses the event boundary. */
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
    <div className="command-shell">
      <aside className="command-sidebar">
        <div className="command-brand">
          <div className="command-brand__mark" aria-hidden>
            <span />
            <span />
            <span />
          </div>
          <div className="min-w-0">
            <div className="command-brand__name">AGENTLOG <span>// GRID</span></div>
            <div className="command-brand__tagline">local-first flight recorder</div>
          </div>
        </div>
        <nav
          aria-label="Primary"
          className="command-nav"
        >
          {NAV_SECTIONS.map((section) => (
            <div key={section.title} className="command-nav__section">
              <div className="command-nav__title">{section.title}</div>
              <div className="command-nav__items">
                {section.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={{ pathname: item.to, search: globalSearch }}
                    end={item.end}
                    onMouseEnter={() => preloadView(item.to)}
                    onFocus={() => preloadView(item.to)}
                    className={({ isActive }) =>
                      cn("command-nav__link", isActive && "command-nav__link--active")
                    }
                    title={`${item.label} · ${item.canonical}`}
                  >
                    <item.icon size={15} strokeWidth={1.7} aria-hidden />
                    <span>{item.label}</span>
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="command-node-card">
          <div className="command-node-card__head">
            <ShieldCheck size={14} aria-hidden />
            <span>Local node</span>
            <span className="command-node-card__dot" aria-hidden />
          </div>
          <div className="command-node-card__metric">
            <span className="tabular text-[17px] font-semibold text-foreground">
              {meta.data?.freshness.sessions.toLocaleString() ?? "—"}
            </span>
            <span>sessions indexed</span>
          </div>
          <div className="command-node-card__sync">synced {synced}</div>
          <div className="command-node-card__motto">your code stays here</div>
        </div>
      </aside>

      <div className="command-workspace">
        <header className="command-topbar">
          <div className="command-topbar__status">
            <span className="command-topbar__eyebrow">systems nominal</span>
            <span className="command-topbar__online"><span aria-hidden /> Online</span>
          </div>
          <div className="command-topbar__divider" aria-hidden />
          <div className="command-topbar__mission">
            <span>record</span><span>understand</span><span>trust</span><span>improve</span>
          </div>
          <div className="command-topbar__controls">
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="command-button"
            >
              command
              <kbd>⌘K</kbd>
            </button>
            <div className="command-range" aria-label="Time range">
              {RANGES.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRange(r)}
                  className={cn(
                    "command-range__button",
                    range === r && "command-range__button--active",
                  )}
                >
                  {r === "all" ? "All" : r}
                </button>
              ))}
            </div>
          </div>
        </header>
        <main className="command-main">
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
