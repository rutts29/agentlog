import { Link, useOutletContext, useParams, useSearchParams } from "react-router-dom";
import { Fragment, useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  displaySessionIdentity,
  authoritativeParentNavigationId,
  fetchSessionDetail,
  logicalHarness,
  runtimeHarness,
  type InheritedContext,
  type PresenceEvent,
  type TreeNode,
  type TranscriptSource,
  type TimelineMessage,
} from "@/lib/api";
import { CopyPath } from "@/components/CopyPath";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { Transcript } from "@/components/Transcript";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import {
  HarnessTag,
  ModelBadge,
  RuntimeHarnessLabel,
  TranscriptStorageBadge,
} from "@/components/ui/badges";
import { classifySpeaker, SPEAKER_LEGEND, type SpeakerKind } from "@/lib/speaker";
import {
  findBranchPath,
  projectBranchTree,
  sessionTreeLabel,
  type BranchTreeRow,
} from "@/lib/sessionTree";
import { formatDuration, formatDayTime, harnessColor } from "@/lib/utils";
import {
  createMatchingPresenceRefreshGate,
  createSessionPresenceRefreshScheduler,
  refreshActiveSessionQueries,
  sessionDetailQueryOptions,
  sessionTreeQueryOptions,
} from "@/lib/sessionQueries";
import { useIngestStream } from "@/lib/useIngestStream";

type Ctx = { range: string };
type DetailSession = Awaited<ReturnType<typeof fetchSessionDetail>>["session"];

