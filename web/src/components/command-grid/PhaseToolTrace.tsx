import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import type { ExecutionPhaseSelection } from "@/components/command-grid/ExecutionTimeline";
import { DiagnosticLoader } from "@/components/DiagnosticLoader";
import { fetchToolTrace, type ToolTraceItem } from "@/lib/api";
import type { MissionReplay } from "@/lib/missionReplay";
import { formatDayTime, formatDuration } from "@/lib/utils";

function outcomeLabel(item: ToolTraceItem): string | null {
  if (item.success === 1) return "success";
  if (item.success === 0) return "failed";
  return null;
}

function durationLabel(item: ToolTraceItem): string | null {
  if (item.duration_ms != null) return formatDuration(item.duration_ms / 1000);
  const started = item.timestamp ? Date.parse(item.timestamp) : Number.NaN;
  const ended = item.result_timestamp ? Date.parse(item.result_timestamp) : Number.NaN;
  if (Number.isFinite(started) && Number.isFinite(ended) && ended > started) {
    return `~${formatDuration((ended - started) / 1_000)} source span`;
  }
  return null;
}

function operationLabel(value: string): string | null {
  const labels: Record<string, string> = {
    artifact_read: "Read artifact",
    artifact_write: "Write artifact",
    execute_other: "Execute command",
    read_only: "Read-only operation",
    verification: "Verification",
  };
  if (!value || value === "unknown") return null;
  return labels[value] ?? value.replaceAll("_", " ");
}

function toolLabel(value: string): string {
  const normalized = value.toLowerCase();
  if (normalized === "exec" || normalized === "exec_command") return "Shell command";
  if (normalized === "js") return "UI automation";
  if (normalized === "apply_patch") return "Apply patch";
  return value.replaceAll("_", " ");
}

function phaseTime(phase: ExecutionPhaseSelection): string {
  const start = formatDayTime(phase.startedAt);
  const end = formatDayTime(phase.endedAt);
  return start === end ? start : `${start} to ${end}`;
}

export function ToolTraceRows({
  items,
  sessionId,
  range,
  openFirst = false,
}: {
  items: ToolTraceItem[];
  sessionId: string;
  range: string;
  openFirst?: boolean;
}) {
  return (
    <div className="phase-trace__tools">
      {items.map((item, index) => {
        const search = new URLSearchParams({ range });
        if (item.message_id) search.set("msg", item.message_id);
        const outcome = outcomeLabel(item);
        const duration = durationLabel(item);
        const operation = operationLabel(item.operation_kind);
        const identityMeta = [
          operation,
          item.model ? `${item.model}${item.effort ? ` / ${item.effort}` : ""}` : null,
        ].filter((value): value is string => Boolean(value));
        return (
          <details className="phase-tool" key={`${item.id}:${item.call_seq}`} open={openFirst && index === 0}>
            <summary>
              <span className="phase-tool__number">{String(index + 1).padStart(2, "0")}</span>
              <span className="phase-tool__identity">
                <strong>{toolLabel(item.tool_name)}</strong>
                {identityMeta.length > 0 ? <small>{identityMeta.join(" · ")}</small> : null}
              </span>
              {item.timestamp ? <time>{formatDayTime(item.timestamp)}</time> : null}
              {outcome ? (
                <span className={`phase-tool__outcome phase-tool__outcome--${item.success === 1 ? "success" : "failure"}`}>
                  {outcome}
                </span>
              ) : null}
              {duration ? <span className="phase-tool__duration">{duration}</span> : null}
            </summary>
            <div className="phase-tool__body">
              <section>
                <header>Input</header>
                <pre>{item.input || "Input was not captured in the source artifact."}</pre>
                {item.input_truncated ? <small>Preview truncated at the local response limit.</small> : null}
              </section>
              <section>
                <header>Output</header>
                <pre>{item.output || "Output was not captured in the source artifact."}</pre>
                {item.output_truncated ? <small>Preview truncated at the local response limit.</small> : null}
              </section>
            </div>
            <footer>
              <span>Ledger action: {item.recorded_action}</span>
              <span>Call sequence: {item.call_seq}{item.result_seq == null ? "" : ` to ${item.result_seq}`}</span>
              <Link to={`/sessions/${encodeURIComponent(sessionId)}?${search.toString()}`}>Open in Holotrace →</Link>
            </footer>
          </details>
        );
      })}
    </div>
  );
}

export function PhaseToolTrace({
  phase,
  replay,
  range,
  onClose,
}: {
  phase: ExecutionPhaseSelection;
  replay: MissionReplay;
  range: string;
  onClose: () => void;
}) {
  const actor = replay.actorById.get(phase.actorId);
  const trace = useQuery({
    queryKey: ["phase-tool-trace", phase.actorId, phase.startSeq, phase.endSeq],
    queryFn: ({ signal }) => fetchToolTrace(phase.actorId, phase.startSeq, phase.endSeq, signal),
    enabled: phase.toolEventCount > 0,
    staleTime: Infinity,
  });
  const items = trace.data?.items ?? [];

  return (
    <section className="phase-trace" aria-label={`${phase.label} tool trace`}>
      <header className="phase-trace__head">
        <div>
          <span className="phase-trace__eyebrow">Selected work phase</span>
          <h2>{phase.label}</h2>
          <p>{actor?.project || "Mission"} · {phaseTime(phase)}</p>
        </div>
        <div className="phase-trace__summary">
          <span><strong>{items.length || Math.ceil(phase.toolEventCount / 2)}</strong> calls</span>
          <span><strong>{phase.eventCount}</strong> recorded events</span>
          {phase.models.map((model) => <span key={model}>{model}</span>)}
          <span className={trace.data?.capture.source_backed ? "is-source" : ""}>
            {trace.data?.capture.source_backed ? "source detail" : "normalized detail"}
          </span>
          <button type="button" onClick={onClose}>Close</button>
        </div>
      </header>

      {phase.toolEventCount === 0 ? (
        <div className="phase-trace__empty">This phase contains no tool calls. Select a work card to inspect tool input and output.</div>
      ) : trace.isLoading ? (
        <DiagnosticLoader label="Reading local tool trace" compact rows={3} />
      ) : trace.isError ? (
        <div className="phase-trace__empty">The normalized phase is available, but its local source detail could not be read.</div>
      ) : (
        <>
          <div className="phase-trace__coverage">
            <span>{trace.data?.capture.note}</span>
            <strong>{trace.data?.capture.detailed_items ?? 0}/{trace.data?.capture.total_items ?? 0} calls have source payloads</strong>
          </div>
          <ToolTraceRows items={items} sessionId={phase.actorId} range={range} />
          <div className="phase-trace__tools">
            {items.length === 0 ? <div className="phase-trace__empty">No normalized tool rows fall inside this phase.</div> : null}
          </div>
        </>
      )}
    </section>
  );
}
