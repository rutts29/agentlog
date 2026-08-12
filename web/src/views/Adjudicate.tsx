import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchAdjudicationQueue,
  fetchAdjudicationReport,
  fetchAdjudicationTaxonomy,
  postAdjudication,
  type AdjudicationLabels,
  type AdjudicationQueueResponse,
  type AdjudicationTurn,
  type TaxonomyOption,
} from "@/lib/api";
import { classifySpeaker } from "@/lib/speaker";
import { useViewShortcuts } from "@/lib/keyboard";
import { EmptyState } from "@/components/EmptyState";
import { LoadingOrb } from "@/components/LoadingOrb";
import { Card, CardTitle, PanelCard } from "@/components/ui/card";
import { HarnessTag, ModelBadge } from "@/components/ui/badges";
import { cn } from "@/lib/utils";

type Step =
  | "human_present"
  | "turn_kind"
  | "user_stance"
  | "agent_stance"
  | "prior_outcome"
  | "done";

type FormState = {
  triage: "yes" | "no" | "unclear" | null;
  turn_kind: string[];
  user_stance: string | null;
  agent_stance: string | null;
  prior_outcome: string | null;
  vague_fields: string[];
  notes: string;
};

const EMPTY_FORM: FormState = {
  triage: null,
  turn_kind: [],
  user_stance: null,
  agent_stance: null,
  prior_outcome: null,
  vague_fields: [],
  notes: "",
};

function labelsEqual(
  a: Pick<AdjudicationLabels, "turn_kind" | "user_stance" | "agent_stance" | "prior_outcome">,
  b: Pick<AdjudicationLabels, "turn_kind" | "user_stance" | "agent_stance" | "prior_outcome">,
): boolean {
  const as = [...(a.turn_kind || [])].sort().join("|");
  const bs = [...(b.turn_kind || [])].sort().join("|");
  return (
    as === bs &&
    a.user_stance === b.user_stance &&
    a.agent_stance === b.agent_stance &&
    a.prior_outcome === b.prior_outcome
  );
}

/** Triage no/unclear intentionally clears turn_kind; do not render that as a failed label. */
function triageFromNotes(notes: string | undefined | null): "no" | "unclear" | "yes" | null {
  const n = notes || "";
  if (n.includes("triage:no_human")) return "no";
  if (n.includes("triage:unclear_human")) return "unclear";
  if (n.includes("triage:has_human")) return "yes";
  return null;
}

function humanTurnKindDisplay(
  labels: Pick<AdjudicationLabels, "turn_kind" | "notes">,
): string {
  const triage = triageFromNotes(labels.notes);
  if (triage === "no") return "n/a (triage: agent/harness)";
  if (triage === "unclear") return "n/a (triage: unclear)";
  const kinds = [...(labels.turn_kind || [])].sort().join(", ");
  return kinds || "(none selected)";
}

function Chip({
  active,
  hotkey,
  onClick,
  children,
}: {
  active?: boolean;
  hotkey?: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[4px] border px-2 py-1.5 text-left text-[12px] leading-snug",
        active
          ? "border-primary/40 bg-muted text-foreground"
          : "border-border text-muted-foreground hover:text-foreground",
      )}
    >
      {hotkey ? (
        <kbd className="shrink-0 rounded-[3px] border border-border-faint bg-background px-1 font-mono text-[10px] text-faint-foreground">
          {hotkey}
        </kbd>
      ) : null}
      <span>{children}</span>
    </button>
  );
}

