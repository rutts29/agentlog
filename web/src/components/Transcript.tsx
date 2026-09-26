import { useEffect, useMemo, useRef, useState } from "react";
import type { TimelineItem, TimelineMessage, ToolEvent } from "@/lib/api";
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
}: {
  timeline: TimelineItem[];
  focusId: string | null;
  isChildSession?: boolean;
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
        {spec.kind === "assistant" && (msg.model || msg.effort) ? (
          <div className="holotrace-turn__model">
            {msg.model}
            {msg.effort ? ` /${msg.effort}` : ""}
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
        ) : (
          <div className="holotrace-turn__empty">(no text)</div>
        )}

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
          <ToolGroup tools={msg.tool_events} />
        ) : null}
      </div>
    </article>
  );
}

function ToolGroup({ tools, orphan }: { tools: ToolEvent[]; orphan?: boolean }) {
  const [open, setOpen] = useState(tools.length <= 3);
  const failed = tools.filter((t) => t.success === 0).length;
  const totalMs = tools.reduce((acc, t) => acc + (t.duration_ms ?? 0), 0);

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
      <span>tool packet · {tools.length} {tools.length === 1 ? "call" : "calls"}</span>
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
    <div className="tool-packet-list-expanded">
      {tools.map((t) => (
        <div
          key={t.id}
          className="tool-packet"
        >
          <span
            aria-hidden
            className="tool-packet__status"
            style={{
              background:
                t.success === 0
                  ? "var(--status-error)"
                  : t.success === 1
                    ? "var(--status-ok)"
                    : "var(--faint-foreground)",
            }}
          />
          <span className="tool-packet__name">{t.tool_name}</span>
          {t.action ? (
            <span className="tool-packet__action">{t.action}</span>
          ) : null}
          {t.duration_ms != null ? (
            <span className="tool-packet__duration">
              {t.duration_ms}ms
            </span>
          ) : null}
        </div>
      ))}
    </div>
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
