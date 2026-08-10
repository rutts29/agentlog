import { Link, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchSessionDetail, type TimelineMessage } from "@/lib/api";
import { CopyPath } from "@/components/CopyPath";
import { EmptyState } from "@/components/EmptyState";
import { Transcript } from "@/components/Transcript";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { HarnessTag, ModelBadge } from "@/components/ui/badges";
import { classifySpeaker, SPEAKER_LEGEND, type SpeakerKind } from "@/lib/speaker";
import { formatDuration, formatDayTime, harnessColor } from "@/lib/utils";

type Ctx = { range: string };

export function SessionDetail() {
  useOutletContext<Ctx>();
  const { sessionId = "" } = useParams();
  const [params] = useSearchParams();
  const focus = params.get("msg");

  const q = useQuery({
    queryKey: ["session", sessionId],
    queryFn: () => fetchSessionDetail(sessionId),
    enabled: Boolean(sessionId),
  });

  const messages = useMemo(
    () =>
      (q.data?.timeline ?? []).filter(
        (t): t is TimelineMessage => t.kind === "message",
      ),
    [q.data],
  );

  const speakerCounts = useMemo(() => {
    const counts = new Map<SpeakerKind, number>();
    for (const m of messages) {
      const k = classifySpeaker(m).kind;
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return counts;
  }, [messages]);

  const buckets = useMemo(() => {
    const n = Math.min(48, Math.max(12, messages.length));
    const out = Array.from({ length: n }, () => ({ tools: 0, human: false }));
    messages.forEach((m, i) => {
      const b = out[Math.min(n - 1, Math.floor((i / messages.length) * n))];
      b.tools += m.tool_events.length;
      if (classifySpeaker(m).kind === "human") b.human = true;
    });
    return out;
  }, [messages]);

  const modelsUsed = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of messages) {
      if (m.model) counts.set(m.model, (counts.get(m.model) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [messages]);

  const toolMix = useMemo(() => {
    const counts = new Map<string, number>();
    for (const m of messages) {
      for (const t of m.tool_events) {
        counts.set(t.tool_name, (counts.get(t.tool_name) ?? 0) + 1);
      }
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
  }, [messages]);

  if (q.isLoading) {
    return <div className="text-[13px] text-muted-foreground">Loading…</div>;
  }
  if (q.isError || !q.data) {
    return (
      <EmptyState
        title="Session not found"
        body="This session id is not in the ledger. It may belong to a range that has not been ingested, or the id is a bare external id from another harness."
        missing={["sessions.id", "sessions.external_id"]}
      />
    );
  }

  const { session, timeline, anatomy, children, skills } = q.data;
  const backSearch = (() => {
    const p = new URLSearchParams(params);
    p.delete("msg");
    return p.toString();
  })();
  const maxBucketTools = Math.max(...buckets.map((b) => b.tools), 1);

  return (
    <div className="space-y-3">
      <div>
        <Link
          to={`/sessions?${backSearch}`}
          className="text-[12px] text-muted-foreground hover:text-foreground"
        >
          ← Sessions <kbd className="ml-1">esc</kbd>
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <h1 className="font-mono text-[15px] font-semibold tracking-tight">
            {session.id}
          </h1>
          <HarnessTag harness={session.harness} />
          <ModelBadge
            model={session.model}
            harness={session.harness}
            effort={session.effort}
          />
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-muted-foreground">
          <span className="text-foreground/90">{session.project}</span>
          {session.branch ? (
            <span className="font-mono text-[11px]">{session.branch}</span>
          ) : null}
          {session.commit_sha ? (
            <span className="font-mono text-[11px] text-faint-foreground">
              {session.commit_sha.slice(0, 10)}
            </span>
          ) : null}
          <span className="text-faint-foreground">·</span>
          <span className="tabular">
            {formatDayTime(session.started_at)}
            {session.ended_at ? ` → ${formatDayTime(session.ended_at)}` : ""}
          </span>
          <span className="text-faint-foreground">·</span>
          <span className="tabular">{formatDuration(session.duration_seconds)}</span>
        </div>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_300px] items-start gap-3">
        {/* Left: the transcript. */}
        <PanelCard
          title="Transcript"
          aside={
            <span className="flex items-center gap-3">
              {SPEAKER_LEGEND.filter((s) => (speakerCounts.get(s.kind) ?? 0) > 0).map(
                (s) => (
                  <span
                    key={s.kind}
                    className="inline-flex items-center gap-1 text-[11px]"
                    title={s.description}
                  >
                    <span
                      aria-hidden
                      className="inline-block h-[6px] w-[6px] rounded-[1px]"
                      style={{ background: s.color }}
                    />
                    <span className="text-faint-foreground">
                      {s.label} {speakerCounts.get(s.kind)}
                    </span>
                  </span>
                ),
              )}
              <span className="text-faint-foreground">
                <kbd>n</kbd> next human
              </span>
            </span>
          }
          className="p-0"
        >
          {timeline.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No messages captured"
                body="This session exists in the ledger but its transcript rows are empty — the source artifact may not have parsed."
                missing={["messages"]}
              />
            </div>
          ) : (
            <Transcript timeline={timeline} focusId={focus} />
          )}
        </PanelCard>

        {/* Right: session anatomy, sticky. */}
        <div className="sticky top-0 space-y-3">
          <Card>
            <CardTitle>Anatomy</CardTitle>
            <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-2">
              <Anat label="messages" value={anatomy.message_count} />
              <Anat label="tool calls" value={anatomy.tool_count} />
              <Anat label="windows" value={anatomy.window_count} />
              <Anat label="children" value={anatomy.child_count} />
            </div>

            <div className="mt-4">
              <div className="microlabel text-[10px] text-faint-foreground">
                Tool bursts across session
              </div>
              <div className="mt-1.5 flex h-9 items-end gap-px">
                {buckets.map((b, i) => (
                  <div key={i} className="flex flex-1 flex-col items-stretch gap-[2px]">
                    <div
                      className="w-full rounded-[1px]"
                      style={{
                        height: `${Math.max(b.tools > 0 ? 8 : 3, (b.tools / maxBucketTools) * 100)}%`,
                        background: "var(--foreground)",
                        opacity: b.tools > 0 ? 0.55 : 0.12,
                      }}
                    />
                    <div
                      className="h-[2px] w-full rounded-[1px]"
                      style={{
                        background: b.human ? "var(--speaker-human)" : "transparent",
                      }}
                    />
                  </div>
                ))}
              </div>
              <div className="mt-1 flex justify-between text-[9px] text-faint-foreground">
                <span>start</span>
                <span style={{ color: "var(--speaker-human)" }}>▂ human turn</span>
                <span>end</span>
              </div>
            </div>
          </Card>

          {modelsUsed.length > 0 ? (
            <Card>
              <CardTitle>Models in transcript</CardTitle>
              <div className="mt-2 space-y-1">
                {modelsUsed.map(([model, count]) => (
                  <div key={model} className="flex items-baseline justify-between gap-2">
                    <span className="min-w-0 truncate font-mono text-[11px] text-muted-foreground">
                      {model}
                    </span>
                    <span className="tabular text-[11px] text-faint-foreground">
                      {count}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

          {toolMix.length > 0 ? (
            <Card>
              <CardTitle>Tool mix</CardTitle>
              <div className="mt-2 space-y-1">
                {toolMix.map(([tool, count]) => (
                  <div key={tool} className="flex items-center gap-2">
                    <span className="w-28 shrink-0 truncate font-mono text-[11px] text-muted-foreground">
                      {tool}
                    </span>
                    <div className="h-[3px] flex-1 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-foreground/60"
                        style={{ width: `${(count / toolMix[0][1]) * 100}%` }}
                      />
                    </div>
                    <span className="tabular w-8 shrink-0 text-right text-[11px] text-faint-foreground">
                      {count}
                    </span>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

          {skills.length > 0 ? (
            <Card>
              <CardTitle>Skills fired</CardTitle>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {skills.map((s) => (
                  <span
                    key={`${s.skill_name}-${s.exposure_type}`}
                    className="rounded-[4px] border border-border px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                    title={s.exposure_type}
                  >
                    {s.skill_name}
                    <span className="ml-1 text-faint-foreground">×{s.c}</span>
                  </span>
                ))}
              </div>
            </Card>
          ) : null}

          {children.length > 0 || session.parent_session_id ? (
            <Card>
              <CardTitle>Orchestration</CardTitle>
              {session.parent_session_id ? (
                <div className="mt-2 text-[11px] text-muted-foreground">
                  <span className="text-faint-foreground">parent </span>
                  <Link
                    to={`/sessions/${encodeURIComponent(session.parent_session_id)}?${backSearch}`}
                    className="font-mono hover:text-foreground"
                  >
                    {session.parent_session_id}
                  </Link>
                </div>
              ) : null}
              {children.length > 0 ? (
                <div className="mt-2 space-y-1">
                  {children.map((c) => (
                    <Link
                      key={c.id}
                      to={`/sessions/${encodeURIComponent(c.id)}?${backSearch}`}
                      className="flex items-center gap-2 text-[11px] hover:text-foreground"
                    >
                      <span
                        aria-hidden
                        className="inline-block h-[5px] w-[5px] shrink-0 rounded-full"
                        style={{ background: harnessColor(c.harness) }}
                      />
                      <span className="min-w-0 truncate font-mono text-muted-foreground">
                        {c.id}
                      </span>
                      <span className="tabular ml-auto shrink-0 text-faint-foreground">
                        {c.message_count} msgs
                      </span>
                    </Link>
                  ))}
                  <Link
                    to={`/orchestration?root=${encodeURIComponent(session.id)}&${backSearch}`}
                    className="mt-1 inline-block text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    Open orchestration tree →
                  </Link>
                </div>
              ) : null}
            </Card>
          ) : null}

          <Card>
            <CardTitle>Source</CardTitle>
            <div className="mt-2">
              <CopyPath path={session.artifact_path} />
            </div>
            {session.cwd ? (
              <div className="mt-2 break-all font-mono text-[10px] leading-[1.4] text-faint-foreground">
                cwd {session.cwd}
              </div>
            ) : null}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Anat({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="tabular text-[16px] font-semibold leading-[1.2]">
        {value.toLocaleString()}
      </div>
      <div className="text-[10px] text-faint-foreground">{label}</div>
    </div>
  );
}
