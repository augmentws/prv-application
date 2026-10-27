"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, BookOpen, CircleHelp, Play, Plus, RefreshCw, RotateCcw, Workflow } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
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
import type { MatterDefinitionAssessmentRead, MatterDefinitionRead, MetadataDefinitionRead, ReviewProgramActionRead, ReviewProgramCreate, ReviewProgramRead, ReviewProgramRunRead, ReviewWorkflowStageRead, ReviewWorkflowTemplateRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

const ACTIVE_RUN_STATUSES = new Set(["QUEUED", "RUNNING", "WAITING_FOR_USER"]);

const PROGRAM_STAGE_HELP: Record<string, string> = {
  MATTER_DEFINITION: "Define the review scope, issues, responsive criteria, and guidance that control the rest of the program.",
  DEFINITION_ASSESSMENT: "Test the Matter Definition against a targeted document batch and identify gaps, ambiguities, and retrieval weaknesses.",
  DEFINITION_REFINEMENT: "Resolve assessment questions, review the generated refinements, and publish the next Matter Definition revision.",
  DEFINITION_ACCEPTANCE: "Freeze the accepted Matter Definition revision and its assessment provenance for downstream Q&A work.",
  QA_TASK_SETUP: "Generate and publish the structured Q&A coding task, including its questions, destination fields, and model profiles.",
  DISCOVERY_QA: "Run both Q&A model profiles on discovery batches to produce initial issue coding examples.",
  DISCOVERY_ANALYSIS: "Compare the Q&A results, measure model agreement, and determine the amount of training support for each issue.",
  ISSUE_SUPPORT_DECISION: "Decide how to handle issues that do not yet have enough reliable examples for classifier training.",
  DISAGREEMENT_REVIEW: "Create an ad hoc review batch for model disagreements and incorporate the reviewer’s decisions.",
  TRAINING_LABEL_FINALIZATION: "Choose the accepted coding source and freeze the labels that will be used to train the classifier.",
  CLASSIFIER_TRAINING: "Train and evaluate the issue classifier using the finalized Q&A and reviewer labels.",
  CLASSIFIER_REVIEW: "Review classifier quality by issue and decide whether to accept the model or improve weak categories.",
  CLASSIFIER_IMPROVEMENT: "Retrieve and review additional targeted documents for weak categories, then retrain the classifier.",
  WHOLE_COLLECTION_SCORING: "Apply the accepted classifier across the entire matter collection.",
  CLASSIFIER_POSITIVE_COVERAGE: "Run Q&A review on classifier-positive documents that have not already received Q&A coding.",
  FINAL_ALIGNMENT: "Compare the final Q&A and classifier codings and summarize agreement, differences, and coverage.",
  NO_CODE_QC: "Sample documents with no issue codes to check the negative population for missed responsive material.",
  COMPLETION: "Freeze the final reports and provenance, record completion, and close the First Pass Review run.",
};

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
  const carriedForwardAcceptanceRuns = useRef(new Set<string>());

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
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("The guided setup is ready. Configure the Matter Definition to continue."); },
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
  const resetDiscoveryQA = useMutation({
    mutationFn: ({ programId, runId }: { programId: string; runId: string }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/reset-discovery-qa`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: crypto.randomUUID(), reason: "Reset from the Review Program workspace" }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("Discovery Q&A was reset. You can now start it again."); },
    onError: mutationError("Discovery Q&A could not be reset."),
  });
  const resolveAction = useMutation({
    mutationFn: ({ programId, runId, action, choiceKey, reason, input }: { programId: string; runId: string; action: ReviewProgramActionRead; choiceKey: string; reason?: string; input?: Record<string, unknown> }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${action.id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ preview_hash: action.preview_hash, choice_key: choiceKey, ...(reason ? { reason } : {}), ...(input ? { input } : {}) }),
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
  const setupClassifierImprovement = useMutation({
    mutationFn: ({ programId, runId, actionId, payload }: { programId: string; runId: string; actionId: string; payload: ClassifierImprovementSetupPayload }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${actionId}/classifier-improvement-setup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("Targeted classifier review has started."); },
    onError: mutationError("Targeted classifier review could not be started."),
  });
  const setupNegativePopulationQC = useMutation({
    mutationFn: ({ programId, runId, actionId, payload }: { programId: string; runId: string; actionId: string; payload: NegativePopulationQCSetupPayload }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${actionId}/negative-population-qc-setup`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("Negative-population QC has started."); },
    onError: mutationError("Negative-population QC could not be started."),
  });

  useEffect(() => {
    const hasRecordedFinalAcceptance = Boolean(currentRun?.progress_summary?.final_unassessed_refinement);
    if (
      !canManage
      || !selectedProgram
      || !currentRun
      || currentRun.status !== "WAITING_FOR_USER"
      || currentRun.current_stage !== "DEFINITION_ACCEPTANCE"
      || !hasRecordedFinalAcceptance
      || carriedForwardAcceptanceRuns.current.has(currentRun.id)
    ) return;
    carriedForwardAcceptanceRuns.current.add(currentRun.id);
    command.mutate({ programId: selectedProgram.id, runId: currentRun.id, verb: "reconcile" });
  }, [canManage, command, currentRun, selectedProgram]);

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
        busy={startRun.isPending || command.isPending || resetDiscoveryQA.isPending || resolveAction.isPending || setupDefinition.isPending || setupQA.isPending || setupDiscovery.isPending || setupClassifierImprovement.isPending || setupNegativePopulationQC.isPending}
        onStart={() => startRun.mutate(selectedProgram.id)}
        onCommand={(verb) => currentRun && command.mutate({ programId: selectedProgram.id, runId: currentRun.id, verb })}
        onResetDiscovery={() => currentRun && resetDiscoveryQA.mutate({ programId: selectedProgram.id, runId: currentRun.id })}
        onResolve={(action, choiceKey, reason, input) => currentRun && resolveAction.mutate({ programId: selectedProgram.id, runId: currentRun.id, action, choiceKey, reason, input })}
        onSetupDefinition={(action, payload) => currentRun && setupDefinition.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupQA={(action, payload) => currentRun && setupQA.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupDiscovery={(action, payload) => currentRun && setupDiscovery.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupClassifierImprovement={(action, payload) => currentRun && setupClassifierImprovement.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupNegativePopulationQC={(action, payload) => currentRun && setupNegativePopulationQC.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
      /> : null}
    </div>}
  </div>;
}

function ProgramWorkspace({ clientId, matterId, program, run, stages, canManage, loading, error, busy, onStart, onCommand, onResetDiscovery, onResolve, onSetupDefinition, onSetupQA, onSetupDiscovery, onSetupClassifierImprovement, onSetupNegativePopulationQC }: {
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
  onResetDiscovery: () => void;
  onResolve: (action: ReviewProgramActionRead, choiceKey: string, reason?: string, input?: Record<string, unknown>) => void;
  onSetupDefinition: (action: ReviewProgramActionRead, payload: DefinitionSetupPayload) => void;
  onSetupQA: (action: ReviewProgramActionRead, payload: QASetupPayload) => void;
  onSetupDiscovery: (action: ReviewProgramActionRead, payload: DiscoverySetupPayload) => void;
  onSetupClassifierImprovement: (action: ReviewProgramActionRead, payload: ClassifierImprovementSetupPayload) => void;
  onSetupNegativePopulationQC: (action: ReviewProgramActionRead, payload: NegativePopulationQCSetupPayload) => void;
}) {
  const openAction = run?.actions?.find((action) => action.status === "OPEN") ?? null;
  const attemptsByStage = useMemo(() => new Map(run?.stage_attempts?.map((attempt) => [attempt.stage_key, attempt]) ?? []), [run?.stage_attempts]);
  const definitionAssessmentRound = Number(run?.progress_summary?.definition_assessment_round ?? 1);
  const isDefinitionReassessment = run?.current_stage === "DEFINITION_ASSESSMENT" && definitionAssessmentRound > 1;
  const definitionAssessmentAttempts = run?.stage_attempts?.filter((attempt) => attempt.stage_key === "DEFINITION_ASSESSMENT") ?? [];
  const completedDefinitionAssessmentAttempt = [...definitionAssessmentAttempts]
    .reverse()
    .find((attempt) => attempt.status === "COMPLETED");
  const currentDefinitionAssessmentAttempt = definitionAssessmentAttempts[definitionAssessmentAttempts.length - 1];
  const currentAssessmentId = typeof run?.progress_summary?.current_assessment_id === "string"
    ? run.progress_summary.current_assessment_id
    : "";
  const definitionAssessments = useQuery({
    queryKey: ["definition-assessments", matterId, "review-program-progress"],
    queryFn: () => coreApi<MatterDefinitionAssessmentRead[]>(`/v1/matters/${matterId}/definition-assessments`),
    enabled: Boolean(run?.current_stage === "DEFINITION_ASSESSMENT" && currentAssessmentId),
    refetchInterval: run?.current_stage === "DEFINITION_ASSESSMENT" ? 5_000 : false,
  });
  const currentAssessment = definitionAssessments.data?.find((assessment) => assessment.id === currentAssessmentId) ?? null;
  if (loading) return <TableLoading />;
  if (error) return <QueryError message={error.message} />;
  return <div className="space-y-4">
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>{program.name}</CardTitle><CardDescription>{program.description || "Guided First Pass Review workflow"}</CardDescription></div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2">
          {canManage && run && (["COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"].includes(run.status) || run.status === "FAILED" && !run.failure_retriable) ? <Button size="sm" disabled={busy} onClick={onStart}><Play />Start another run</Button> : null}
          {run && canManage && run.current_stage === "DISCOVERY_QA" && Boolean(run.progress_summary?.discovery_qa) ? <ResetDiscoveryQADialog busy={busy} onReset={onResetDiscovery} /> : null}
          {run && canManage && ACTIVE_RUN_STATUSES.has(run.status) ? <><Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("reconcile")}><RefreshCw />Reconcile</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("cancel")}><Ban />Cancel</Button></> : null}
          {run?.status === "FAILED" && run.failure_retriable && canManage ? <Button size="sm" disabled={busy} onClick={() => onCommand("retry")}><RotateCcw />Retry</Button> : null}
        </div>
      </CardHeader>
      {run ? <CardContent><div className="flex flex-wrap items-center gap-2"><StatusBadge status={run.status} /><Badge variant="outline">{isDefinitionReassessment ? `Definition refinement · reassessment round ${definitionAssessmentRound}` : friendly(run.current_stage)}</Badge><span className="text-xs text-muted-foreground">Started {formatDate(run.created_at)}</span></div>{run.failure_summary ? <p className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{run.failure_summary}</p> : null}</CardContent> : <CardContent><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-sm font-medium">This program has not been configured yet.</p><p className="mt-1 text-sm text-muted-foreground">Begin the guided setup to create, clone, or adopt a Matter Definition. No document analysis starts until you save the definition and choose to assess it.</p></div>{canManage ? <Button size="sm" disabled={busy} onClick={onStart}><Play />Start Setup</Button> : null}</div></CardContent>}
    </Card>
    {run && currentAssessment ? <DefinitionAssessmentProgress clientId={clientId} matterId={matterId} run={run} assessment={currentAssessment} /> : null}
    {run?.status === "COMPLETED" || run?.status === "COMPLETED_WITH_ERRORS" ? <CompletedReviewProgramSummary run={run} /> : null}
    {openAction?.action_type === "MATTER_DEFINITION_SETUP" ? <DefinitionSetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDefinition(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "MATTER_DEFINITION_REFINEMENT" ? <DefinitionRefinementCard clientId={clientId} matterId={matterId} action={openAction} canManage={canManage} busy={busy} onResolve={(choice, input) => onResolve(openAction, choice, undefined, input)} /> : openAction?.action_type === "QA_TASK_SETUP" ? <QASetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupQA(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "DISCOVERY_QA_SETUP" ? <DiscoverySetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDiscovery(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "CLASSIFIER_IMPROVEMENT_SETUP" ? <ClassifierImprovementSetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupClassifierImprovement(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "NEGATIVE_POPULATION_QC_SETUP" ? <NegativePopulationQCSetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupNegativePopulationQC(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "REVIEW_PROGRAM_COMPLETION" ? <CompletionDecisionCard action={openAction} canManage={canManage} busy={busy} onComplete={(reason) => onResolve(openAction, "COMPLETE", reason)} onStop={() => onResolve(openAction, "STOP")} /> : openAction ? <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{openAction.title}</CardTitle><CardDescription>{openAction.explanation}</CardDescription></CardHeader><CardContent><DefinitionAssessmentSummary action={openAction} /><QAPublicationSummary action={openAction} /><IssueSupportSummary action={openAction} /><TrainingLabelSummary action={openAction} matterId={matterId} /><ClassifierReviewSummary action={openAction} /><WholeCollectionScoringSummary action={openAction} /><ClassifierPositiveCoverageSummary action={openAction} /><FinalAlignmentSummary action={openAction} /><NegativePopulationQCReport action={openAction} /><NegativePopulationQCFindingsReview action={openAction} matterId={matterId} />{openAction.stage_key === "DEFINITION_ACCEPTANCE" ? <Button asChild size="sm" variant="outline" className="mb-3"><Link href={definitionAssessmentHref(clientId, matterId, openAction)}><BookOpen />Open Matter Definition</Link></Button> : null}<div className="flex flex-wrap gap-2">{openAction.allowed_choices.map((choice, index) => {
      return <Button key={`${choice.key}-${index}`} size="sm" variant={choice.key === "STOP" ? "destructive" : "default"} disabled={!canManage || busy} onClick={() => onResolve(openAction, choice.key)} title={choice.consequence}>{choice.label}</Button>;
    })}</div></CardContent></Card> : null}
    {run ? <Card><CardHeader><CardTitle>Program stages</CardTitle><CardDescription>The coordinator records every stage attempt and waits only at explicit human gates.</CardDescription></CardHeader><CardContent><ol className="grid gap-2 sm:grid-cols-2">{stages.map((stage) => {
      const attempt = isDefinitionReassessment && stage.key === "DEFINITION_ASSESSMENT"
        ? completedDefinitionAssessmentAttempt
        : attemptsByStage.get(stage.key);
      const containsCurrentReassessment = isDefinitionReassessment && stage.key === "DEFINITION_REFINEMENT";
      const isCurrent = (stage.key === run.current_stage && !isDefinitionReassessment) || containsCurrentReassessment;
      const upcoming = isDefinitionReassessment ? stage.ordinal > 3 : stage.ordinal > run.current_stage_ordinal;
      return <li key={stage.key} className={cn("flex items-start gap-3 rounded-lg border p-3", isCurrent && "border-primary/50 bg-primary/5", upcoming && "opacity-55")}><span className="grid size-7 shrink-0 place-items-center rounded-full bg-muted text-xs font-semibold">{stage.ordinal}</span><div className="min-w-0 flex-1"><div className="flex items-start justify-between gap-2"><p className="text-sm font-medium">{stage.name}</p><ProgramStageTooltip stage={stage} /></div><div className="mt-1 flex flex-wrap gap-1">{attempt ? <StatusBadge status={attempt.status} /> : <Badge variant="outline">Upcoming</Badge>}<Badge variant="outline">{friendly(stage.batching_policy)}</Badge>{stage.requires_human_gate ? <Badge variant="outline">Decision</Badge> : null}</div>{containsCurrentReassessment ? <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-primary/30 bg-background/80 px-2 py-1.5"><span className="text-xs font-medium">Reassessment · Round {definitionAssessmentRound}</span><StatusBadge status={currentDefinitionAssessmentAttempt?.status ?? run.status} /></div> : null}</div></li>;
    })}</ol></CardContent></Card> : null}
  </div>;
}

function ResetDiscoveryQADialog({ busy, onReset }: { busy: boolean; onReset: () => void }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button size="sm" variant="outline" disabled={busy}><RotateCcw />Reset Discovery Q&amp;A</Button></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Reset Discovery Q&amp;A?</DialogTitle>
        <DialogDescription>This cancels active Discovery Q&amp;A processing, archives its batch set and child batches, and returns this stage to setup. Prior records remain available for audit, but their documents may be selected again.</DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => setOpen(false)}>Keep current run</Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={() => { setOpen(false); onReset(); }}>Reset Discovery Q&amp;A</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

function ProgramStageTooltip({ stage }: { stage: ReviewWorkflowStageRead }) {
  const tooltipId = `review-program-stage-${stage.ordinal}-help`;
  const description = PROGRAM_STAGE_HELP[stage.key]
    ?? `${stage.name} is a ${friendly(stage.batching_policy)} stage${stage.requires_human_gate ? " that requires a user decision" : ""}.`;
  return <span className="group/stage-tooltip relative inline-flex shrink-0">
    <button type="button" className="rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" aria-label={`About ${stage.name}`} aria-describedby={tooltipId}>
      <CircleHelp className="size-4" aria-hidden="true" />
    </button>
    <span id={tooltipId} role="tooltip" className="pointer-events-none invisible absolute right-0 top-6 z-50 w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal leading-relaxed text-popover-foreground opacity-0 shadow-md transition-opacity group-hover/stage-tooltip:visible group-hover/stage-tooltip:opacity-100 group-focus-within/stage-tooltip:visible group-focus-within/stage-tooltip:opacity-100">
      {description}
    </span>
  </span>;
}

function DefinitionAssessmentProgress({ clientId, matterId, run, assessment }: { clientId: string; matterId: string; run: ReviewProgramRunRead; assessment: MatterDefinitionAssessmentRead }) {
  const round = Number(run.progress_summary?.definition_assessment_round ?? 1);
  const processed = assessment.summarized_count + assessment.skipped_count + assessment.failed_count;
  const total = assessment.selected_count || assessment.requested_document_count;
  const parameters = new URLSearchParams({ assessment: assessment.id });
  if (assessment.guidance_id) parameters.set("guidance", assessment.guidance_id);
  return <Card className="border-primary/30 bg-primary/5"><CardHeader><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle>Definition reassessment · Round {round}</CardTitle><CardDescription>{assessment.status === "SUMMARIZING" ? "Analyzing documents against the newly published Matter Definition." : `Current activity: ${friendly(assessment.status)}.`}</CardDescription></div><StatusBadge status={assessment.status} /></div></CardHeader><CardContent><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-sm font-medium tabular-nums">{processed.toLocaleString()} of {total.toLocaleString()} documents processed</p><p className="mt-1 text-xs text-muted-foreground">{assessment.summarized_count.toLocaleString()} summarized · {assessment.skipped_count.toLocaleString()} skipped · {assessment.failed_count.toLocaleString()} failed</p></div><Button asChild size="sm" variant="outline"><Link href={`/app/clients/${clientId}/matters/${matterId}/definition?${parameters.toString()}`}><BookOpen />Open current assessment</Link></Button></div></CardContent></Card>;
}

export function definitionAssessmentHref(clientId: string, matterId: string, action: ReviewProgramActionRead): string {
  const base = `/app/clients/${clientId}/matters/${matterId}/definition`;
  const context = action.decision_context;
  if (!context || typeof context !== "object" || Array.isArray(context)) return base;
  const definitionId = context.matter_definition_id;
  const assessment = context.assessment;
  const assessmentId = assessment && typeof assessment === "object" && !Array.isArray(assessment)
    ? (assessment as Record<string, unknown>).id
    : null;
  const params = new URLSearchParams();
  if (typeof definitionId === "string" && definitionId) params.set("guidance", definitionId);
  if (typeof assessmentId === "string" && assessmentId) params.set("assessment", assessmentId);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function DefinitionAssessmentSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "DEFINITION_REFINEMENT" && action.stage_key !== "DEFINITION_ACCEPTANCE") return null;
  const assessment = action.decision_context?.assessment;
  if (!assessment || typeof assessment !== "object" || Array.isArray(assessment)) return null;
  const values = assessment as Record<string, unknown>;
  const questions = values.question_counts && typeof values.question_counts === "object" && !Array.isArray(values.question_counts)
    ? values.question_counts as Record<string, unknown>
    : {};
  const finalRefinement = action.decision_context?.final_unassessed_refinement;
  const finalValues = finalRefinement && typeof finalRefinement === "object" && !Array.isArray(finalRefinement)
    ? finalRefinement as Record<string, unknown>
    : null;
  return <>{finalValues && action.stage_key === "DEFINITION_ACCEPTANCE" ? <div className="mb-3 rounded-lg border border-warning/40 bg-warning/8 p-3 text-sm"><p className="font-semibold">Final refinement was not independently reassessed</p><p className="mt-1 text-xs text-muted-foreground">Revision {String(finalValues.accepted_revision ?? "—")} was produced from the resolved questions in assessment round {String(finalValues.assessment_round ?? "—")}. Accepting it records that limitation and continues to Q&amp;A setup.</p></div> : null}<div className="mb-3 grid gap-2 rounded-lg border bg-background/70 p-3 text-sm sm:grid-cols-2 lg:grid-cols-4"><div><p className="text-xs text-muted-foreground">Assessment</p><p className="font-medium">{friendly(String(values.status ?? "unknown"))}</p></div><div><p className="text-xs text-muted-foreground">Documents</p><p className="font-medium">{Number(values.summarized_count ?? 0)} summarized · {Number(values.failed_count ?? 0)} failed</p></div><div><p className="text-xs text-muted-foreground">Open questions</p><p className="font-medium">{Number(questions.open ?? 0)} open · {Number(questions.open_blocking ?? 0)} blocking</p></div><div><p className="text-xs text-muted-foreground">Refinement</p><p className="font-medium">{friendly(String(values.guidance_refinement_status ?? "not ready"))}</p></div></div></>;
}

function DefinitionRefinementCard({ clientId, matterId, action, canManage, busy, onResolve }: {
  clientId: string;
  matterId: string;
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onResolve: (choice: string, input?: Record<string, unknown>) => void;
}) {
  const assessment = action.decision_context?.assessment;
  const values = assessment && typeof assessment === "object" && !Array.isArray(assessment)
    ? assessment as Record<string, unknown>
    : {};
  const questions = values.question_counts && typeof values.question_counts === "object" && !Array.isArray(values.question_counts)
    ? values.question_counts as Record<string, unknown>
    : {};
  const openQuestions = Number(questions.open ?? 0);
  const refinementStatus = String(values.guidance_refinement_status ?? "NOT_READY");
  const publicationRequired = values.publication_required === true;
  const publicationReady = values.publication_ready === true;
  const refinedRevision = Number(values.refined_matter_definition_revision ?? 0) || null;
  const assessmentRound = Number(values.assessment_round ?? 1);
  const assessmentRoundLimit = Number(values.assessment_round_limit ?? 3);
  const assessmentLimitReached = values.assessment_round_limit_reached === true;
  const generating = ["QUEUED", "RUNNING"].includes(refinementStatus);
  const failed = refinementStatus === "FAILED";
  const canContinue = openQuestions === 0 && !generating && !failed && (!publicationRequired || publicationReady);
  const primary = action.allowed_choices.find((choice) => choice.key === "CONTINUE");
  const stop = action.allowed_choices.find((choice) => choice.key === "STOP");

  let guidance = "Resolve every assessment question before a revised Matter Definition can be prepared.";
  if (generating) guidance = "All questions are resolved. The revised Matter Definition draft is being created.";
  else if (failed) guidance = "The revised Matter Definition draft could not be created. Open the assessment for the failure details.";
  else if (publicationRequired && !publicationReady) guidance = `Draft revision ${refinedRevision ?? "—"} is ready. Review it and publish it before continuing.`;
  else if (publicationRequired && assessmentLimitReached) guidance = `Revision ${refinedRevision ?? "—"} is published, but assessment round ${assessmentRound} reached the configured limit of ${assessmentRoundLimit}. Choose whether to use this revision as the final candidate or run one additional validation assessment.`;
  else if (publicationRequired) guidance = `Revision ${refinedRevision ?? "—"} is published and ready for reassessment.`;
  else if (openQuestions === 0) guidance = "No revised draft is required. Continue to the Matter Definition acceptance decision.";

  const finalRoundDecision = publicationRequired && publicationReady && assessmentLimitReached;
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>Resolve findings and publish the revised Matter Definition</CardTitle><CardDescription>Review the assessment findings, answer every question, and explicitly publish any generated draft before the program reassesses it.</CardDescription></CardHeader><CardContent><DefinitionAssessmentSummary action={action} /><p className="mb-3 rounded-md border bg-background/70 p-3 text-sm font-medium">{guidance}</p><div className="flex flex-wrap gap-2"><Button asChild size="sm" variant="outline"><Link href={definitionAssessmentHref(clientId, matterId, action)}><BookOpen />{publicationRequired && !publicationReady ? "Review and publish draft" : "Open Matter Definition"}</Link></Button>{primary && finalRoundDecision ? <><Button size="sm" disabled={!canManage || busy || !canContinue} onClick={() => onResolve(primary.key, { final_round_decision: "ACCEPT_REFINEMENT" })}>Accept revision {refinedRevision ?? "—"} and continue to Q&amp;A setup</Button><Button size="sm" variant="outline" disabled={!canManage || busy || !canContinue} onClick={() => onResolve(primary.key, { final_round_decision: "EXTEND_AND_REASSESS" })}>Run additional validation assessment</Button></> : primary ? <Button size="sm" disabled={!canManage || busy || !canContinue} onClick={() => onResolve(primary.key)} title={canContinue ? primary.consequence : guidance}>{publicationRequired ? "Continue to reassessment" : "Continue to acceptance"}</Button> : null}{stop ? <Button size="sm" variant="destructive" disabled={!canManage || busy} onClick={() => onResolve(stop.key)} title={stop.consequence}>{stop.label}</Button> : null}</div></CardContent></Card>;
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

function TrainingLabelSummary({ action, matterId }: { action: ReviewProgramActionRead; matterId: string }) {
  if (action.stage_key !== "DISAGREEMENT_REVIEW" && action.stage_key !== "TRAINING_LABEL_FINALIZATION") return null;
  const analysisRaw = action.decision_context?.discovery_analysis;
  const analysis = analysisRaw && typeof analysisRaw === "object" && !Array.isArray(analysisRaw) ? analysisRaw as Record<string, unknown> : {};
  const overallRaw = analysis.overall;
  const overall = overallRaw && typeof overallRaw === "object" && !Array.isArray(overallRaw) ? overallRaw as Record<string, unknown> : {};
  const resolutionRaw = action.decision_context?.training_label_resolution;
  const resolution = resolutionRaw && typeof resolutionRaw === "object" && !Array.isArray(resolutionRaw) ? resolutionRaw as Record<string, unknown> : {};
  const batches = Array.isArray(resolution.difference_batches)
    ? resolution.difference_batches.filter((batch): batch is Record<string, unknown> => Boolean(batch && typeof batch === "object" && !Array.isArray(batch)))
    : [];
  const profileRuns = Array.isArray(resolution.profile_run_ids) ? resolution.profile_run_ids : [];
  const analyzedProfileRuns = Array.isArray(analysis.profile_run_ids) ? analysis.profile_run_ids : [];
  const policy = resolution.policy ? friendly(String(resolution.policy)) : "Not selected";
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Label policy</p><p className="font-medium">{policy}</p></div><div><p className="text-xs text-muted-foreground">Pinned model runs</p><p className="font-medium">{profileRuns.length || analyzedProfileRuns.length}</p></div><div><p className="text-xs text-muted-foreground">Exact agreements</p><p className="font-medium">{Number(overall.exact_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Model differences</p><p className="font-medium">{Number(overall.partial_count ?? 0) + Number(overall.disagreement_count ?? 0)}</p></div></div>
    {batches.length ? <div className="space-y-2"><p className="text-xs font-medium text-muted-foreground">Ad hoc difference batches</p>{batches.map((batch) => <div key={String(batch.id)} className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background p-2"><div><p className="font-medium">{String(batch.name ?? "Difference review")}</p><p className="text-xs text-muted-foreground">{Number(batch.completed_document_count ?? 0)} of {Number(batch.document_count ?? 0)} completed · {friendly(String(batch.status ?? "queued"))}</p></div><Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}?batch=${String(batch.id)}`}>Open review batch</Link></Button></div>)}</div> : null}
  </div>;
}

function ClassifierReviewSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "CLASSIFIER_REVIEW") return null;
  const raw = action.decision_context?.classifier_results;
  const results = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const models = Array.isArray(results.models)
    ? results.models.filter((model): model is Record<string, unknown> => Boolean(model && typeof model === "object" && !Array.isArray(model)))
    : [];
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Overall quality</p><p className="font-medium">{friendly(String(results.overall_quality ?? "unknown"))}</p></div><div><p className="text-xs text-muted-foreground">Classifiers</p><p className="font-medium">{Number(results.model_count ?? models.length)}</p></div><div><p className="text-xs text-muted-foreground">Weak classifiers</p><p className="font-medium">{Number(results.weak_model_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Weak categories</p><p className="font-medium">{Number(results.weak_category_count ?? 0)}</p></div></div>
    <div className="space-y-2">{models.map((model) => {
      const metricsRaw = model.metrics;
      const metrics = metricsRaw && typeof metricsRaw === "object" && !Array.isArray(metricsRaw) ? metricsRaw as Record<string, unknown> : {};
      const weak = Array.isArray(model.weak_categories) ? model.weak_categories : [];
      return <div key={String(model.classification_model_id)} className="rounded-md border bg-background p-3"><div className="flex flex-wrap items-center justify-between gap-2"><p className="font-medium">{String(model.field_name ?? model.field_key ?? "Classifier")}</p><Badge variant="outline" className={model.quality === "WEAK" ? "border-destructive/40 text-destructive" : undefined}>{friendly(String(model.quality ?? "unknown"))}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{Number(model.training_eligible_count ?? 0)} training documents · PR AUC {formatMetric(metrics.primary_pr_auc)} · Macro F1 {formatMetric(metrics.macro_f1)} · {weak.length} weak or excluded categories</p></div>;
    })}</div>
    <p className="text-xs text-muted-foreground">These are group-aware cross-fit development estimates. Weak flags guide additional review; they are not a substitute for formal validation.</p>
  </div>;
}

function WholeCollectionScoringSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "WHOLE_COLLECTION_SCORING" && action.stage_key !== "CLASSIFIER_POSITIVE_COVERAGE") return null;
  const planRaw = action.decision_context?.scoring_plan;
  const plan = planRaw && typeof planRaw === "object" && !Array.isArray(planRaw) ? planRaw as Record<string, unknown> : {};
  const resultRaw = action.decision_context?.whole_collection_scoring;
  const result = resultRaw && typeof resultRaw === "object" && !Array.isArray(resultRaw) ? resultRaw as Record<string, unknown> : {};
  const modelsRaw = Array.isArray(result.models) ? result.models : Array.isArray(plan.models) ? plan.models : [];
  const models = modelsRaw.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)));
  const aggregateRaw = result.aggregate_model_document_counts;
  const aggregate = aggregateRaw && typeof aggregateRaw === "object" && !Array.isArray(aggregateRaw) ? aggregateRaw as Record<string, unknown> : {};
  const completed = action.stage_key === "CLASSIFIER_POSITIVE_COVERAGE";
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Scope</p><p className="font-medium">Whole matter</p></div><div><p className="text-xs text-muted-foreground">Classifiers</p><p className="font-medium">{Number(result.model_count ?? plan.model_count ?? models.length)}</p></div><div><p className="text-xs text-muted-foreground">Collection documents</p><p className="font-medium">{completed ? Number(result.collection_document_count ?? 0) : "Calculated at launch"}</p></div><div><p className="text-xs text-muted-foreground">Scored outputs</p><p className="font-medium">{completed ? Number(aggregate.scored_document_count ?? 0) : "Pending"}</p></div></div>
    <div className="space-y-2">{models.map((model) => <div key={String(model.classification_model_id)} className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background p-3"><div><p className="font-medium">{String(model.field_name ?? model.field_key ?? "Classifier")}</p><p className="text-xs text-muted-foreground">{completed ? `${Number(model.scored_document_count ?? 0)} scored · ${Number(model.abstained_document_count ?? 0)} abstained · ${Number(model.excluded_document_count ?? 0)} excluded` : `Accepted version · ${friendly(String(model.quality ?? "accepted"))}`}</p></div>{model.status ? <StatusBadge status={String(model.status)} /> : <Badge variant="outline">Ready</Badge>}</div>)}</div>
    <p className="text-xs text-muted-foreground">Counts are reported per classifier, so aggregate document counts can include the same document once for each coding field.</p>
  </div>;
}

function ClassifierPositiveCoverageSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "CLASSIFIER_POSITIVE_COVERAGE") return null;
  const raw = action.decision_context?.coverage_plan;
  const plan = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Required reviewers</p><p className="font-medium">{Number(plan.required_profile_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Candidate ceiling</p><p className="font-medium">{Number(plan.maximum_candidate_model_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Batching</p><p className="font-medium">Task-specific set</p></div></div>
    <p className="text-xs text-muted-foreground">The exact population is calculated from successful, non-abstained positive predictions and current-content Q&A results when you start coverage. Existing documents reviewed by both pinned profiles are excluded.</p>
  </div>;
}

function FinalAlignmentSummary({ action }: { action: ReviewProgramActionRead }) {
  if (action.stage_key !== "NO_CODE_QC") return null;
  const raw = action.decision_context?.final_alignment;
  const report = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const alignmentRaw = report.alignment_counts;
  const alignment = alignmentRaw && typeof alignmentRaw === "object" && !Array.isArray(alignmentRaw) ? alignmentRaw as Record<string, unknown> : {};
  const sourceRaw = report.source_coverage_counts;
  const source = sourceRaw && typeof sourceRaw === "object" && !Array.isArray(sourceRaw) ? sourceRaw as Record<string, unknown> : {};
  const issues = Array.isArray(report.issues) ? report.issues : [];
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Aligned population</p><p className="font-medium">{Number(report.population_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">All-source exact</p><p className="font-medium">{Number(alignment.EXACT_ALL ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Disputed or partial</p><p className="font-medium">{Number(alignment.PARTIAL ?? 0) + Number(alignment.DISAGREEMENT ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Issue assignments</p><p className="font-medium">{issues.length}</p></div></div>
    <p className="text-xs text-muted-foreground">{Number(source.CURRENT ?? 0)} documents have current-source results from both Q&A profiles; {Number(source.STALE_OR_PARTIAL ?? 0) + Number(source.MISSING_QA ?? 0) + Number(source.SOURCE_UNAVAILABLE ?? 0)} are stale, partial, missing, or source-unavailable. Agreement is consistency evidence, not accuracy.</p>
  </div>;
}

interface NegativePopulationQCSetupPayload {
  preview_hash: string;
  random_sample_size: number;
  near_threshold_sample_size: number;
  cohort_control_sample_size: number;
  mandatory_review_limit: number;
  target_batch_size: number;
  keep_families_together: boolean;
}

function NegativePopulationQCSetupCard({ action, canManage, busy, onSubmit, onStop }: {
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (payload: NegativePopulationQCSetupPayload) => void;
  onStop: () => void;
}) {
  const defaultsRaw = action.decision_context?.defaults;
  const defaults = defaultsRaw && typeof defaultsRaw === "object" && !Array.isArray(defaultsRaw) ? defaultsRaw as Record<string, unknown> : {};
  const [randomSample, setRandomSample] = useState(String(defaults.random_sample_size ?? 200));
  const [nearThreshold, setNearThreshold] = useState(String(defaults.near_threshold_sample_size ?? 100));
  const [cohortControl, setCohortControl] = useState(String(defaults.cohort_control_sample_size ?? 50));
  const [mandatoryLimit, setMandatoryLimit] = useState(String(defaults.mandatory_review_limit ?? 100));
  const [batchSize, setBatchSize] = useState(String(defaults.target_batch_size ?? 100));
  const [keepFamilies, setKeepFamilies] = useState(Boolean(defaults.keep_families_together ?? true));
  function submit(event: FormEvent) {
    event.preventDefault();
    const values = [randomSample, nearThreshold, cohortControl, mandatoryLimit, batchSize].map(Number);
    if (!values.every(Number.isInteger) || values.slice(0, 4).some((value) => value < 0) || values[4] < 1 || values.slice(0, 4).reduce((sum, value) => sum + value, 0) < 1) return;
    onSubmit({
      preview_hash: action.preview_hash,
      random_sample_size: values[0],
      near_threshold_sample_size: values[1],
      cohort_control_sample_size: values[2],
      mandatory_review_limit: values[3],
      target_batch_size: values[4],
      keep_families_together: keepFamilies,
    });
  }
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><FinalAlignmentSummary action={action} /><div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5"><div className="space-y-2"><Label htmlFor="qc-random">Random audit</Label><Input id="qc-random" type="number" min={0} max={25000} value={randomSample} onChange={(event) => setRandomSample(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="qc-threshold">Near threshold</Label><Input id="qc-threshold" type="number" min={0} max={25000} value={nearThreshold} onChange={(event) => setNearThreshold(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="qc-cohort">Cohort controls</Label><Input id="qc-cohort" type="number" min={0} max={25000} value={cohortControl} onChange={(event) => setCohortControl(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="qc-mandatory">Mandatory review cap</Label><Input id="qc-mandatory" type="number" min={0} max={25000} value={mandatoryLimit} onChange={(event) => setMandatoryLimit(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="qc-batch-size">Documents per batch</Label><Input id="qc-batch-size" type="number" min={1} max={10000} value={batchSize} onChange={(event) => setBatchSize(event.target.value)} /></div></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => setKeepFamilies(event.target.checked)} disabled={!canManage || busy} />Keep document families together</label><div className="rounded-lg border bg-background/70 p-3 text-xs text-muted-foreground"><p>The random audit is the only lane used for a collection-level miss-rate estimate. Near-threshold and cohort controls report discovery yield only. Failed, abstained, and stale documents with usable source content enter the mandatory-review lane; source-unavailable documents remain separately counted for manual resolution.</p></div><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy}>{busy ? "Starting…" : "Start Negative-population QC"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

function NegativePopulationQCReport({ action }: { action: ReviewProgramActionRead }) {
  if (action.action_type !== "NEGATIVE_POPULATION_QC_REPORT") return null;
  const qcRaw = action.decision_context?.negative_population_qc;
  const qc = qcRaw && typeof qcRaw === "object" && !Array.isArray(qcRaw) ? qcRaw as Record<string, unknown> : {};
  const reportRaw = qc.report;
  const report = reportRaw && typeof reportRaw === "object" && !Array.isArray(reportRaw) ? reportRaw as Record<string, unknown> : {};
  const estimateRaw = report.random_audit_estimate;
  const estimate = estimateRaw && typeof estimateRaw === "object" && !Array.isArray(estimateRaw) ? estimateRaw as Record<string, unknown> : {};
  const rate = typeof estimate.observed_positive_rate === "number" ? `${(estimate.observed_positive_rate * 100).toFixed(1)}%` : "Unavailable";
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm"><div className="grid gap-2 sm:grid-cols-5"><div><p className="text-xs text-muted-foreground">QC documents</p><p className="font-medium">{Number(report.selected_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">New positives</p><p className="font-medium">{Number(report.positive_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Random-audit rate</p><p className="font-medium">{rate}</p></div><div><p className="text-xs text-muted-foreground">Unresolved runs</p><p className="font-medium">{Number(report.unresolved_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Manual resolution</p><p className="font-medium">{Number(report.mandatory_unreviewable_count ?? 0)}</p></div></div><p className="text-xs text-muted-foreground">Only the random-audit lane supports a population estimate. Targeted-lane yield identifies risk but is not a collection-wide error rate.</p></div>;
}

function NegativePopulationQCFindingsReview({ action, matterId }: { action: ReviewProgramActionRead; matterId: string }) {
  if (action.action_type !== "NEGATIVE_POPULATION_QC_FINDINGS_REVIEW") return null;
  const qcRaw = action.decision_context?.negative_population_qc;
  const qc = qcRaw && typeof qcRaw === "object" && !Array.isArray(qcRaw) ? qcRaw as Record<string, unknown> : {};
  const reviewRaw = qc.findings_review;
  const review = reviewRaw && typeof reviewRaw === "object" && !Array.isArray(reviewRaw) ? reviewRaw as Record<string, unknown> : {};
  const batchId = String(review.review_batch_id ?? "");
  return <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background/70 p-3 text-sm"><div><p className="font-medium">Assigned QC findings batch</p><p className="text-xs text-muted-foreground">{Number(review.document_count ?? 0)} positive, disputed, or unresolved documents · assigned for human coding</p></div>{batchId ? <Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}?batch=${batchId}`}>Open review batch</Link></Button> : null}</div>;
}

function CompletionDecisionCard({ action, canManage, busy, onComplete, onStop }: {
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onComplete: (reason?: string) => void;
  onStop: () => void;
}) {
  const previewRaw = action.decision_context?.completion_preview;
  const preview = previewRaw && typeof previewRaw === "object" && !Array.isArray(previewRaw) ? previewRaw as Record<string, unknown> : {};
  const checklistRaw = preview.checklist;
  const checklist = checklistRaw && typeof checklistRaw === "object" && !Array.isArray(checklistRaw) ? checklistRaw as Record<string, unknown> : {};
  const incomplete = Object.entries(checklist).filter(([, value]) => value !== true);
  const [reason, setReason] = useState("");
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent className="space-y-4"><div className="grid gap-2 sm:grid-cols-2">{Object.entries(checklist).map(([key, value]) => <div key={key} className="flex items-center justify-between rounded-lg border bg-background/70 px-3 py-2 text-sm"><span>{friendly(key)}</span><Badge variant={value === true ? "active" : "outline"}>{value === true ? "Ready" : "Incomplete"}</Badge></div>)}</div><div className="grid gap-2 rounded-lg border bg-background/70 p-3 text-sm sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Child records</p><p className="font-medium">{Number(preview.child_record_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Recorded decisions</p><p className="font-medium">{Number(preview.resolved_decision_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Unresolved QC</p><p className="font-medium">{Number(preview.unresolved_qc_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Manual resolution</p><p className="font-medium">{Number(preview.manual_resolution_document_count ?? 0)}</p></div></div><div className="space-y-2"><Label htmlFor="completion-reason">Completion notes</Label><Textarea id="completion-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Optional rationale or accepted limitations" maxLength={4000} /></div><p className="text-xs text-muted-foreground">Completion creates an immutable report and closes this run. It does not publish model or Q&A coding into matter metadata.</p><div className="flex flex-wrap gap-2"><Button disabled={!canManage || busy || incomplete.length > 0} onClick={() => onComplete(reason.trim() || undefined)}>{busy ? "Completing…" : "Complete first-pass review"}</Button><Button variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></CardContent></Card>;
}

function CompletedReviewProgramSummary({ run }: { run: ReviewProgramRunRead }) {
  const summaryRaw = run.progress_summary?.completion_report;
  const summary = summaryRaw && typeof summaryRaw === "object" && !Array.isArray(summaryRaw) ? summaryRaw as Record<string, unknown> : {};
  if (!summary.id) return null;
  const unresolvedRaw = summary.unresolved;
  const unresolved = unresolvedRaw && typeof unresolvedRaw === "object" && !Array.isArray(unresolvedRaw) ? unresolvedRaw as Record<string, unknown> : {};
  return <Card className="border-emerald-500/40 bg-emerald-500/5"><CardHeader><CardTitle>First-pass review complete</CardTitle><CardDescription>The immutable completion report preserves the accepted workflow state and provenance. No coding was published to matter metadata.</CardDescription></CardHeader><CardContent><div className="grid gap-2 text-sm sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Completed</p><p className="font-medium">{formatDate(String(summary.completed_at ?? run.completed_at ?? ""))}</p></div><div><p className="text-xs text-muted-foreground">QC unresolved</p><p className="font-medium">{Number(unresolved.qc_unresolved_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Manual resolution</p><p className="font-medium">{Number(unresolved.manual_resolution_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Report hash</p><p className="truncate font-mono text-xs">{String(summary.content_hash ?? "")}</p></div></div></CardContent></Card>;
}

interface DiscoverySetupPayload {
  preview_hash: string;
  target_document_count: number;
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
  const [batchSize, setBatchSize] = useState(String(defaults.target_batch_size ?? 100));
  const [keepFamilies, setKeepFamilies] = useState(Boolean(defaults.keep_families_together ?? true));
  function submit(event: FormEvent) {
    event.preventDefault();
    const target = Number(targetCount);
    const size = Number(batchSize);
    if (![target, size].every(Number.isInteger) || target < 1 || size < 1) return;
    onSubmit({ preview_hash: action.preview_hash, target_document_count: target, target_batch_size: size, keep_families_together: keepFamilies });
  }
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><div className="grid gap-2 rounded-lg border bg-background/70 p-3 text-sm sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Retrieval queries</p><p className="font-medium">{Number(plan.query_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Pinned index</p><p className="font-medium">Generation {String(search.generation ?? "unavailable")}</p></div><div><p className="text-xs text-muted-foreground">Plan source</p><p className="font-medium">Accepted assessment</p></div></div><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="discovery-target">Document target</Label><Input id="discovery-target" type="number" min={1} max={100000} value={targetCount} onChange={(event) => setTargetCount(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="discovery-batch-size">Documents per batch</Label><Input id="discovery-batch-size" type="number" min={1} max={10000} value={batchSize} onChange={(event) => setBatchSize(event.target.value)} /></div></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => setKeepFamilies(event.target.checked)} disabled={!canManage || busy} />Keep document families together</label><p className="text-xs text-muted-foreground">The same immutable targeted population and managed child batches will be used for both pinned model profiles. Results remain isolated from matter metadata.</p><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy}>{busy ? "Starting…" : "Start discovery Q&A"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

interface ClassifierImprovementQueryPayload {
  metadata_definition_id: string;
  class_key: string;
  query: string;
  quota: number;
}

interface ClassifierImprovementSetupPayload {
  preview_hash: string;
  queries: ClassifierImprovementQueryPayload[];
  control_sample_size: number;
  target_batch_size: number;
  keep_families_together: boolean;
}

function ClassifierImprovementSetupCard({ action, canManage, busy, onSubmit, onStop }: {
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (payload: ClassifierImprovementSetupPayload) => void;
  onStop: () => void;
}) {
  const planRaw = action.decision_context?.retrieval_plan;
  const plan = planRaw && typeof planRaw === "object" && !Array.isArray(planRaw) ? planRaw as Record<string, unknown> : {};
  const initialTargets = Array.isArray(plan.targets)
    ? plan.targets.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const defaultsRaw = action.decision_context?.defaults;
  const defaults = defaultsRaw && typeof defaultsRaw === "object" && !Array.isArray(defaultsRaw) ? defaultsRaw as Record<string, unknown> : {};
  const [queries, setQueries] = useState<ClassifierImprovementQueryPayload[]>(() => initialTargets.map((target) => ({
    metadata_definition_id: String(target.metadata_definition_id ?? ""),
    class_key: String(target.class_key ?? ""),
    query: String(target.suggested_query ?? ""),
    quota: Number(target.suggested_quota ?? 50),
  })));
  const [controlCount, setControlCount] = useState(String(defaults.control_sample_size ?? 25));
  const [batchSize, setBatchSize] = useState(String(defaults.target_batch_size ?? 100));
  const [keepFamilies, setKeepFamilies] = useState(Boolean(defaults.keep_families_together ?? true));
  function updateQuery(index: number, patch: Partial<ClassifierImprovementQueryPayload>) {
    setQueries((current) => current.map((query, queryIndex) => queryIndex === index ? { ...query, ...patch } : query));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const control = Number(controlCount);
    const size = Number(batchSize);
    if (!queries.length || !Number.isInteger(control) || control < 0 || !Number.isInteger(size) || size < 1) return;
    if (queries.some((query) => !query.query.trim() || !Number.isInteger(query.quota) || query.quota < 1)) return;
    onSubmit({
      preview_hash: action.preview_hash,
      queries: queries.map((query) => ({ ...query, query: query.query.trim() })),
      control_sample_size: control,
      target_batch_size: size,
      keep_families_together: keepFamilies,
    });
  }
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><div className="space-y-3"><p className="text-sm font-medium">Weak-category retrieval plan</p>{initialTargets.map((target, index) => <div key={`${String(target.metadata_definition_id)}:${String(target.class_key)}`} className="grid gap-3 rounded-lg border bg-background/70 p-3 sm:grid-cols-[minmax(0,1fr)_8rem]"><div className="space-y-2"><Label htmlFor={`improvement-query-${index}`}>{String(target.field_name ?? target.field_key ?? "Coding field")} · {String(target.class_label ?? target.class_key)}</Label><Input id={`improvement-query-${index}`} value={queries[index]?.query ?? ""} onChange={(event) => updateQuery(index, { query: event.target.value })} maxLength={2000} required /></div><div className="space-y-2"><Label htmlFor={`improvement-quota-${index}`}>Target</Label><Input id={`improvement-quota-${index}`} type="number" min={1} max={25000} value={queries[index]?.quota ?? 50} onChange={(event) => updateQuery(index, { quota: Number(event.target.value) })} required /></div><p className="text-xs text-muted-foreground sm:col-span-2">{Array.isArray(target.quality_reasons) ? target.quality_reasons.map((reason) => friendly(String(reason))).join(" · ") : "Weak category"}</p></div>)}</div><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="improvement-control">Broad control</Label><Input id="improvement-control" type="number" min={0} max={25000} value={controlCount} onChange={(event) => setControlCount(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="improvement-batch-size">Documents per batch</Label><Input id="improvement-batch-size" type="number" min={1} max={10000} value={batchSize} onChange={(event) => setBatchSize(event.target.value)} /></div></div><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => setKeepFamilies(event.target.checked)} disabled={!canManage || busy} />Keep document families together</label><p className="text-xs text-muted-foreground">Previously reviewed program documents are excluded. The new frozen population will run through both pinned Q&A profiles, then return to support and label review before classifier retraining.</p><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy || !queries.length}>{busy ? "Starting…" : "Start targeted review"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
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
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><Plus />New Review Program</Button></DialogTrigger><DialogContent><form onSubmit={submit}><DialogHeader><DialogTitle>Create Review Program</DialogTitle><DialogDescription>Create a reusable First Pass Review workspace. After creation, guided setup begins with the Matter Definition; no analysis work launches yet.</DialogDescription></DialogHeader><div className="space-y-4"><div className="space-y-2"><Label htmlFor="review-program-name">Name</Label><Input id="review-program-name" value={name} onChange={(event) => { setName(event.target.value); if (!keyEdited) setKey(slug(event.target.value)); }} required maxLength={200} placeholder="Standard first pass" /></div><div className="space-y-2"><Label htmlFor="review-program-key">Key</Label><Input id="review-program-key" value={key} onChange={(event) => { setKeyEdited(true); setKey(slug(event.target.value)); }} required minLength={3} maxLength={100} pattern="[a-z][a-z0-9_-]+[a-z0-9]" placeholder="standard-first-pass" /><p className="text-xs text-muted-foreground">Stable within this matter; lowercase letters, numbers, hyphens, and underscores.</p></div><div className="space-y-2"><Label htmlFor="review-program-description">Program description (optional)</Label><Textarea id="review-program-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={4000} placeholder="For example: Standard first pass for the investigation team." /><p className="text-xs text-muted-foreground">Administrative note only. You will configure substantive review guidance in the Matter Definition during guided setup.</p></div></div><DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={create.isPending || !name.trim() || !key}>{create.isPending ? "Creating…" : "Create program"}</Button></DialogFooter></form></DialogContent></Dialog>;
}

function mutationError(fallback: string) {
  return (error: Error) => toast.error(error.message || fallback);
}

function friendly(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatMetric(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";
}

function slug(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100);
}
