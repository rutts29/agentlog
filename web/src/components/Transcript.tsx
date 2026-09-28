import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  fetchToolTrace,
  type TimelineItem,
  type TimelineMessage,
  type ToolEvent,
  type ToolTraceItem,
} from "@/lib/api";
import { ToolTraceRows } from "@/components/command-grid/PhaseToolTrace";
import { classifySpeaker, type SpeakerSpec } from "@/lib/speaker";
import { formatClock, cn } from "@/lib/utils";
import { useViewShortcuts } from "@/lib/keyboard";

type RenderItem =
  | { type: "turn"; msg: TimelineMessage; spec: SpeakerSpec }
  | { type: "toolgroup"; id: string; tools: ToolEvent[] };

function buildRenderItems(
  timeline: TimelineItem[],
  isChildSession: boolean,
): RenderItem[] {
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
      out.push({
        type: "turn",
        msg: item,
        spec: classifySpeaker(item, { isChildSession }),
      });
    }
  }
  return out;
}

export function Transcript({
  timeline,
  focusId,
  isChildSession = false,
  assistantIdentityFallback = null,
  sessionId,
  range,
}: {
  timeline: TimelineItem[];
  focusId: string | null;
  isChildSession?: boolean;
  assistantIdentityFallback?: string | null;
  sessionId: string;
  range: string;
}) {
  const items = useMemo(
    () => buildRenderItems(timeline, isChildSession),
    [timeline, isChildSession],
  );
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
    <div ref={containerRef} className="holotrace">
      {items.map((item) =>
        item.type === "toolgroup" ? (
          <ToolGroup key={`g-${item.id}`} tools={item.tools} sessionId={sessionId} range={range} orphan />
        ) : (
          <Turn
            key={item.msg.id}
            msg={item.msg}
            spec={item.spec}
            focused={focusId === item.msg.id}
            assistantIdentityFallback={assistantIdentityFallback}
            sessionId={sessionId}
            range={range}
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
  assistantIdentityFallback,
  sessionId,
  range,
}: {
  msg: TimelineMessage;
  spec: SpeakerSpec;
  focused: boolean;
  assistantIdentityFallback: string | null;
  sessionId: string;
  range: string;
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
  const recordedModel = msg.model && !/^\(?unknown\)?$/i.test(msg.model) ? msg.model : null;
  const assistantIdentity = recordedModel ?? assistantIdentityFallback;
  const assistantIdentityLine = assistantIdentity
    ? `${assistantIdentity}${msg.effort ? ` /${msg.effort}` : ""}`
    : msg.effort
      ? `effort ${msg.effort}`
      : null;

  return (
    <article
      ref={ref}
      id={`msg-${msg.id}`}
      className={cn(
        "holotrace-turn",
        `holotrace-turn--${spec.kind}`,
        human && "holotrace-turn--command",
        focused && "holotrace-turn--focused",
      )}
    >
      <div className="holotrace-turn__gutter">
        <span className="holotrace-turn__time">{formatClock(msg.timestamp)}</span>
        <span className="holotrace-turn__node" style={{ background: spec.color }} aria-hidden />
        <span className="holotrace-turn__speaker" style={{ color: spec.color }}>{spec.label}</span>
        {spec.detail ? (
          <span className="holotrace-turn__detail">{spec.detail}</span>
        ) : null}
      </div>

      <div className="holotrace-turn__body" style={{ borderColor: spec.color }}>
        {spec.kind === "assistant" && assistantIdentityLine ? (
          <div className="holotrace-turn__model">
            {assistantIdentityLine}
          </div>
        ) : null}

        {body ? (
          <pre
            className={cn(
              "holotrace-turn__copy",
              human
                ? "holotrace-turn__copy--human"
                : spec.kind === "assistant"
                  ? "holotrace-turn__copy--agent"
                  : "holotrace-turn__copy--system",
            )}
          >
            {preview}
            {collapsed ? "…" : ""}
          </pre>
        ) : msg.tool_events.length === 0 ? (
          <div className="holotrace-turn__empty">(no text)</div>
        ) : null}

        <div className="holotrace-turn__actions">
          {spec.collapsed ? (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="holotrace-action"
            >
              {expanded ? "collapse" : `expand · ${body.length.toLocaleString()} chars`}
            </button>
          ) : null}
          {spec.extractedQuery !== null && spec.extractedQuery !== undefined ? (
            <button
              type="button"
              onClick={() => setShowEnvelope((v) => !v)}
              className="holotrace-action"
            >
              {showEnvelope
                ? "hide harness envelope"
                : `harness envelope · ${(msg.text ?? "").length.toLocaleString()} chars`}
            </button>
          ) : null}
        </div>

        {showEnvelope ? (
          <pre className="holotrace-envelope">
            {msg.text}
          </pre>
        ) : null}

        {msg.tool_events.length > 0 ? (
          <ToolGroup tools={msg.tool_events} sessionId={sessionId} range={range} />
        ) : null}
      </div>
    </article>
  );
}

function normalizedTraceItems(tools: ToolEvent[]): ToolTraceItem[] {
  const items: ToolTraceItem[] = [];
  for (let index = 0; index < tools.length; index += 1) {
    const tool = tools[index];
    const next = tools[index + 1];
    const paired =
      tool.action === "call" &&
      next?.tool_name === tool.tool_name &&
      (next.action === "result" || next.action === "end");
    const terminal = paired ? next : tool;
    const recordedInput = tool.action && !/^(call|result|end)$/i.test(tool.action) ? tool.action : null;
    items.push({
      id: tool.id,
      call_id: null,
      call_seq: tool.seq ?? index,
      result_seq: paired ? next.seq : null,
      tool_name: tool.tool_name,
      operation_kind: tool.operation_kind || "unknown",
      timestamp: null,
      result_timestamp: null,
      message_id: tool.message_id,
      recorded_action: tool.action || "recorded",
      model: null,
      effort: null,
      input: recordedInput,
      input_truncated: false,
      output: null,
      output_truncated: false,
      success: terminal.success,
      duration_ms: terminal.duration_ms,
    });
    if (paired) index += 1;
  }
  return items;
}

function ToolGroup({
  tools,
  sessionId,
  range,
  orphan,
}: {
  tools: ToolEvent[];
  sessionId: string;
  range: string;
  orphan?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const failed = tools.filter((t) => t.success === 0).length;
  const totalMs = tools.reduce((acc, t) => acc + (t.duration_ms ?? 0), 0);
  const sequences = tools.map((tool) => tool.seq).filter((seq): seq is number => seq != null);
  const startSeq = sequences.length > 0 ? Math.min(...sequences) : null;
  const endSeq = sequences.length > 0 ? Math.max(...sequences) : null;
  const fallbackItems = useMemo(() => normalizedTraceItems(tools), [tools]);
  const trace = useQuery({
    queryKey: ["transcript-tool-trace", sessionId, startSeq, endSeq],
    queryFn: ({ signal }) => fetchToolTrace(sessionId, startSeq!, endSeq!, signal),
    enabled: open && startSeq != null && endSeq != null,
    staleTime: Infinity,
  });
  const traceItems = trace.data?.items.length ? trace.data.items : fallbackItems;
  const callCount = trace.data?.items.length || fallbackItems.length;

  const summary = (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      className="tool-packet-summary"
      aria-expanded={open}
    >
      <span
        aria-hidden
        className={cn("tool-packet-summary__arrow", open && "tool-packet-summary__arrow--open")}
      >
        ▶
      </span>
      <span>tool packet · {callCount} {callCount === 1 ? "call" : "calls"}</span>
      {failed > 0 ? (
        <span className="tool-packet-summary__failed">{failed} failed</span>
      ) : null}
      {totalMs > 0 ? (
        <span className="tool-packet-summary__duration">
          {totalMs >= 1000 ? `${(totalMs / 1000).toFixed(1)}s` : `${totalMs}ms`}
        </span>
      ) : null}
    </button>
  );

  const list = open ? (
    trace.isLoading ? (
      <div className="tool-trace-inline-status">Reading local tool inputs and outputs…</div>
    ) : (
      <ToolTraceRows items={traceItems} sessionId={sessionId} range={range} />
    )
  ) : null;

  if (orphan) {
    return (
      <div className="holotrace-turn holotrace-turn--tool holotrace-turn--orphan-tool">
        <div className="holotrace-turn__gutter">
          <span className="holotrace-turn__time">event</span>
          <span className="holotrace-turn__node" style={{ background: "var(--speaker-tool)" }} aria-hidden />
          <span className="holotrace-turn__speaker" style={{ color: "var(--speaker-tool)" }}>tool</span>
        </div>
        <div className="holotrace-turn__body" style={{ borderColor: "var(--speaker-tool)" }}>
          {summary}
          {list}
        </div>
      </div>
    );
  }
  return (
    <div className="holotrace-toolgroup">
      {summary}
      {list}
    </div>
  );
}
