import { useEffect, useMemo, useRef } from "react";
import { Link, useOutletContext } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { DiagnosticLoader } from "@/components/DiagnosticLoader";
import { EmptyState } from "@/components/EmptyState";
import { RuntimeGrid } from "@/components/command-grid/RuntimeGrid";
import { SectorMap } from "@/components/command-grid/SectorMap";
import { fetchAttention, type PresenceEvent } from "@/lib/api";
import { projectCommandFloor } from "@/lib/commandFloor";
import { overviewGraphQueryOptions, overviewQueryOptions } from "@/lib/overviewQueries";
import { useIngestStream } from "@/lib/useIngestStream";
import { useLivePresence } from "@/lib/useLivePresence";
import { formatCount, formatDayTime, formatDuration } from "@/lib/utils";

type Ctx = { range: string };

function metricValue(value: number): string {
  return value >= 10_000 ? formatCount(value) : value.toLocaleString();
}

function queueTone(severity: string, state: string): string {
  if (severity === "error" || /fail|error/i.test(state)) return "fail";
  if (severity === "warn" || /wait|review|approval/i.test(state)) return "warn";
  return "info";
}

export function Overview() {
  const { range } = useOutletContext<Ctx>();
  const queryClient = useQueryClient();
  const presenceHandler = useRef<(data: PresenceEvent) => void>(() => {});
  const { connected } = useIngestStream(
    ({ events }) => {
      if (events.length === 0) return;
      for (const key of ["overview", "graph", "attention", "meta"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
    },
    (data) => {
      presenceHandler.current(data);
      queryClient.invalidateQueries({ queryKey: ["attention"] });
    },
  );
  const { presence, onPresenceEvent } = useLivePresence(connected);
  useEffect(() => {
    presenceHandler.current = onPresenceEvent;
  }, [onPresenceEvent]);

  const overview = useQuery(overviewQueryOptions(range));
  const graph = useQuery(overviewGraphQueryOptions(range, overview.isSuccess));
  const attention = useQuery({ queryKey: ["attention"], queryFn: fetchAttention });
  const projection = useMemo(
    () => projectCommandFloor(presence.sessions),
    [presence.sessions],
  );
  const search = new URLSearchParams({ range }).toString();

  if (overview.isLoading) return <DiagnosticLoader label="Reading command ledger" rows={7} />;
  if (overview.isError || !overview.data) {
    return (
      <EmptyState
        title="Command deck unavailable"
        body="The local ledger summary could not be read. Live presence and stored evidence were not modified."
      />
    );
  }

  const data = overview.data;
  const kpis = data.summary.kpis;
  const recent = data.recent.items.slice(0, 6);
  const topTools = data.tools.items.slice(0, 6);
  const attentionItems = [
    ...(attention.data?.items ?? []),
    ...(attention.data?.resumable ?? []),
  ].slice(0, 6);
  const tokensIn = kpis.tokens_est.totals.input_tokens ?? 0;

  return (
    <div className="ops-board">
      <header className="command-page-head">
        <div>
          <div className="command-page-head__eyebrow">local node · {connected ? "stream linked" : "recovery link"}</div>
          <h1>Command deck</h1>
          <p>Live execution, current topology, mission history, and evidence from one local ledger.</p>
        </div>
        <Link className="command-link" to={`/floor?${search}`}>Open command floor →</Link>
      </header>

      <section className="ops-metrics" aria-label="Command metrics">
        <div><span>Sessions</span><strong>{metricValue(kpis.sessions.value)}</strong><small>{range}</small></div>
        <div><span>Messages</span><strong>{metricValue(kpis.messages.value)}</strong><small>captured</small></div>
        <div><span>Tool calls</span><strong>{metricValue(kpis.tool_events.value)}</strong><small>{data.tools.distinct_tools} tools</small></div>
        <div className="is-live"><span>Active now</span><strong>{projection.workingCount}</strong><small>{projection.operatorCount} operators</small></div>
        <div className={projection.waitingCount > 0 ? "is-warn" : ""}><span>Human gates</span><strong>{projection.waitingCount}</strong><small>awaiting input</small></div>
        <div className={attentionItems.length > 0 ? "is-fail" : ""}><span>Priority queue</span><strong>{attentionItems.length}</strong><small>{tokensIn > 0 ? `${formatCount(tokensIn)} input tokens` : "coverage partial"}</small></div>
      </section>

      <div className="ops-board__grid">
        <RuntimeGrid actors={projection.actors} missions={recent} search={search} />

        {graph.isLoading ? (
          <DiagnosticLoader label="Mapping sector topology" compact rows={4} />
        ) : graph.data ? (
          <SectorMap data={graph.data} search={search} compact />
        ) : (
          <section className="ops-panel"><EmptyState title="Sector map unavailable" body="The topology endpoint did not return a graph." /></section>
        )}

        <section className="ops-panel priority-panel">
          <header className="ops-panel__head"><div><span className="ops-panel__slash" aria-hidden />Priority queue <small>human and failure signals</small></div><span>{attentionItems.length} shown</span></header>
          <div className="priority-list">
            {attention.isLoading ? <DiagnosticLoader label="Checking priority queue" compact rows={4} /> : attentionItems.length === 0 ? (
              <div className="ops-empty">No priority signals detected.</div>
            ) : attentionItems.map((item, index) => (
              <Link key={`${item.session_id}-${item.state}-${index}`} className={`priority-row priority-row--${queueTone(item.severity, item.state)}`} to={`/sessions/${encodeURIComponent(item.session_id)}?${search}`}>
                <span className="priority-row__index">P-{String(index + 1).padStart(2, "0")}</span>
                <span><strong>{item.state.replaceAll("_", " ")}</strong><small>{item.reason}</small></span>
                <span>{item.harness || "unknown"}</span>
              </Link>
            ))}
          </div>
        </section>

        <section className="ops-panel trace-panel">
          <header className="ops-panel__head"><div><span className="ops-panel__slash" aria-hidden />Trace shards <small>recent sessions and live signals</small></div><Link to={`/sessions?${search}`}>all traces →</Link></header>
          <div className="trace-list">
            {projection.actors.slice(0, 3).map((actor) => (
              <Link key={actor.id} to={`/floor?${new URLSearchParams({ range, crew: actor.id }).toString()}`} className="trace-row">
                <time>LIVE</time><span className="trace-row__source">{actor.callsign}</span><span>{actor.state.label}</span><small>{actor.project}</small>
              </Link>
            ))}
            {recent.slice(0, Math.max(2, 6 - projection.actors.length)).map((session) => (
              <Link key={session.id} to={`/sessions/${encodeURIComponent(session.navigation_id ?? session.id)}?${search}`} className="trace-row">
                <time>{formatDayTime(session.started_at)}</time><span className="trace-row__source">{session.harness}</span><span>{session.project}</span><small>{session.tool_count} tools · {session.message_count} msgs</small>
              </Link>
            ))}
          </div>
        </section>

        <section className="ops-panel missions-panel">
          <header className="ops-panel__head"><div><span className="ops-panel__slash" aria-hidden />Active missions <small>recent conversation roots</small></div><Link to={`/sessions?${search}`}>{recent.length} shown →</Link></header>
          <div className="mission-list">
            {recent.map((session, index) => (
              <Link key={session.id} to={`/sessions/${encodeURIComponent(session.navigation_id ?? session.id)}?${search}`} className="mission-row">
                <span className="mission-row__id">M-{String(index + 1).padStart(3, "0")}</span>
                <span><strong>{session.project}</strong><small>{session.model} · {session.harness}</small></span>
                <span>{session.message_count}m / {session.tool_count}t</span>
                <span>{formatDuration(session.duration_seconds)}</span>
              </Link>
            ))}
          </div>
        </section>

        <section className="ops-panel tools-panel">
          <header className="ops-panel__head"><div><span className="ops-panel__slash" aria-hidden />Tool packets <small>range traffic</small></div><span>{data.tools.total} calls</span></header>
          <div className="tool-packet-list">
            {topTools.map((tool) => (
              <div key={tool.tool} className="tool-packet-row">
                <span>{tool.tool}</span>
                <span className="tool-packet-row__track"><i style={{ width: `${Math.max(4, tool.share * 100)}%` }} /></span>
                <strong>{tool.count}</strong>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
