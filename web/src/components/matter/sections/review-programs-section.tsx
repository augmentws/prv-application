"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, BookOpen, Play, Plus, RefreshCw, RotateCcw, Workflow } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { useMatter } from "@/components/matter/matter-context";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { MatterDefinitionRead, MetadataDefinitionRead, ReviewProgramActionRead, ReviewProgramCreate, ReviewProgramRead, ReviewProgramRunRead, ReviewWorkflowStageRead, ReviewWorkflowTemplateRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVE_RUN_STATUSES = new Set(["QUEUED", "RUNNING", "WAITING_FOR_USER"]);

export function ReviewProgramsSection() {
  const { clientId, matterId, permissionKeys } = useMatter();
  const canManage = permissionKeys.has("review_program.manage");
  const queryClient = useQueryClient();
  const pathname = usePathname();
  const router = useRouter();
  const searchParams = useSearchParams();
  const programs = useQuery({
    queryKey: ["review-programs", matterId],
    queryFn: () => coreApi<ReviewProgramRead[]>(`/v1/matters/${matterId}/review-programs`),
  });
  const templates = useQuery({
    queryKey: ["review-workflow-templates"],
    queryFn: () => coreApi<ReviewWorkflowTemplateRead[]>("/v1/review-workflow-templates"),
  });
  const requestedProgramId = searchParams.get("program");
  const selectedProgram = programs.data?.find((program) => program.id === requestedProgramId) ?? programs.data?.[0] ?? null;
  const runs = useQuery({
    queryKey: ["review-program-runs", matterId, selectedProgram?.id],
    queryFn: () => coreApi<ReviewProgramRunRead[]>(`/v1/matters/${matterId}/review-programs/${selectedProgram!.id}/runs`),
    enabled: Boolean(selectedProgram),
    refetchInterval: (query) => query.state.data?.some((run) => ACTIVE_RUN_STATUSES.has(run.status)) ? 5_000 : false,
  });
  const currentRun = runs.data?.find((run) => run.id === selectedProgram?.current_run_id) ?? runs.data?.[0] ?? null;
  const template = templates.data?.find((item) => item.key === selectedProgram?.template_key) ?? null;

  useEffect(() => {
    if (!requestedProgramId && selectedProgram) {
      const params = new URLSearchParams(searchParams.toString());
      params.set("program", selectedProgram.id);
      router.replace(`${pathname}?${params.toString()}`, { scroll: false });
    }
  }, [pathname, requestedProgramId, router, searchParams, selectedProgram]);

  function selectProgram(programId: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("program", programId);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  async function refresh(programId: string) {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["review-programs", matterId] }),
      queryClient.invalidateQueries({ queryKey: ["review-program-runs", matterId, programId] }),
      queryClient.invalidateQueries({ queryKey: ["notifications"] }),
      queryClient.invalidateQueries({ queryKey: ["notification-counts"] }),
    ]);
  }

  const startRun = useMutation({
    mutationFn: (programId: string) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: crypto.randomUUID() }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The Review Program has started."); },
    onError: mutationError("The Review Program could not be started."),
  });
  const command = useMutation({
    mutationFn: ({ programId, runId, verb }: { programId: string; runId: string; verb: "cancel" | "retry" | "reconcile" }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/${verb}`, {
      method: "POST",
      body: verb === "reconcile" ? undefined : JSON.stringify({ idempotency_key: crypto.randomUUID() }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The Review Program was updated."); },
    onError: mutationError("The Review Program could not be updated."),
  });
  const resolveAction = useMutation({
    mutationFn: ({ programId, runId, action, choiceKey }: { programId: string; runId: string; action: ReviewProgramActionRead; choiceKey: string }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${action.id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ preview_hash: action.preview_hash, choice_key: choiceKey }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The decision was recorded."); },
    onError: mutationError("The decision could not be recorded."),
  });
  const setupDefinition = useMutation({
    mutationFn: ({ programId, runId, actionId, payload }: { programId: string; runId: string; actionId: string; payload: DefinitionSetupPayload }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${actionId}/definition-setup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The Matter Definition assessment has started."); },
    onError: mutationError("The Matter Definition could not be configured."),
  });
  const setupQA = useMutation({
    mutationFn: ({ programId, runId, actionId, payload }: { programId: string; runId: string; actionId: string; payload: QASetupPayload }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${actionId}/qa-setup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The Q&A task is being generated."); },
    onError: mutationError("The Q&A task could not be generated."),
  });
  const setupDiscovery = useMutation({
    mutationFn: ({ programId, runId, actionId, payload }: { programId: string; runId: string; actionId: string; payload: DiscoverySetupPayload }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${actionId}/discovery-setup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("Discovery Q&A has started."); },
    onError: mutationError("Discovery Q&A could not be started."),
  });

  if (programs.isPending || templates.isPending) return <TableLoading />;
  if (programs.error || templates.error) return <QueryError message={programs.error?.message ?? templates.error?.message} />;

  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">Review programs</h2><p className="text-sm text-muted-foreground">Guided, reusable review workflows that coordinate matter setup, analysis tasks, decisions, and quality control.</p></div>
      {canManage ? <CreateReviewProgramDialog matterId={matterId} onCreated={async (program) => { await refresh(program.id); selectProgram(program.id); }} /> : null}
    </div>
    {!programs.data.length ? <Card><CardContent className="grid min-h-48 place-items-center py-8 text-center"><div><Workflow className="mx-auto mb-3 size-8 text-muted-foreground" /><p className="font-semibold">No Review Programs yet</p><p className="mt-1 max-w-md text-sm text-muted-foreground">Create a First Pass Review program to begin a guided workflow. Multiple programs can use different guidance and fields in the same matter.</p></div></CardContent></Card> : <div className="grid gap-5 lg:grid-cols-[minmax(15rem,0.34fr)_minmax(0,1fr)]">
      <div className="space-y-2" aria-label="Review Programs">
        {programs.data.map((program) => <button key={program.id} type="button" onClick={() => selectProgram(program.id)} className={cn("w-full rounded-xl border bg-card p-4 text-left transition hover:border-primary/40", program.id === selectedProgram?.id && "border-primary ring-2 ring-primary/10")}>
          <div className="flex items-start justify-between gap-2"><span className="font-semibold">{program.name}</span><StatusBadge status={program.status} /></div>
          <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">{program.description || "First Pass Review"}</p>
          <p className="mt-3 text-xs text-muted-foreground">Version {program.current_version?.version ?? 1} · created {formatDate(program.created_at)}</p>
        </button>)}
      </div>
      {selectedProgram ? <ProgramWorkspace
        clientId={clientId}
        matterId={matterId}
        program={selectedProgram}
        run={currentRun}
        stages={template?.stages ?? []}
        canManage={canManage}
        loading={runs.isPending}
        error={runs.error}
        busy={startRun.isPending || command.isPending || resolveAction.isPending || setupDefinition.isPending || setupQA.isPending || setupDiscovery.isPending}
        onStart={() => startRun.mutate(selectedProgram.id)}
        onCommand={(verb) => currentRun && command.mutate({ programId: selectedProgram.id, runId: currentRun.id, verb })}
        onResolve={(action, choiceKey) => currentRun && resolveAction.mutate({ programId: selectedProgram.id, runId: currentRun.id, action, choiceKey })}
        onSetupDefinition={(action, payload) => currentRun && setupDefinition.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupQA={(action, payload) => currentRun && setupQA.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupDiscovery={(action, payload) => currentRun && setupDiscovery.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
      /> : null}
    </div>}
  </div>;
}

function ProgramWorkspace({ clientId, matterId, program, run, stages, canManage, loading, error, busy, onStart, onCommand, onResolve, onSetupDefinition, onSetupQA, onSetupDiscovery }: {
  clientId: string;
  matterId: string;
  program: ReviewProgramRead;
  run: ReviewProgramRunRead | null;
  stages: ReviewWorkflowStageRead[];
  canManage: boolean;
  loading: boolean;
  error: Error | null;
  busy: boolean;
  onStart: () => void;
  onCommand: (verb: "cancel" | "retry" | "reconcile") => void;
  onResolve: (action: ReviewProgramActionRead, choiceKey: string) => void;
  onSetupDefinition: (action: ReviewProgramActionRead, payload: DefinitionSetupPayload) => void;
  onSetupQA: (action: ReviewProgramActionRead, payload: QASetupPayload) => void;
  onSetupDiscovery: (action: ReviewProgramActionRead, payload: DiscoverySetupPayload) => void;
}) {
  const openAction = run?.actions?.find((action) => action.status === "OPEN") ?? null;
  const attemptsByStage = useMemo(() => new Map(run?.stage_attempts?.map((attempt) => [attempt.stage_key, attempt]) ?? []), [run?.stage_attempts]);
  if (loading) return <TableLoading />;
  if (error) return <QueryError message={error.message} />;
  return <div className="space-y-4">
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>{program.name}</CardTitle><CardDescription>{program.description || "Guided First Pass Review workflow"}</CardDescription></div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2">
          {canManage && (!run || ["COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"].includes(run.status) || run.status === "FAILED" && !run.failure_retriable) ? <Button size="sm" disabled={busy} onClick={onStart}><Play />Start run</Button> : null}
          {run && canManage && ACTIVE_RUN_STATUSES.has(run.status) ? <><Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("reconcile")}><RefreshCw />Reconcile</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("cancel")}><Ban />Cancel</Button></> : null}
          {run?.status === "FAILED" && run.failure_retriable && canManage ? <Button size="sm" disabled={busy} onClick={() => onCommand("retry")}><RotateCcw />Retry</Button> : null}
        </div>
      </CardHeader>
      {run ? <CardContent><div className="flex flex-wrap items-center gap-2"><StatusBadge status={run.status} /><Badge variant="outline">{friendly(run.current_stage)}</Badge><span className="text-xs text-muted-foreground">Started {formatDate(run.created_at)}</span></div>{run.failure_summary ? <p className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{run.failure_summary}</p> : null}</CardContent> : <CardContent><p className="text-sm text-muted-foreground">This program is configured but has not been started.</p></CardContent>}
    </Card>
    {openAction?.action_type === "MATTER_DEFINITION_SETUP" ? <DefinitionSetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDefinition(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "QA_TASK_SETUP" ? <QASetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupQA(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "DISCOVERY_QA_SETUP" ? <DiscoverySetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDiscovery(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction ? <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{openAction.title}</CardTitle><CardDescription>{openAction.explanation}</CardDescription></CardHeader><CardContent><DefinitionAssessmentSummary action={openAction} /><QAPublicationSummary action={openAction} /><IssueSupportSummary action={openAction} />{openAction.stage_key === "DEFINITION_REFINEMENT" || openAction.stage_key === "DEFINITION_ACCEPTANCE" ? <Button asChild size="sm" variant="outline" className="mb-3"><Link href={`/app/clients/${clientId}/matters/${matterId}/definition`}><BookOpen />Open Matter Definition</Link></Button> : null}<div className="flex flex-wrap gap-2">{openAction.allowed_choices.map((choice, index) => {
      return <Button key={`${choice.key}-${index}`} size="sm" variant={choice.key === "STOP" ? "destructive" : "default"} disabled={!canManage || busy} onClick={() => onResolve(openAction, choice.key)} title={choice.consequence}>{choice.label}</Button>;
    })}</div></CardContent></Card> : null}
    {run ? <Card><CardHeader><CardTitle>Program stages</CardTitle><CardDescription>The coordinator records every stage attempt and waits only at explicit human gates.</CardDescription></CardHeader><CardContent><ol className="grid gap-2 sm:grid-cols-2">{stages.map((stage) => {
      const attempt = attemptsByStage.get(stage.key);
      const upcoming = stage.ordinal > run.current_stage_ordinal;
      return <li key={stage.key} className={cn("flex items-start gap-3 rounded-lg border p-3", stage.key === run.current_stage && "border-primary/50 bg-primary/5", upcoming && "opacity-55")}><span className="grid size-7 shrink-0 place-items-center rounded-full bg-muted text-xs font-semibold">{stage.ordinal}</span><div className="min-w-0"><p className="text-sm font-medium">{stage.name}</p><div className="mt-1 flex flex-wrap gap-1">{attempt ? <StatusBadge status={attempt.status} /> : <Badge variant="outline">Upcoming</Badge>}<Badge variant="outline">{friendly(stage.batching_policy)}</Badge>{stage.requires_human_gate ? <Badge variant="outline">Decision</Badge> : null}</div></div></li>;
    })}</ol></CardContent></Card> : null}
  </div>;
}

function DefinitionAssessmentSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "DEFINITION_REFINEMENT" && action.stage_key !== "DEFINITION_ACCEPTANCE") return null;
  const assessment = action.decision_context?.assessment;
  if (!assessment || typeof assessment !== "object" || Array.isArray(assessment)) return null;
  const values = assessment as Record<string, unknown>;
  const questions = values.question_counts && typeof values.question_counts === "object" && !Array.isArray(values.question_counts)
    ? values.question_counts as Record<string, unknown>
    : {};
  return <div className="mb-3 grid gap-2 rounded-lg border bg-background/70 p-3 text-sm sm:grid-cols-2 lg:grid-cols-4"><div><p className="text-xs text-muted-foreground">Assessment</p><p className="font-medium">{friendly(String(values.status ?? "unknown"))}</p></div><div><p className="text-xs text-muted-foreground">Documents</p><p className="font-medium">{Number(values.summarized_count ?? 0)} summarized · {Number(values.failed_count ?? 0)} failed</p></div><div><p className="text-xs text-muted-foreground">Open questions</p><p className="font-medium">{Number(questions.open ?? 0)} open · {Number(questions.open_blocking ?? 0)} blocking</p></div><div><p className="text-xs text-muted-foreground">Refinement</p><p className="font-medium">{friendly(String(values.guidance_refinement_status ?? "not ready"))}</p></div></div>;
}

function QAPublicationSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.action_type !== "QA_TASK_PUBLICATION") return null;
  const task = action.decision_context?.qa_task;
  const compiler = action.decision_context?.compiler;
  const profiles = action.decision_context?.execution_profiles;
  const taskValues = task && typeof task === "object" && !Array.isArray(task) ? task as Record<string, unknown> : {};
  const compilerValues = compiler && typeof compiler === "object" && !Array.isArray(compiler) ? compiler as Record<string, unknown> : {};
  const progress = compilerValues.progress && typeof compilerValues.progress === "object" && !Array.isArray(compilerValues.progress) ? compilerValues.progress as Record<string, unknown> : {};
  const profileValues = Array.isArray(profiles) ? profiles.filter((profile): profile is Record<string, unknown> => Boolean(profile && typeof profile === "object" && !Array.isArray(profile))) : [];
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm"><div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Task</p><p className="font-medium">{String(taskValues.name ?? "Generated Q&A task")}</p></div><div><p className="text-xs text-muted-foreground">Questions</p><p className="font-medium">{Number(progress.question_count ?? 0)} questions · {Number(progress.recommendation_count ?? 0)} recommendations</p></div><div><p className="text-xs text-muted-foreground">Validation</p><p className="font-medium">{Number(progress.warning_count ?? 0)} warnings · {Number(progress.omission_count ?? 0)} omissions</p></div></div><div><p className="mb-1 text-xs text-muted-foreground">Pinned execution profiles</p><div className="flex flex-wrap gap-2">{profileValues.map((profile) => <Badge key={String(profile.id)} variant="outline">{String(profile.name)} · {String(profile.resolved_model)}</Badge>)}</div></div></div>;
}

function IssueSupportSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "ISSUE_SUPPORT_DECISION") return null;
  const raw = action.decision_context?.discovery_analysis;
  const analysis = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const overallRaw = analysis.overall;
  const overall = overallRaw && typeof overallRaw === "object" && !Array.isArray(overallRaw) ? overallRaw as Record<string, unknown> : {};
  const lowSupport = Array.isArray(analysis.low_support) ? analysis.low_support : [];
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm"><div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Support</p><p className="font-medium">{friendly(String(analysis.support_status ?? "unknown"))}</p></div><div><p className="text-xs text-muted-foreground">Exact</p><p className="font-medium">{Number(overall.exact_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Differences</p><p className="font-medium">{Number(overall.partial_count ?? 0) + Number(overall.disagreement_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Low-support categories</p><p className="font-medium">{lowSupport.length}</p></div></div>{lowSupport.length ? <p className="text-xs text-muted-foreground">Low-support categories remain visible in the frozen analysis and require an explicit decision before classifier training.</p> : null}</div>;
}

interface DiscoverySetupPayload {
  preview_hash: string;
  target_document_count: number;
  control_sample_size: number;
  target_batch_size: number;
  keep_families_together: boolean;
}

function DiscoverySetupCard({ action, canManage, busy, onSubmit, onStop }: {
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (payload: DiscoverySetupPayload) => void;
  onStop: () => void;
}) {
  const defaultsRaw = action.decision_context?.defaults;
  const defaults = defaultsRaw && typeof defaultsRaw === "object" && !Array.isArray(defaultsRaw) ? defaultsRaw as Record<string, unknown> : {};
  const planRaw = action.decision_context?.retrieval_plan;
  const plan = planRaw && typeof planRaw === "object" && !Array.isArray(planRaw) ? planRaw as Record<string, unknown> : {};
  const searchRaw = action.decision_context?.search_index_generation;
  const search = searchRaw && typeof searchRaw === "object" && !Array.isArray(searchRaw) ? searchRaw as Record<string, unknown> : {};
  const [targetCount, setTargetCount] = useState(String(defaults.target_document_count ?? 500));
  const [controlCount, setControlCount] = useState(String(defaults.control_sample_size ?? 50));
  const [batchSize, setBatchSize] = useState(String(defaults.target_batch_size ?? 100));
  const [keepFamilies, setKeepFamilies] = useState(Boolean(defaults.keep_families_together ?? true));
  function submit(event: FormEvent) {
    event.preventDefault();
    const target = Number(targetCount);
    const control = Number(controlCount);
    const size = Number(batchSize);
    if (![target, control, size].every(Number.isInteger) || target < 1 || control < 0 || control > target || size < 1) return;
    onSubmit({ preview_hash: action.preview_hash, target_document_count: target, control_sample_size: control, target_batch_size: size, keep_families_together: keepFamilies });
  }
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><div className="grid gap-2 rounded-lg border bg-background/70 p-3 text-sm sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Retrieval queries</p><p className="font-medium">{Number(plan.query_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Pinned index</p><p className="font-medium">Generation {String(search.generation ?? "unavailable")}</p></div><div><p className="text-xs text-muted-foreground">Plan source</p><p className="font-medium">Accepted assessment</p></div></div><div className="grid gap-4 sm:grid-cols-3"><div className="space-y-2"><Label htmlFor="discovery-target">Document target</Label><Input id="discovery-target" type="number" min={1} max={100000} value={targetCount} onChange={(event) => setTargetCount(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="discovery-control">Broad control</Label><Input id="discovery-control" type="number" min={0} max={25000} value={controlCount} onChange={(event) => setControlCount(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="discovery-batch-size">Documents per batch</Label><Input id="discovery-batch-size" type="number" min={1} max={10000} value={batchSize} onChange={(event) => setBatchSize(event.target.value)} /></div></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => setKeepFamilies(event.target.checked)} disabled={!canManage || busy} />Keep document families together</label><p className="text-xs text-muted-foreground">The same immutable population and managed child batches will be used for both pinned model profiles. Results remain isolated from matter metadata.</p><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy}>{busy ? "Starting…" : "Start discovery Q&A"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

interface QASetupPayload {
  preview_hash: string;
  task_key: string;
  name: string;
  description?: string;
  destination_metadata_definition_ids: string[];
}

function QASetupCard({ matterId, action, canManage, busy, onSubmit, onStop }: {
  matterId: string;
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (payload: QASetupPayload) => void;
  onStop: () => void;
}) {
  const [name, setName] = useState("First-pass Q&A");
  const [key, setKey] = useState("first_pass_qa");
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState("Generated from the accepted Matter Definition.");
  const [selected, setSelected] = useState<string[]>([]);
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
  });
  const assignable = definitions.data?.filter((definition) => definition.status === "ACTIVE" && definition.value_source === "ASSERTED" && definition.ai_assignable) ?? [];
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim() || !key || !selected.length) return;
    onSubmit({
      preview_hash: action.preview_hash,
      task_key: key,
      name: name.trim(),
      description: description.trim() || undefined,
      destination_metadata_definition_ids: selected,
    });
  }
  function toggle(id: string, checked: boolean) {
    setSelected((current) => checked ? [...current, id] : current.filter((value) => value !== id));
  }
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="qa-task-name">Task name</Label><Input id="qa-task-name" value={name} onChange={(event) => { setName(event.target.value); if (!keyEdited) setKey(slug(event.target.value).replaceAll("-", "_")); }} required maxLength={200} /></div><div className="space-y-2"><Label htmlFor="qa-task-key">Task key</Label><Input id="qa-task-key" value={key} onChange={(event) => { setKeyEdited(true); setKey(slug(event.target.value).replaceAll("-", "_")); }} required pattern="[a-z][a-z0-9_]*" maxLength={100} /></div></div><div className="space-y-2"><Label htmlFor="qa-task-description">Description</Label><Textarea id="qa-task-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={10000} /></div><fieldset className="space-y-2"><legend className="text-sm font-medium">Coding fields</legend><p className="text-xs text-muted-foreground">Select the issue and responsiveness fields this task may write. The compiler must map only these fields.</p>{definitions.isPending ? <p className="text-sm text-muted-foreground">Loading coding fields…</p> : definitions.error ? <p className="text-sm text-destructive">{definitions.error.message}</p> : assignable.length ? <div className="grid gap-2 sm:grid-cols-2">{assignable.map((definition) => <label key={definition.id} className="flex items-start gap-2 rounded-lg border bg-background p-3 text-sm"><input className="mt-1" type="checkbox" checked={selected.includes(definition.id)} onChange={(event) => toggle(definition.id, event.target.checked)} disabled={!canManage || busy} /><span><span className="block font-medium">{definition.display_name}</span><span className="text-xs text-muted-foreground">{definition.key} · {friendly(definition.type)}</span></span></label>)}</div> : <p className="rounded-lg border p-3 text-sm text-muted-foreground">No active, asserted, AI-assignable coding fields are available. Configure fields in Matter metadata first.</p>}</fieldset><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy || definitions.isPending || !name.trim() || !key || !selected.length}>{busy ? "Generating…" : "Generate Q&A task"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

type DefinitionSetupMode = "CREATE" | "CLONE" | "ADOPT";

interface DefinitionSetupPayload {
  preview_hash: string;
  mode: DefinitionSetupMode;
  existing_definition_id?: string;
  definition_key?: string;
  name?: string;
  description?: string;
  content_markdown?: string;
  assessment_target_document_count: number;
  assessment_control_sample_size: number;
  assessment_use_batching: boolean;
  acknowledge_large_run_warning: boolean;
}

function DefinitionSetupCard({ matterId, action, canManage, busy, onSubmit, onStop }: {
  matterId: string;
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (payload: DefinitionSetupPayload) => void;
  onStop: () => void;
}) {
  const [mode, setMode] = useState<DefinitionSetupMode>("CREATE");
  const [existingId, setExistingId] = useState("");
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [targetCount, setTargetCount] = useState("500");
  const [controlCount, setControlCount] = useState("0");
  const [useBatching, setUseBatching] = useState(true);
  const [acknowledgeLargeRun, setAcknowledgeLargeRun] = useState(false);
  const definitions = useQuery({
    queryKey: ["matter-definitions", matterId],
    queryFn: () => coreApi<MatterDefinitionRead[]>(`/v1/matters/${matterId}/guidance`),
  });
  const selectable = definitions.data?.filter((definition) => definition.status === "ACTIVE" && definition.published_revision !== null) ?? [];
  const valid = mode === "CREATE"
    ? Boolean(name.trim() && key && content.trim())
    : Boolean(existingId && (mode === "ADOPT" || name.trim() && key));

  function submit(event: FormEvent) {
    event.preventDefault();
    const target = Number(targetCount);
    const control = Number(controlCount);
    if (!Number.isInteger(target) || target < 1 || !Number.isInteger(control) || control < 0 || control > target) return;
    onSubmit({
      preview_hash: action.preview_hash,
      mode,
      ...(mode !== "CREATE" ? { existing_definition_id: existingId } : {}),
      ...(mode !== "ADOPT" ? { definition_key: key, name, description: description || undefined } : {}),
      ...(mode === "CREATE" ? { content_markdown: content } : {}),
      assessment_target_document_count: target,
      assessment_control_sample_size: control,
      assessment_use_batching: useBatching,
      acknowledge_large_run_warning: acknowledgeLargeRun,
    });
  }

  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="definition-setup-mode">Starting point</Label><Select value={mode} onValueChange={(value) => setMode(value as DefinitionSetupMode)} disabled={!canManage || busy}><SelectTrigger id="definition-setup-mode"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="CREATE">Create a new definition</SelectItem><SelectItem value="CLONE">Clone an existing definition</SelectItem><SelectItem value="ADOPT">Adopt an existing definition</SelectItem></SelectContent></Select></div>{mode !== "CREATE" ? <div className="space-y-2"><Label htmlFor="definition-setup-existing">Published definition</Label><Select value={existingId} onValueChange={setExistingId} disabled={!canManage || busy || definitions.isPending}><SelectTrigger id="definition-setup-existing"><SelectValue placeholder={definitions.isPending ? "Loading…" : "Select a definition"} /></SelectTrigger><SelectContent>{selectable.map((definition) => <SelectItem key={definition.id} value={definition.id}>{definition.name} · revision {definition.published_revision}</SelectItem>)}</SelectContent></Select>{definitions.error ? <p className="text-xs text-destructive">{definitions.error.message}</p> : null}</div> : null}</div>{mode !== "ADOPT" ? <div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="definition-setup-name">Definition name</Label><Input id="definition-setup-name" value={name} onChange={(event) => { setName(event.target.value); if (!keyEdited) setKey(slug(event.target.value)); }} required maxLength={200} /></div><div className="space-y-2"><Label htmlFor="definition-setup-key">Definition key</Label><Input id="definition-setup-key" value={key} onChange={(event) => { setKeyEdited(true); setKey(slug(event.target.value)); }} required minLength={1} maxLength={100} pattern="[a-z][a-z0-9_-]*" /></div></div> : null}{mode === "CREATE" ? <div className="space-y-2"><Label htmlFor="definition-setup-content">Matter Definition</Label><Textarea id="definition-setup-content" className="min-h-48" value={content} onChange={(event) => setContent(event.target.value)} required placeholder="Describe responsiveness, issues, exclusions, and coding guidance." /></div> : null}{mode !== "ADOPT" ? <div className="space-y-2"><Label htmlFor="definition-setup-description">Description</Label><Textarea id="definition-setup-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={10000} /></div> : null}<div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="definition-assessment-target">Assessment document target</Label><Input id="definition-assessment-target" type="number" min={1} value={targetCount} onChange={(event) => setTargetCount(event.target.value)} required /></div><div className="space-y-2"><Label htmlFor="definition-assessment-control">Control sample</Label><Input id="definition-assessment-control" type="number" min={0} value={controlCount} onChange={(event) => setControlCount(event.target.value)} required /></div></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={useBatching} onChange={(event) => setUseBatching(event.target.checked)} disabled={!canManage || busy} />Use provider batching for this ad hoc assessment</label><label className="flex items-start gap-2 text-sm"><input className="mt-1" type="checkbox" checked={acknowledgeLargeRun} onChange={(event) => setAcknowledgeLargeRun(event.target.checked)} disabled={!canManage || busy} /><span>Acknowledge the additional cost and processing time if this target exceeds the configured large-assessment limit.</span></label><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy || !valid}>{busy ? "Starting…" : "Save and assess"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

function CreateReviewProgramDialog({ matterId, onCreated }: { matterId: string; onCreated: (program: ReviewProgramRead) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [keyEdited, setKeyEdited] = useState(false);
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: (payload: ReviewProgramCreate) => coreApi<ReviewProgramRead>(`/v1/matters/${matterId}/review-programs`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: async (program) => { await onCreated(program); setOpen(false); setName(""); setKey(""); setKeyEdited(false); setDescription(""); toast.success(`${program.name} was created.`); },
    onError: mutationError("The Review Program could not be created."),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate({ key, name, description: description || null });
  }
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Plus />New Review Program</Button></DialogTrigger><DialogContent><form onSubmit={submit}><DialogHeader><DialogTitle>Create Review Program</DialogTitle><DialogDescription>Start a reusable First Pass Review workflow. This creates the program workspace; it does not launch analysis work yet.</DialogDescription></DialogHeader><div className="space-y-4"><div className="space-y-2"><Label htmlFor="review-program-name">Name</Label><Input id="review-program-name" value={name} onChange={(event) => { setName(event.target.value); if (!keyEdited) setKey(slug(event.target.value)); }} required maxLength={200} placeholder="Standard first pass" /></div><div className="space-y-2"><Label htmlFor="review-program-key">Key</Label><Input id="review-program-key" value={key} onChange={(event) => { setKeyEdited(true); setKey(slug(event.target.value)); }} required minLength={3} maxLength={100} pattern="[a-z][a-z0-9_-]+[a-z0-9]" placeholder="standard-first-pass" /><p className="text-xs text-muted-foreground">Stable within this matter; lowercase letters, numbers, hyphens, and underscores.</p></div><div className="space-y-2"><Label htmlFor="review-program-description">Description</Label><Textarea id="review-program-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={4000} placeholder="Guidance, fields, or scope that distinguishes this program." /></div></div><DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={create.isPending || !name.trim() || !key}>{create.isPending ? "Creating…" : "Create program"}</Button></DialogFooter></form></DialogContent></Dialog>;
}

function mutationError(fallback: string) {
  return (error: Error) => toast.error(error.message || fallback);
}

function friendly(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function slug(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100);
}