function WindowTurns({ turns }: { turns: AdjudicationTurn[] }) {
  if (!turns.length) {
    return (
      <div className="px-4 py-6 text-[12px] text-muted-foreground">
        No turn text available for this window.
      </div>
    );
  }
  return (
    <div className="divide-y divide-border-faint">
      {turns.map((turn) => {
        const spec = classifySpeaker({
          role: turn.role,
          text: turn.text,
          is_tool_plumbing: turn.is_tool_plumbing,
          request_kind:
            turn.slot === "human" || turn.slot === "next_human"
              ? "substantive"
              : null,
        });
        // Force full text — adjudication needs the whole window, not a 220-char preview.
        const body =
          turn.slot === "human" || turn.slot === "next_human"
            ? (spec.extractedQuery ?? turn.text)
            : turn.text;
        const slotLabel =
          turn.slot === "prior_agent"
            ? "prior agent"
            : turn.slot === "next_human"
              ? "next human"
              : spec.label;
        const human = turn.slot === "human" || turn.slot === "next_human";
        return (
          <div
            key={turn.id}
            className={cn(
              "grid grid-cols-[92px_minmax(0,1fr)] gap-x-3 px-4 py-2.5",
              human && "bg-[var(--speaker-human-dim)]",
            )}
          >
            <div className="pt-[2px] text-right">
              <div
                className={cn("microlabel text-[10px] leading-4", human && "font-semibold")}
                style={{ color: spec.color }}
              >
                {slotLabel}
              </div>
            </div>
            <div
              className="min-w-0 border-l-2 pl-3"
              style={{
                borderColor: human
                  ? spec.color
                  : `color-mix(in srgb, ${spec.color} 55%, transparent)`,
              }}
            >
              {turn.model ? (
                <div className="mb-1 font-mono text-[10px] text-faint-foreground">
                  {turn.model}
                </div>
              ) : null}
              <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.45] text-foreground/90">
                {body || "(no text)"}
              </pre>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function CompareField({
  label,
  human,
  llm,
  neutral = false,
}: {
  label: string;
  human: string;
  llm: string;
  /** When true, skip red/green agree coloring (e.g. triage skipped enums). */
  neutral?: boolean;
}) {
  const agree = !neutral && human === llm;
  const disagree = !neutral && human !== llm;
  return (
    <div className="grid grid-cols-[110px_1fr_1fr] gap-2 border-b border-border-faint py-1.5 text-[12px] last:border-0">
      <div className="microlabel text-[10px] text-faint-foreground">{label}</div>
      <div
        className="font-mono"
        style={
          agree
            ? { color: "var(--status-ok)" }
            : disagree
              ? { color: "var(--status-error)" }
              : undefined
        }
      >
        {human || "null"}
      </div>
      <div
        className={cn("font-mono", (disagree || neutral) && "text-muted-foreground")}
        style={agree ? { color: "var(--status-ok)" } : undefined}
      >
        {llm || "null"}
      </div>
    </div>
  );
}

export function Adjudicate() {
  const qc = useQueryClient();
  const queue = useQuery({
    queryKey: ["adjudication-queue"],
    queryFn: () => fetchAdjudicationQueue(false),
  });
  const items = queue.data?.items ?? [];
  const taxonomy = useQuery({
    queryKey: ["adjudication-taxonomy"],
    queryFn: fetchAdjudicationTaxonomy,
    enabled: items.length > 0,
  });
  const report = useQuery({
    queryKey: ["adjudication-report"],
    queryFn: fetchAdjudicationReport,
    enabled: items.length > 0,
  });

  // Single source of truth for completed count and position.
  const done = queue.data?.progress.done ?? items.filter((i) => i.adjudicated).length;
  const total = queue.data?.progress.total ?? items.length;

  const [index, setIndex] = useState(0);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [step, setStep] = useState<Step>("human_present");
  const [revealed, setRevealed] = useState(false);
  const [savedHuman, setSavedHuman] = useState<AdjudicationLabels | null>(null);
  const [revealedLlm, setRevealedLlm] = useState<AdjudicationLabels | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryPayload, setRetryPayload] = useState<FormState | null>(null);
  const [bootstrapped, setBootstrapped] = useState(false);
  const safeIndex = items.length ? Math.min(Math.max(index, 0), items.length - 1) : 0;
  const current = items[safeIndex] ?? null;
  const position = current?.position ?? (items.length ? safeIndex + 1 : 0);
  const atStart = safeIndex <= 0;
  const atEnd = !items.length || safeIndex >= items.length - 1;

  useEffect(() => {
    if (!items.length || bootstrapped) return;
    const pending = items.findIndex((i) => !i.adjudicated);
    setIndex(pending >= 0 ? pending : 0);
    setBootstrapped(true);
  }, [items, bootstrapped]);

  useEffect(() => {
    if (!current) return;
    if (current.adjudication) {
      const notes = current.adjudication.notes ?? "";
      const triage: FormState["triage"] = notes.includes("triage:no_human")
        ? "no"
        : notes.includes("triage:unclear_human")
          ? "unclear"
          : "yes";
      setForm({
        triage,
        turn_kind: [...current.adjudication.turn_kind],
        user_stance: current.adjudication.user_stance,
        agent_stance: current.adjudication.agent_stance,
        prior_outcome: current.adjudication.prior_outcome,
        vague_fields: [],
        notes,
      });
      setRevealed(true);
      setSavedHuman(current.adjudication);
      setRevealedLlm(current.llm);
      setStep("done");
    } else {
      setForm(EMPTY_FORM);
      setRevealed(false);
      setSavedHuman(null);
      setRevealedLlm(null);
      setStep("human_present");
    }
    setError(null);
    setRetryPayload(null);
  }, [current?.window_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveMut = useMutation({
    mutationFn: (payload: FormState) => {
      if (!current) throw new Error("no window");
      return postAdjudication(current.window_id, {
        turn_kind: payload.turn_kind,
        user_stance: payload.user_stance,
        agent_stance: payload.agent_stance,
        prior_outcome: payload.prior_outcome,
        notes: payload.notes,
        source: "audit_pack",
        triage: payload.triage,
        vague_fields: payload.vague_fields,
      });
    },
    onSuccess: (data) => {
      setSavedHuman(data);
      setRevealedLlm(data.llm ?? null);
      setRevealed(true);
      setStep("done");
      setError(null);
      setRetryPayload(null);
      // Optimistic progress update so header/report agree immediately.
      qc.setQueryData<AdjudicationQueueResponse>(["adjudication-queue"], (prev) => {
        if (!prev) return prev;
        const itemsNext = prev.items.map((it) =>
          it.window_id === data.window_id
            ? {
                ...it,
                adjudicated: true,
                adjudication: data,
                llm: data.llm ?? it.llm,
              }
            : it,
        );
        const doneNext = itemsNext.filter((i) => i.adjudicated).length;
        return {
          ...prev,
          items: itemsNext,
          progress: {
            done: doneNext,
            total: itemsNext.length,
            remaining: Math.max(0, itemsNext.length - doneNext),
          },
        };
      });
      void qc.invalidateQueries({ queryKey: ["adjudication-report"] });
    },
    onError: (err: Error) => {
      const raw = err.message || "unknown error";
      const busy =
        /\b(503|locked|busy)\b/i.test(raw) || /database is locked/i.test(raw);
      setError(
        busy
          ? "Database was busy. Your labels are still here — press Retry save."
          : `Save failed. Your labels are still here — press Retry save. (${raw})`,
      );
    },
  });

  function go(delta: number) {
    if (!items.length) return;
    setIndex((i) => Math.min(items.length - 1, Math.max(0, i + delta)));
  }

  function jumpToPosition(raw: string | number) {
    if (!items.length) return;
    const n = typeof raw === "number" ? raw : Number.parseInt(String(raw).trim(), 10);
    if (!Number.isFinite(n)) return;
    const clamped = Math.min(items.length, Math.max(1, Math.trunc(n)));
    setIndex(clamped - 1);
  }

  function jumpNextUnlabeled() {
    if (!items.length) return;
    const from = safeIndex + 1;
    const ahead = items.findIndex((it, i) => i >= from && !it.adjudicated);
    if (ahead >= 0) {
      setIndex(ahead);
      return;
    }
    const wrap = items.findIndex((it) => !it.adjudicated);
    if (wrap >= 0) setIndex(wrap);
  }

  function commit(next: FormState) {
    if (saveMut.isPending) return;
    setRetryPayload(next);
    setError(null);
    saveMut.mutate(next);
  }

  function retrySave() {
    if (saveMut.isPending) return;
    const payload = retryPayload ?? form;
    commit(payload);
  }

  function applyTriage(value: "yes" | "no" | "unclear") {
    const next = { ...form, triage: value };
    setForm(next);
    if (value === "yes") {
      setStep("turn_kind");
      return;
    }
    commit(next);
  }

  function markVague(field: "user_stance" | "agent_stance" | "prior_outcome") {
    const next: FormState = {
      ...form,
      [field]: "abstain",
      vague_fields: form.vague_fields.includes(field)
        ? form.vague_fields
        : [...form.vague_fields, field],
    };
    setForm(next);
    advanceAfter(field, next);
  }

  function advanceAfter(field: Step, nextForm: FormState) {
    if (field === "turn_kind") setStep("user_stance");
    else if (field === "user_stance") setStep("agent_stance");
    else if (field === "agent_stance") setStep("prior_outcome");
    else if (field === "prior_outcome") commit(nextForm);
  }

  const stepOptions: TaxonomyOption[] = useMemo(() => {
    if (!taxonomy.data) return [];
    if (step === "human_present") return taxonomy.data.human_present;
    if (step === "turn_kind") return taxonomy.data.turn_kind;
    if (step === "user_stance") return taxonomy.data.user_stance;
    if (step === "agent_stance") return taxonomy.data.agent_stance;
    if (step === "prior_outcome") return taxonomy.data.prior_outcome;
    return [];
  }, [taxonomy.data, step]);

  useViewShortcuts((e) => {
    if (!taxonomy.data || !current) return false;

    if (e.key === "ArrowRight") {
      if (!atEnd) go(1);
      return true;
    }
    if (e.key === "ArrowLeft") {
      if (!atStart) go(-1);
      return true;
    }
    if (e.key === "]") {
      jumpNextUnlabeled();
      return true;
    }
    if (e.key === "Enter") {
      if (revealed) {
        if (!atEnd) go(1);
        return true;
      }
      if (step === "turn_kind") {
        setStep("user_stance");
        return true;
      }
      if (step === "prior_outcome") {
        commit(form);
      }
      return true;
    }
    if (revealed || step === "done") return false;

    const key = e.key.toLowerCase();
    if (key === "v" && step !== "human_present" && step !== "turn_kind") {
      markVague(step);
      return true;
    }
    const hit = stepOptions.find((o) => o.key === key);
    if (!hit) return false;

    if (step === "human_present") {
      applyTriage(hit.value as "yes" | "no" | "unclear");
      return true;
    }
    if (step === "turn_kind") {
      setForm((f) => {
        const has = f.turn_kind.includes(hit.value);
        return {
          ...f,
          turn_kind: has
            ? f.turn_kind.filter((k) => k !== hit.value)
            : [...f.turn_kind, hit.value],
        };
      });
      return true;
    }
    if (hit.value === "abstain" && hit.key === "v") {
      markVague(step);
      return true;
    }
    const next = { ...form, [step]: hit.value } as FormState;
    setForm(next);
    advanceAfter(step, next);
    return true;
  });

  if (queue.isLoading) {
    return <LoadingOrb label="Checking escalations" />;
  }
  if (queue.isError || !queue.data) {
    return (
      <EmptyState
        title="Could not load manual-review queue"
        body="Refresh or check that agentlog serve is running."
      />
    );
  }
  if (items.length === 0) {
    return (
      <div className="space-y-3">
        <div className="flex items-baseline justify-between gap-3">
          <div>
            <h1 className="text-[18px] font-semibold tracking-tight">Manual review</h1>
            <p className="mt-0.5 text-[12px] text-faint-foreground">
              Reserved for exceptional cases that need an explicit human decision
            </p>
          </div>
          <span className="text-right text-[12px] text-faint-foreground">
            historical sessions are not queued
          </span>
        </div>
        <EmptyState
          title="No manual escalations"
          body="Routine session analysis does not populate this page. Ambiguous or high-risk items may appear here when explicitly escalated for human review."
          missing={["manual escalation only", "automatic population off"]}
          className="min-h-[240px]"
        />
      </div>
    );
  }
  if (taxonomy.isLoading) {
    return <LoadingOrb label="Preparing review" />;
  }
  if (taxonomy.isError || !taxonomy.data) {
    return (
      <EmptyState
        title="Could not load review taxonomy"
        body="Refresh or check that agentlog serve is running."
      />
    );
  }

  const pct = total ? Math.round((done / total) * 100) : 0;
  const elig = queue.data.original_pack_eligibility;
  const turns = current?.payload.turns ?? [];
  const sessionHref = current?.session_id
    ? `/sessions/${encodeURIComponent(current.session_id)}`
    : null;

  return (
    <div className="space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <div>
          <h1 className="text-[18px] font-semibold tracking-tight">Manual review</h1>
          <p className="mt-0.5 text-[12px] text-faint-foreground">
            Escalated blind review — commit before the LLM answer is revealed
          </p>
        </div>
        <div className="text-right text-[12px] text-muted-foreground">
          <div className="tabular">
            completed{" "}
            <span className="text-foreground">
              {done}/{total}
            </span>
          </div>
          <div className="tabular text-faint-foreground">
            window{" "}
            <span className="text-muted-foreground">
              {position}/{total}
            </span>
          </div>
        </div>
      </div>

      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary/70 transition-[width]"
          style={{ width: `${pct}%` }}
        />
      </div>

      {elig ? (
        <div className="text-[11px] text-faint-foreground">
          Original pack: {elig.ineligible}/{elig.total} ineligible (
          {elig.ineligible_rate == null
            ? "—"
            : `${(elig.ineligible_rate * 100).toFixed(0)}%`}
          ) — min human text {elig.min_human_chars} chars after unwrap
        </div>
      ) : null}

      <div className="grid grid-cols-[minmax(0,1fr)_300px] gap-3">
        <div className="space-y-3">
          {!current ? (
            <EmptyState title="Queue empty" body="No adjudicable windows available." />
          ) : (
            <>
              <PanelCard
                title={
                  <span className="flex items-center gap-2">
                    <span className="tabular">
                      window {position}/{total}
                    </span>
                    <HarnessTag harness={current.harness ?? "other"} />
                    <ModelBadge
                      model={current.payload.model}
                      harness={current.harness ?? undefined}
                    />
                  </span>
                }
                aside={
                  sessionHref ? (
                    <Link
                      to={sessionHref}
                      target="_blank"
                      rel="noreferrer"
                      className="text-[11px] text-muted-foreground hover:text-foreground"
                    >
                      open session
                    </Link>
                  ) : (
                    <span className="font-mono text-[11px] text-faint-foreground">
                      {current.window_id}
                    </span>
                  )
                }
              >
                <div className="max-h-[520px] overflow-y-auto">
                  <WindowTurns turns={turns} />
                </div>
              </PanelCard>

              <Card>
                <div className="mb-2 flex items-baseline justify-between gap-2">
                  <CardTitle>
                    {revealed
                      ? "Committed"
                      : step === "human_present"
                        ? "Is there a real human turn here?"
                        : step === "turn_kind"
                          ? "What is the human doing? (multi — Enter to continue)"
                          : step === "user_stance"
                            ? "How does the human sound?"
                            : step === "agent_stance"
                              ? "What is the agent doing?"
                              : step === "prior_outcome"
                                ? "What happened next?"
                                : "Labels"}
                  </CardTitle>
                  <div className="text-[11px] text-faint-foreground">
                    keys select · v = too vague · Enter{" "}
                    {revealed ? "next" : step === "turn_kind" ? "save/continue" : "—"} ·
                    arrows navigate
                  </div>
                </div>

                {!revealed && step !== "done" ? (
                  <div className="flex flex-wrap gap-1.5">
                    {stepOptions.map((opt) => {
                      const active =
                        step === "turn_kind"
                          ? form.turn_kind.includes(opt.value)
                          : step === "human_present"
                            ? form.triage === opt.value
                            : (form[step] as string | null) === opt.value;
                      return (
                        <Chip
                          key={`${opt.key}-${opt.value}`}
                          hotkey={opt.key}
                          active={active}
                          onClick={() => {
                            if (step === "human_present") {
                              applyTriage(opt.value as "yes" | "no" | "unclear");
                              return;
                            }
                            if (step === "turn_kind") {
                              setForm((f) => ({
                                ...f,
                                turn_kind: f.turn_kind.includes(opt.value)
                                  ? f.turn_kind.filter((k) => k !== opt.value)
                                  : [...f.turn_kind, opt.value],
                              }));
                              return;
                            }
                            if (opt.key === "v" || opt.value === "abstain") {
                              markVague(step);
                              return;
                            }
                            const next = {
                              ...form,
                              [step]: opt.value,
                            } as FormState;
                            setForm(next);
                            advanceAfter(step, next);
                          }}
                        >
                          {opt.label}
                        </Chip>
                      );
                    })}
                  </div>
                ) : null}

                {step === "turn_kind" && !revealed ? (
                  <div className="mt-3 flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => setStep("user_stance")}
                      className="rounded-control border border-border bg-muted px-3 py-1.5 text-[12px] text-foreground hover:bg-popover"
                    >
                      Continue <kbd className="ml-1">↵</kbd>
                    </button>
                    <span className="text-[11px] text-faint-foreground">
                      selected:{" "}
                      {form.turn_kind.length
                        ? form.turn_kind.join(", ")
                        : "(none yet — ok to continue)"}
                    </span>
                  </div>
                ) : null}

                {error ? (
                  <div
                    className="mt-3 rounded-control border px-3 py-2 text-[12px]"
                    style={{
                      color: "var(--status-error)",
                      borderColor: "var(--status-error)",
                    }}
                    role="alert"
                  >
                    <p>{error}</p>
                    <button
                      type="button"
                      onClick={retrySave}
                      disabled={saveMut.isPending}
                      className="mt-2 rounded-control border border-border bg-muted px-3 py-1.5 text-[12px] text-foreground hover:bg-popover disabled:opacity-40"
                    >
                      {saveMut.isPending ? "Retrying…" : "Retry save"}
                    </button>
                  </div>
                ) : null}
                {saveMut.isPending && !error ? (
                  <p className="mt-2 text-[12px] text-muted-foreground">Saving…</p>
                ) : null}

                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {revealed ? (
                    <button
                      type="button"
                      disabled={atEnd}
                      onClick={() => go(1)}
                      className="rounded-control border border-border bg-muted px-3 py-1.5 text-[12px] text-foreground hover:bg-popover disabled:opacity-40"
                    >
                      Next <kbd className="ml-1">↵</kbd>
                    </button>
                  ) : null}
                  <button
                    type="button"
                    disabled={atEnd}
                    onClick={() => go(1)}
                    className="rounded-control border border-border px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40"
                  >
                    Skip
                  </button>
                  <button
                    type="button"
                    disabled={atStart}
                    onClick={() => go(-1)}
                    className="rounded-control border border-border px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-40"
                  >
                    Prev
                  </button>
                  <button
                    type="button"
                    onClick={jumpNextUnlabeled}
                    className="rounded-control border border-border px-3 py-1.5 text-[12px] text-muted-foreground hover:text-foreground"
                  >
                    Next unlabeled <kbd className="ml-1">]</kbd>
                  </button>
                  <form
                    className="ml-auto flex items-center gap-1.5"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const fd = new FormData(e.currentTarget);
                      jumpToPosition(String(fd.get("pos") || position));
                    }}
                  >
                    <span className="text-[11px] text-faint-foreground">go to</span>
                    <input
                      name="pos"
                      key={position}
                      defaultValue={String(position)}
                      inputMode="numeric"
                      className="tabular w-14 rounded-control border border-border bg-background px-2 py-1 text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                      aria-label="Jump to window position"
                    />
                    <button
                      type="submit"
                      className="rounded-control border border-border px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground"
                    >
                      Jump
                    </button>
                  </form>
                </div>
              </Card>

              {revealed && savedHuman ? (
                <Card>
                  <div className="mb-2 flex items-baseline justify-between">
                    <CardTitle>Reveal — human vs LLM</CardTitle>
                    {(() => {
                      const triage =
                        savedHuman.triage ?? triageFromNotes(savedHuman.notes);
                      if (triage === "no" || triage === "unclear") {
                        return (
                          <span className="text-[12px] text-faint-foreground">
                            skipped — {triage === "no" ? "not a human turn" : "unclear speaker"}
                          </span>
                        );
                      }
                      if (!revealedLlm) {
                        return (
                          <span className="text-[12px] text-faint-foreground">
                            no LLM label in DB
                          </span>
                        );
                      }
                      const agree = labelsEqual(savedHuman, revealedLlm);
                      return (
                        <span
                          className="text-[12px]"
                          style={{
                            color: agree
                              ? "var(--status-ok)"
                              : "var(--status-error)",
                          }}
                        >
                          {agree ? "agree" : "disagree"}
                        </span>
                      );
                    })()}
                  </div>
                  {revealedLlm ? (
                    <>
                      <div className="grid grid-cols-[110px_1fr_1fr] gap-2 pb-1 text-[10px] text-faint-foreground">
                        <div />
                        <div className="microlabel">human</div>
                        <div className="microlabel">llm</div>
                      </div>
                      {(() => {
                        const triageSkip =
                          triageFromNotes(savedHuman.notes) === "no" ||
                          triageFromNotes(savedHuman.notes) === "unclear";
                        return (
                          <>
                            <CompareField
                              label="turn_kind"
                              neutral={triageSkip}
                              human={humanTurnKindDisplay(savedHuman)}
                              llm={
                                [...(revealedLlm.turn_kind || [])]
                                  .sort()
                                  .join(", ") || "(none)"
                              }
                            />
                            <CompareField
                              label="user_stance"
                              neutral={triageSkip}
                              human={
                                triageSkip
                                  ? "n/a"
                                  : (savedHuman.user_stance ?? "null")
                              }
                              llm={revealedLlm.user_stance ?? "null"}
                            />
                            <CompareField
                              label="agent_stance"
                              neutral={triageSkip}
                              human={
                                triageSkip
                                  ? "n/a"
                                  : (savedHuman.agent_stance ?? "null")
                              }
                              llm={revealedLlm.agent_stance ?? "null"}
                            />
                            <CompareField
                              label="prior_outcome"
                              neutral={triageSkip}
                              human={
                                triageSkip
                                  ? "n/a"
                                  : (savedHuman.prior_outcome ?? "null")
                              }
                              llm={revealedLlm.prior_outcome ?? "null"}
                            />
                          </>
                        );
                      })()}
                    </>
                  ) : (
                    <div className="font-mono text-[12px] text-muted-foreground">
                      triage/notes: {savedHuman.notes || "(none)"}
                      <br />
                      turn_kind: {humanTurnKindDisplay(savedHuman)}
                    </div>
                  )}
                </Card>
              ) : null}
            </>
          )}
        </div>

        <ReportPanel
          done={done}
          total={total}
          report={report.data}
          loading={report.isLoading}
        />
      </div>
    </div>
  );
}

function ReportPanel({
  done,
  total,
  report,
  loading,
}: {
  done: number;
  total: number;
  report: Awaited<ReturnType<typeof fetchAdjudicationReport>> | undefined;
  loading: boolean;
}) {
  return (
    <Card className="sticky top-0 h-fit">
      <CardTitle>Agreement</CardTitle>
      <div className="mt-2 tabular text-[13px] text-foreground">
        {done}
        <span className="text-muted-foreground"> / {total} completed</span>
      </div>
      {loading || !report ? (
        <div className="mt-3 text-[12px] text-muted-foreground">Loading report…</div>
      ) : report.insufficient_data ? (
        <p className="mt-2 text-[12px] text-faint-foreground">
          Need {report.min_required} human+LLM pairs before rates unlock
          ({report.with_llm ?? 0} paired so far; {report.adjudicated} saved).
        </p>
      ) : (
        <div className="mt-3 space-y-3">
          {(["turn_kind", "user_stance", "agent_stance", "prior_outcome"] as const).map(
            (field) => {
              const block = report.fields?.[field];
              if (!block) return null;
              const em = block.exact_match;
              const rate = em.rate == null ? "—" : `${(em.rate * 100).toFixed(0)}%`;
              return (
                <div key={field}>
                  <div className="flex items-baseline justify-between">
                    <span className="microlabel text-[10px] text-faint-foreground">
                      {field}
                    </span>
                    <span className="tabular font-mono text-[12px] text-foreground">
                      {rate}
                      <span className="text-faint-foreground">
                        {" "}
                        ({em.matches}/{em.n})
                      </span>
                    </span>
                  </div>
                  {block.confusion_pairs.slice(0, 3).map((c) => (
                    <div
                      key={`${c.human}-${c.llm}`}
                      className="mt-1 truncate font-mono text-[10px] text-muted-foreground"
                      title={`${c.human} → ${c.llm}`}
                    >
                      {c.human} → {c.llm}
                      <span className="tabular text-faint-foreground"> ×{c.count}</span>
                    </div>
                  ))}
                </div>
              );
            },
          )}
        </div>
      )}
    </Card>
  );
}