export function SessionDetail() {
  useOutletContext<Ctx>();
  const { sessionId = "" } = useParams();
  const [params] = useSearchParams();
  const queryClient = useQueryClient();
  const focus = params.get("msg");
  const currentDetailRef = useRef<{
    sessionId: string;
    session: DetailSession | undefined;
  }>({ sessionId, session: undefined });
  const presenceRefresh = useRef(
    createSessionPresenceRefreshScheduler({
      refresh: () => {
        const current = currentDetailRef.current;
        if (!current.sessionId) return;
        return refreshActiveSessionQueries(queryClient, current.sessionId);
      },
    }),
  );
  const presenceRefreshGate = useRef(createMatchingPresenceRefreshGate());

  useIngestStream(({ events }) => {
    if (!events.length || !sessionId) return;
    void refreshActiveSessionQueries(queryClient, sessionId);
  }, (data: PresenceEvent) => {
    const current = currentDetailRef.current;
    const session = current.session;
    if (session && presenceRefreshGate.current.accept(data, session)) {
      presenceRefresh.current.schedule();
    }
  });
  const q = useQuery(sessionDetailQueryOptions(sessionId));
  const treeQuery = useQuery(sessionTreeQueryOptions(sessionId));
  currentDetailRef.current = { sessionId, session: q.data?.session };
  useEffect(
    () => () => {
      presenceRefresh.current.cancel();
      presenceRefreshGate.current.reset();
    },
    [sessionId],
  );
  const isChildSession = Boolean(
    q.data?.session && authoritativeParentNavigationId(q.data.session),
  );
  const branchNavRef = useRef<HTMLElement | null>(null);
  const selectedTreeId = q.data?.session.navigation_id ?? q.data?.session.id ?? sessionId;
  const branchProjection = useMemo(
    () => projectBranchTree(treeQuery.data?.tree),
    [treeQuery.data?.tree],
  );
  const branchPath = useMemo(
    () =>
      findBranchPath(
        branchProjection.rows,
        new Set([
          sessionId,
          selectedTreeId,
          q.data?.session.id ?? "",
        ]),
      ),
    [branchProjection.rows, q.data?.session.id, selectedTreeId, sessionId],
  );
  useEffect(() => {
    const container = branchNavRef.current;
    const selected = container?.querySelector<HTMLElement>("[aria-current='page']");
    if (!container || !selected) return;
    container.scrollTop = Math.max(
      0,
      selected.offsetTop - container.offsetTop - container.clientHeight / 2,
    );
  }, [selectedTreeId, treeQuery.data]);

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
      const k = classifySpeaker(m, { isChildSession }).kind;
      counts.set(k, (counts.get(k) ?? 0) + 1);
    }
    return counts;
  }, [messages, isChildSession]);

  const buckets = useMemo(() => {
    const n = Math.min(48, Math.max(12, messages.length));
    const out = Array.from({ length: n }, () => ({ tools: 0, human: false }));
    messages.forEach((m, i) => {
      const b = out[Math.min(n - 1, Math.floor((i / messages.length) * n))];
      b.tools += m.tool_events.length;
      if (classifySpeaker(m, { isChildSession }).kind === "human") b.human = true;
    });
    return out;
  }, [messages, isChildSession]);

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
    return <LoadingOrb label="Opening transcript" />;
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

  const { session, timeline, anatomy, skills } = q.data;
  const source: TranscriptSource | null = session.source ?? q.data.transcript?.source ?? null;
  const sourceWarning = sourceWarningText(source);
  const transcriptId = session.transcript_session_id ?? q.data.transcript?.id ?? session.id;
  const orchestratorId = session.orchestrator_session_id;
  const isBackingSession = Boolean(orchestratorId && orchestratorId !== session.id);
  const hasSeparateBackingTranscript = transcriptId !== session.id;
  const backSearch = (() => {
    const p = new URLSearchParams(params);
    p.delete("msg");
    p.delete("root");
    return p.toString();
  })();
  const currentNavigationId = session.navigation_id ?? session.id;
  const branchCount = Math.max(
    0,
    treeQuery.data?.tree.descendant_count ??
      (treeQuery.data?.bounds?.total_node_count ??
        branchProjection.rows.length + branchProjection.omittedNodeCount) - 1,
  );
  const omittedBranchCount = Math.max(
    treeQuery.data?.bounds?.omitted_node_count ?? 0,
    branchProjection.omittedNodeCount,
  );
  const inheritedContext = q.data.inherited_context;
  const runtimeBacking = session.runtime_backing_provenance;
  const showInheritedContext = Boolean(
    inheritedContext &&
      (inheritedContext.status ||
        inheritedContext.message_count > 0 ||
        inheritedContext.record_count > 0),
  );
  const maxBucketTools = Math.max(...buckets.map((b) => b.tools), 1);

  return (
    <div className="space-y-3">
      <div>
        <Link
          to={`/sessions${backSearch ? `?${backSearch}` : ""}`}
          className="text-[12px] text-muted-foreground hover:text-foreground"
        >
          ← Sessions <kbd className="ml-1">esc</kbd>
        </Link>
        {branchPath && branchPath.length > 1 ? (
          <BranchBreadcrumb path={branchPath} currentId={currentNavigationId} search={backSearch} />
        ) : null}
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <h1 className="font-mono text-[15px] font-semibold tracking-tight">
            {displaySessionIdentity(session)}
          </h1>
          <HarnessTag harness={logicalHarness(session)} />
          <RuntimeHarnessLabel
            logicalHarness={logicalHarness(session)}
            runtimeHarness={runtimeHarness(session)}
          />
          <ModelBadge
            model={session.model}
            harness={runtimeHarness(session)}
            effort={session.effort}
          />
          <TranscriptStorageBadge
            storage={session.transcript_storage}
            sourceStatus={source?.status}
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

      {sourceWarning ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-card border border-status-warn/40 bg-status-warn/5 px-3 py-2 text-[12px] text-muted-foreground"
        >
          <span className="shrink-0 text-status-warn">!</span>
          <span>{sourceWarning}</span>
        </div>
      ) : null}

      <div className="grid grid-cols-1 items-start gap-3 lg:grid-cols-[minmax(0,1fr)_300px]">
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
              {(speakerCounts.get("human") ?? 0) > 0 ? (
                <span className="text-faint-foreground">
                  <kbd>n</kbd> next human
                </span>
              ) : null}
            </span>
          }
          className="p-0"
        >
          {showInheritedContext && inheritedContext ? (
            <InheritedContextRow
              context={inheritedContext}
              parentId={
                Object.prototype.hasOwnProperty.call(
                  inheritedContext,
                  "parent_navigation_id",
                )
                  ? authoritativeParentNavigationId(inheritedContext)
                  : authoritativeParentNavigationId(session)
              }
              search={backSearch}
            />
          ) : null}
          {timeline.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No messages captured"
                body="This session exists in the ledger but its transcript rows are empty — the source artifact may not have parsed."
                missing={["messages"]}
              />
            </div>
          ) : (
            <Transcript
              timeline={timeline}
              focusId={focus}
              isChildSession={isChildSession}
            />
          )}
        </PanelCard>

        {/* Right: session anatomy, sticky. */}
        <div className="space-y-3 lg:sticky lg:top-0">
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

          {treeQuery.isLoading ? (
            <Card>
              <CardTitle>Conversation branches</CardTitle>
              <div className="mt-2 text-[11px] text-faint-foreground">Loading branches…</div>
            </Card>
          ) : treeQuery.data ? (
            <Card>
              <div className="flex items-baseline justify-between gap-2">
                <CardTitle>Conversation branches</CardTitle>
                <span className="tabular text-[10px] text-faint-foreground">
                  {branchCount.toLocaleString()} workers
                </span>
              </div>
              <nav
                ref={branchNavRef}
                aria-label="Conversation branches"
                className="mt-2 max-h-80 overflow-y-auto pr-1"
              >
                <BranchTree
                  rows={branchProjection.rows}
                  currentId={currentNavigationId}
                  search={backSearch}
                />
                {omittedBranchCount > 0 ? (
                  <div
                    role="status"
                    className="ml-2 mt-1 border-l border-border-faint px-2 py-1 text-[10px] text-faint-foreground"
                  >
                    … {omittedBranchCount.toLocaleString()} more branches not shown
                  </div>
                ) : null}
              </nav>
              {branchCount > 0 ? (
                <Link
                  to={`/orchestration?root=${encodeURIComponent(treeQuery.data.root_id)}${backSearch ? `&${backSearch}` : ""}`}
                  className="mt-2 inline-block text-[11px] text-muted-foreground hover:text-foreground"
                >
                  Open runtime topology →
                </Link>
              ) : null}
            </Card>
          ) : null}

          <Card>
            <CardTitle>Source</CardTitle>
            <div className="mt-2 space-y-2">
              {isBackingSession ? (
                <div>
                  <div className="microlabel text-[10px] text-faint-foreground">
                    Orchestrator session
                  </div>
                  <Link
                    to={sessionHref(orchestratorId!, backSearch)}
                    className="mt-1 block font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    {orchestratorId}
                  </Link>
                </div>
              ) : null}
              <div>
                <div className="microlabel text-[10px] text-faint-foreground">
                  {isBackingSession
                    ? "Backing transcript"
                    : hasSeparateBackingTranscript
                      ? "Orchestrator session"
                      : "Session source"}
                </div>
                <div className="mt-1 font-mono text-[11px] text-muted-foreground">
                  {session.id}
                </div>
                <div className="mt-1">
                  <CopyPath path={session.artifact_path} />
                </div>
              </div>
              {!isBackingSession && hasSeparateBackingTranscript ? (
                <div className="border-t border-border-faint pt-2">
                  <div className="microlabel text-[10px] text-faint-foreground">
                    Backing transcript
                  </div>
                  <Link
                    to={sessionHref(transcriptId, backSearch)}
                    className="mt-1 block font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  >
                    {transcriptId}
                  </Link>
                  <div className="mt-1">
                    <CopyPath path={q.data.transcript?.artifact_path} />
                  </div>
                </div>
              ) : null}
              {!isBackingSession && source?.status !== "legacy" && runtimeBacking ? (
                <details className="border-t border-border-faint pt-2">
                  <summary className="cursor-pointer list-none text-[11px] text-muted-foreground hover:text-foreground">
                    Runtime backing · {runtimeBacking.harness} · validated
                  </summary>
                  <div className="mt-2 space-y-1.5 pl-2 text-[10px] text-faint-foreground">
                    <div>Provenance only; not transcript content or counts.</div>
                    <div className="break-all font-mono">
                      {runtimeBacking.session_id}
                    </div>
                    <CopyPath path={runtimeBacking.artifact_path ?? null} />
                  </div>
                </details>
              ) : null}
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

