import { useEffect, useMemo, useRef, useState } from "react";
import type { TimelineItem, TimelineMessage, ToolEvent } from "@/lib/api";
import { classifySpeaker, type SpeakerSpec } from "@/lib/speaker";
import { formatClock, cn } from "@/lib/utils";
import { useViewShortcuts } from "@/lib/keyboard";

type RenderItem =
  | { type: "turn"; msg: TimelineMessage; spec: SpeakerSpec }
  | { type: "toolgroup"; id: string; tools: ToolEvent[] };

function buildRenderItems(timeline: TimelineItem[]): RenderItem[] {
  const out: RenderItem[] = [];
  for (const item of timeline) {
    if (item.kind === "tool") {
      const last = out[out.length - 1];
      const ev: ToolEvent = {
        id: item.id,
        message_id: null,
        seq: item.seq,
        tool_name: item.tool_name,
        action: item.action,
        success: item.success,
        duration_ms: item.duration_ms,
      };
      if (last && last.type === "toolgroup") last.tools.push(ev);
      else out.push({ type: "toolgroup", id: item.id, tools: [ev] });
    } else {
      out.push({ type: "turn", msg: item, spec: classifySpeaker(item) });
    }
  }
  return out;
}

export function Transcript({
  timeline,
  focusId,
}: {
  timeline: TimelineItem[];
  focusId: string | null;
}) {
  const items = useMemo(() => buildRenderItems(timeline), [timeline]);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const humanIds = useMemo(
    () =>
      items
        .filter(
          (i): i is Extract<RenderItem, { type: "turn" }> =>
            i.type === "turn" && i.spec.kind === "human",
        )
        .map((i) => i.msg.id),
    [items],
  );
  const humanCursor = useRef(-1);

  useViewShortcuts((e) => {
    if (e.key !== "n" || humanIds.length === 0) return false;
    humanCursor.current = (humanCursor.current + 1) % humanIds.length;
    document
      .getElementById(`msg-${humanIds[humanCursor.current]}`)
      ?.scrollIntoView({ block: "center" });
    return true;
  });

  return (
    <div ref={containerRef} className="divide-y divide-border-faint">
      {items.map((item) =>
        item.type === "toolgroup" ? (
          <ToolGroup key={`g-${item.id}`} tools={item.tools} orphan />
        ) : (
          <Turn
            key={item.msg.id}
            msg={item.msg}
            spec={item.spec}
            focused={focusId === item.msg.id}
          />
        ),
      )}
    </div>
  );
}

function Turn({
  msg,
  spec,
  focused,
}: {
  msg: TimelineMessage;
  spec: SpeakerSpec;
  focused: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [showEnvelope, setShowEnvelope] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ block: "center" });
  }, [focused]);

  const human = spec.kind === "human";
  const body = spec.extractedQuery ?? msg.text ?? "";
  const collapsed = spec.collapsed && !expanded;
  const preview = collapsed ? body.slice(0, 220) : body;

  return (
    <div
      ref={ref}
      id={`msg-${msg.id}`}
      className={cn(
        "grid grid-cols-[92px_minmax(0,1fr)] gap-x-3 px-4 py-2.5",
        human && "bg-[var(--speaker-human-dim)]",
      )}
      style={
        focused
          ? {
              /* The active turn is this view's one glow center (R1). */
              background: `color-mix(in srgb, ${spec.color} 7%, transparent)`,
              boxShadow: `inset 2px 0 0 ${spec.color}, 0 0 20px -6px color-mix(in srgb, ${spec.color} 35%, transparent)`,
            }
          : undefined
      }
    >
      {/* Gutter: who is talking, at a glance. */}
      <div className="pt-[2px] text-right">
        <div
          className={cn("microlabel text-[10px] leading-4", human && "font-semibold")}
          style={{ color: spec.color }}
        >
          {spec.label}
        </div>
        {spec.detail ? (
          <div className="truncate font-mono text-[9px] leading-4 text-faint-foreground">
            {spec.detail}
          </div>
        ) : null}
        <div className="tabular text-[10px] leading-4 text-faint-foreground">
          {formatClock(msg.timestamp)}
        </div>
      </div>

      {/* Body with the speaker rail. */}
      <div
        className="min-w-0 border-l-2 pl-3"
        style={{
          borderColor: human
            ? spec.color
            : `color-mix(in srgb, ${spec.color} 55%, transparent)`,
        }}
      >
        {spec.kind === "assistant" && (msg.model || msg.effort) ? (
          <div className="mb-1 font-mono text-[10px] text-faint-foreground">
            {msg.model}
            {msg.effort ? ` /${msg.effort}` : ""}
          </div>
        ) : null}

        {body ? (
          <pre
            className={cn(
              "whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.45]",
              human
                ? "font-medium text-foreground"
                : spec.kind === "assistant"
                  ? "text-foreground/90"
                  : "text-muted-foreground",
            )}
          >
            {preview}
            {collapsed ? "…" : ""}
          </pre>
        ) : (
          <div className="text-[11px] italic text-faint-foreground">(no text)</div>
        )}

        <div className="mt-1 flex flex-wrap items-center gap-3">
          {spec.collapsed ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="text-[11px] text-faint-foreground hover:text-muted-foreground"
            >
              {expanded ? "collapse" : `expand · ${body.length.toLocaleString()} chars`}
            </button>
          ) : null}
          {spec.extractedQuery !== null && spec.extractedQuery !== undefined ? (
            <button
              type="button"
              onClick={() => setShowEnvelope((v) => !v)}
              className="text-[11px] text-faint-foreground hover:text-muted-foreground"
            >
              {showEnvelope
                ? "hide harness envelope"
                : `harness envelope · ${(msg.text ?? "").length.toLocaleString()} chars`}
            </button>
          ) : null}
        </div>

        {showEnvelope ? (
          <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-[4px] border border-border-faint bg-background/60 p-2 font-mono text-[11px] leading-[1.4] text-faint-foreground">
            {msg.text}
          </pre>
        ) : null}

        {msg.tool_events.length > 0 ? (
          <ToolGroup tools={msg.tool_events} />
        ) : null}
      </div>
    </div>
  );
}

