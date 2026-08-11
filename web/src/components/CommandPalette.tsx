import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  fetchFacets,
  fetchRecent,
  fetchSkills,
  logicalHarness,
  runtimeHarness,
} from "@/lib/api";
import { HarnessTag, RuntimeHarnessLabel } from "@/components/ui/badges";
import { formatDayTime } from "@/lib/utils";
import { cn } from "@/lib/utils";

type Item = {
  id: string;
  group: string;
  label: string;
  meta?: string;
  harness?: string;
  runtimeHarness?: string;
  run: () => void;
};

const VIEWS: Array<{ label: string; path: string }> = [
  { label: "Overview", path: "/" },
  { label: "Sessions", path: "/sessions" },
  { label: "Search", path: "/search" },
  { label: "Models", path: "/models" },
  { label: "Orchestration", path: "/orchestration" },
  { label: "Auto-review", path: "/auto-review" },
  { label: "Skills", path: "/skills" },
  { label: "Insights", path: "/insights" },
  { label: "Proposals", path: "/proposals" },
  { label: "Manual review", path: "/adjudicate" },
];

export function CommandPalette({
  open,
  onClose,
  range,
}: {
  open: boolean;
  onClose: () => void;
  range: string;
}) {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const recent = useQuery({
    queryKey: ["palette-recent", range],
    queryFn: () => fetchRecent(range, 12),
    enabled: open,
  });
  const facets = useQuery({
    queryKey: ["facets", range],
    queryFn: () => fetchFacets(range),
    enabled: open,
  });
  const skills = useQuery({
    queryKey: ["skills", range],
    queryFn: () => fetchSkills(range),
    enabled: open,
  });

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const search = params.toString();
  const items = useMemo<Item[]>(() => {
    const go = (path: string, extra?: Record<string, string>) => () => {
      const p = new URLSearchParams(search);
      p.delete("cursor");
      p.delete("msg");
      p.delete("root");
      if (extra) for (const [k, v] of Object.entries(extra)) p.set(k, v);
      navigate({ pathname: path, search: p.toString() });
      onClose();
    };
    const out: Item[] = VIEWS.map((v) => ({
      id: `view:${v.path}`,
      group: "Views",
      label: v.label,
      run: go(v.path),
    }));
    for (const s of recent.data?.items ?? []) {
      out.push({
        id: `session:${s.id}`,
        group: "Recent sessions",
        label: s.id,
        meta: `${s.project} · ${formatDayTime(s.started_at)}`,
        harness: logicalHarness(s),
        runtimeHarness: runtimeHarness(s),
        run: go(`/sessions/${encodeURIComponent(s.id)}`),
      });
    }
    for (const p of facets.data?.project ?? []) {
      out.push({
        id: `project:${p.value}`,
        group: "Projects",
        label: p.value,
        meta: `${p.count} sessions`,
        run: go("/sessions", { project: p.value }),
      });
    }
    for (const sk of skills.data?.items ?? []) {
      out.push({
        id: `skill:${sk.skill}`,
        group: "Skills",
        label: sk.skill,
        meta: `${sk.fires} fires`,
        run: go("/skills"),
      });
    }
    return out;
  }, [recent.data, facets.data, skills.data, navigate, onClose, search]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) {
      // Resting state: views + recent sessions only.
      return items.filter(
        (i) => i.group === "Views" || i.group === "Recent sessions",
      );
    }
    return items
      .filter(
        (i) =>
          i.label.toLowerCase().includes(q) ||
          (i.meta ?? "").toLowerCase().includes(q),
      )
      .slice(0, 40);
  }, [items, query]);

  useEffect(() => {
    setCursor(0);
  }, [query]);

  useEffect(() => {
    const el = listRef.current?.querySelector('[data-active="true"]');
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  if (!open) return null;

  const groups: Array<[string, Item[]]> = [];
  for (const item of filtered) {
    const last = groups[groups.length - 1];
    if (last && last[0] === item.group) last[1].push(item);
    else groups.push([item.group, [item]]);
  }
  let flatIdx = -1;

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60"
      onMouseDown={onClose}
      role="dialog"
      aria-modal
    >
      <div
        className="elevated-overlay mx-auto mt-[12vh] w-[600px] max-w-[92vw] overflow-hidden rounded-card border border-border"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-border px-4">
          <span className="microlabel text-faint-foreground">go to</span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((c) => Math.min(filtered.length - 1, c + 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((c) => Math.max(0, c - 1));
              } else if (e.key === "Enter") {
                e.preventDefault();
                filtered[cursor]?.run();
              } else if (e.key === "Escape") {
                e.preventDefault();
                onClose();
              }
            }}
            placeholder="session, project, skill, view…"
            className="w-full bg-transparent py-3 text-[13px] text-foreground placeholder:text-faint-foreground focus:outline-none"
          />
          <kbd>esc</kbd>
        </div>
        <div ref={listRef} className="max-h-[46vh] overflow-y-auto py-1.5">
          {filtered.length === 0 ? (
            <div className="px-4 py-6 text-[12px] text-faint-foreground">
              Nothing matches “{query}”.
            </div>
          ) : (
            groups.map(([group, groupItems]) => (
              <div key={group}>
                <div className="microlabel px-4 pb-1 pt-2 text-[10px] text-faint-foreground">
                  {group}
                </div>
                {groupItems.map((item) => {
                  flatIdx += 1;
                  const idx = flatIdx;
                  const active = idx === cursor;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      data-active={active}
                      onMouseEnter={() => setCursor(idx)}
                      onClick={() => item.run()}
                      className={cn(
                        "flex w-full items-baseline gap-2 px-4 py-1.5 text-left",
                        active && "bg-muted",
                      )}
                    >
                      {item.harness ? (
                        <HarnessTag harness={item.harness} className="shrink-0" />
                      ) : null}
                      {item.harness && item.runtimeHarness ? (
                        <RuntimeHarnessLabel
                          logicalHarness={item.harness}
                          runtimeHarness={item.runtimeHarness}
                          className="shrink-0"
                        />
                      ) : null}
                      <span
                        className={cn(
                          "min-w-0 truncate text-[13px]",
                          item.group === "Recent sessions" &&
                            "font-mono text-[12px]",
                        )}
                      >
                        {item.label}
                      </span>
                      {item.meta ? (
                        <span className="ml-auto shrink-0 text-[11px] text-faint-foreground">
                          {item.meta}
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
        <div className="flex items-center gap-3 border-t border-border px-4 py-2 text-[11px] text-faint-foreground">
          <span>
            <kbd>↑</kbd> <kbd>↓</kbd> navigate
          </span>
          <span>
            <kbd>↵</kbd> open
          </span>
          <span className="ml-auto">
            <kbd>[</kbd> <kbd>]</kbd> time range
          </span>
        </div>
      </div>
    </div>
  );
}