function sessionHref(id: string, search: string): string {
  return `/sessions/${encodeURIComponent(id)}${search ? `?${search}` : ""}`;
}

function shortSessionId(id: string): string {
  const external = id.includes(":") ? id.slice(id.indexOf(":") + 1) : id;
  return external.length > 13
    ? `${external.slice(0, 8)}…${external.slice(-4)}`
    : external;
}

function BranchBreadcrumb({
  path,
  currentId,
  search,
}: {
  path: TreeNode[];
  currentId: string;
  search: string;
}) {
  return (
    <nav aria-label="Session branch path" className="mt-2 flex min-w-0 items-center gap-1 text-[11px] text-faint-foreground">
      {path.map((node, index) => {
        const navigationId = node.navigation_id ?? node.id;
        const current = navigationId === currentId || node.id === currentId;
        return (
          <span key={node.id} className="inline-flex min-w-0 items-center gap-1">
            {index > 0 ? <span aria-hidden>›</span> : null}
            {current ? (
              <span aria-current="page" className="max-w-40 truncate font-mono text-muted-foreground">
                {index === 0 ? sessionTreeLabel(node, 0) : shortSessionId(node.id)}
              </span>
            ) : (
              <Link
                to={sessionHref(navigationId, search)}
                className="max-w-40 truncate font-mono hover:text-foreground"
              >
                {index === 0 ? sessionTreeLabel(node, 0) : shortSessionId(node.id)}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}

function InheritedContextRow({
  context,
  parentId,
  search,
}: {
  context: InheritedContext;
  parentId: string | null;
  search: string;
}) {
  const counts = [
    context.message_count > 0
      ? `${context.message_count.toLocaleString()} inherited ${context.message_count === 1 ? "message" : "messages"}`
      : null,
    context.record_count > 0
      ? `${context.record_count.toLocaleString()} source ${context.record_count === 1 ? "record" : "records"}`
      : null,
  ].filter(Boolean);

  return (
    <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-x-3 border-b border-border-faint bg-background/30 px-4 py-2.5">
      <div className="pt-[2px] text-right">
        <div className="microlabel text-[10px] leading-4 text-faint-foreground">context</div>
      </div>
      <div className="min-w-0 border-l-2 border-border pl-3 text-[11px] text-muted-foreground">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span>Inherited context from parent</span>
          {parentId ? (
            <Link to={sessionHref(parentId, search)} className="text-foreground/80 hover:text-foreground">
              View parent →
            </Link>
          ) : null}
        </div>
        <div className="mt-0.5 text-faint-foreground">
          {counts.length > 0 ? counts.join(" · ") : "Parent context"} · not repeated in this transcript
        </div>
      </div>
    </div>
  );
}

function BranchTree({
  rows,
  currentId,
  search,
}: {
  rows: BranchTreeRow[];
  currentId: string;
  search: string;
}) {
  const shownWorkflowGroups = new Set<string>();
  return (
    <ul role="tree">
      {rows.map(({ node, depth }, index) => {
        const navigationId = node.navigation_id ?? node.id;
        const selected = navigationId === currentId || node.id === currentId;
        const logical = logicalHarness(node);
        const runtime = runtimeHarness(node);
        const groupKey = node.workflow_group_id
          ? `${node.parent_navigation_id ?? ""}:${node.workflow_group_id}`
          : null;
        const showWorkflowGroup = Boolean(
          groupKey && !shownWorkflowGroups.has(groupKey),
        );
        if (groupKey) shownWorkflowGroups.add(groupKey);
        return (
          <Fragment key={`${navigationId}-${index}`}>
            {showWorkflowGroup ? (
              <li
                role="presentation"
                className="mt-2 border-l border-border-faint pl-2 text-[10px] font-medium uppercase tracking-[0.12em] text-faint-foreground"
                style={{ marginLeft: `${Math.min(depth, 12) * 8}px` }}
              >
                {node.workflow_group_label ?? node.workflow_group_id}
              </li>
            ) : null}
            <li
              role="treeitem"
              aria-level={depth + 1}
              style={{ paddingLeft: `${Math.min(depth, 12) * 8}px` }}
            >
            <Link
              to={sessionHref(navigationId, search)}
              aria-current={selected ? "page" : undefined}
              className={`block rounded-control px-2 py-1.5 hover:bg-muted/50 ${selected ? "bg-muted/60" : ""}`}
              style={
                selected
                  ? { boxShadow: `inset 2px 0 0 ${harnessColor(logical)}` }
                  : undefined
              }
            >
              <div className="flex min-w-0 items-baseline gap-2">
                <span className="shrink-0 text-[11px] text-foreground/90">
                  {sessionTreeLabel(node, depth)}
                </span>
                <span
                  className="min-w-0 truncate font-mono text-[10px] text-faint-foreground"
                  title={node.id}
                >
                  {shortSessionId(node.id)}
                </span>
                <span className="tabular ml-auto shrink-0 text-[10px] text-faint-foreground">
                  {node.message_count.toLocaleString()} msgs
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
                <HarnessTag harness={logical} muted className="text-[10px]" />
                <RuntimeHarnessLabel
                  logicalHarness={logical}
                  runtimeHarness={runtime}
                />
              </div>
            </Link>
            </li>
          </Fragment>
        );
      })}
    </ul>
  );
}

function sourceWarningText(source: TranscriptSource | null): string | null {
  if (!source || source.status === "ready" || source.status === "legacy") return null;
  const prefix = source.status === "source_changed" ? "Source changed" : "Source unavailable";
  return `${prefix}: ${source.warning ?? "the canonical transcript could not be read safely."}`;
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