function ToolGroup({ tools, orphan }: { tools: ToolEvent[]; orphan?: boolean }) {
  const [open, setOpen] = useState(false);
  const failed = tools.filter((t) => t.success === 0).length;
  const totalMs = tools.reduce((acc, t) => acc + (t.duration_ms ?? 0), 0);

  const summary = (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      className="inline-flex items-center gap-2 font-mono text-[11px] text-muted-foreground hover:text-foreground"
    >
      <span
        aria-hidden
        className={cn(
          "inline-block text-[9px] text-faint-foreground transition-transform",
          open && "rotate-90",
        )}
      >
        ▶
      </span>
      {tools.length} tool {tools.length === 1 ? "call" : "calls"}
      {failed > 0 ? (
        <span style={{ color: "var(--status-error)" }}>{failed} failed</span>
      ) : null}
      {totalMs > 0 ? (
        <span className="tabular text-faint-foreground">
          {totalMs >= 1000 ? `${(totalMs / 1000).toFixed(1)}s` : `${totalMs}ms`}
        </span>
      ) : null}
    </button>
  );

  const list = open ? (
    <div className="mt-1.5 space-y-0.5 border-l border-border-faint pl-3">
      {tools.map((t) => (
        <div
          key={t.id}
          className="flex items-baseline gap-2 font-mono text-[11px] text-muted-foreground"
        >
          <span
            aria-hidden
            className="inline-block h-[5px] w-[5px] shrink-0 translate-y-[-1px] rounded-full"
            style={{
              background:
                t.success === 0
                  ? "var(--status-error)"
                  : t.success === 1
                    ? "var(--status-ok)"
                    : "var(--faint-foreground)",
            }}
          />
          <span className="text-foreground/80">{t.tool_name}</span>
          {t.action ? (
            <span className="min-w-0 truncate text-faint-foreground">{t.action}</span>
          ) : null}
          {t.duration_ms != null ? (
            <span className="tabular ml-auto shrink-0 text-faint-foreground">
              {t.duration_ms}ms
            </span>
          ) : null}
        </div>
      ))}
    </div>
  ) : null;

  if (orphan) {
    return (
      <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-x-3 px-4 py-2">
        <div className="pt-[1px] text-right">
          <div className="microlabel text-[10px] leading-4" style={{ color: "var(--speaker-tool)" }}>
            tool
          </div>
        </div>
        <div
          className="min-w-0 border-l-2 pl-3"
          style={{ borderColor: "color-mix(in srgb, var(--speaker-tool) 55%, transparent)" }}
        >
          {summary}
          {list}
        </div>
      </div>
    );
  }
  return (
    <div className="mt-1.5">
      {summary}
      {list}
    </div>
  );
}
