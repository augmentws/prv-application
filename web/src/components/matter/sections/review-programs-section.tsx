"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Ban, BarChart3, BookOpen, CircleHelp, GitCompareArrows, ListChecks, Play, Plus, Presentation, RefreshCw, RotateCcw, Workflow, X } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { useMatter } from "@/components/matter/matter-context";
import { DecisionSpecificationView } from "@/components/decision-specification-view";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import type { AnalysisTaskBatchSetRead, AnalysisTaskBatchSetRunRead, MatterAnalysisTaskVersionRead, MatterDefinitionAssessmentQuestionRead, MatterDefinitionAssessmentRead, MatterDefinitionRead, MatterQuestionAnsweringJobRead, MetadataDefinitionRead, ReviewProgramActionRead, ReviewProgramCreate, ReviewProgramRead, ReviewProgramRunRead, ReviewProgramStageAttemptRead, ReviewWorkflowStageRead, ReviewWorkflowTemplateRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate, formatDateTime } from "@/lib/format";
import { classificationModelHref, classificationScoringRunHref, matterJobsHref, reviewProgramFinalAlignmentHref, reviewProgramQAPopulationStatisticsHref } from "@/lib/matter-routes";
import { cn } from "@/lib/utils";

const ACTIVE_RUN_STATUSES = new Set(["QUEUED", "RUNNING", "WAITING_FOR_USER"]);
const ACTIONABLE_RUN_STATUSES = new Set([...ACTIVE_RUN_STATUSES, "FAILED"]);

function preferredReviewProgramRun(
  runs: ReviewProgramRunRead[],
  currentRunId: string | null | undefined,
): ReviewProgramRunRead | null {
  const current = currentRunId ? runs.find((run) => run.id === currentRunId) : null;
  if (current) return current;
  return [...runs].sort((left, right) => {
    const rank = (run: ReviewProgramRunRead) => ACTIONABLE_RUN_STATUSES.has(run.status)
      ? 0
      : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status)
        ? 1
        : 2;
    const rankDifference = rank(left) - rank(right);
    if (rankDifference) return rankDifference;
    const stageDifference = Number(right.current_stage_ordinal ?? 0) - Number(left.current_stage_ordinal ?? 0);
    if (stageDifference) return stageDifference;
    return new Date(right.created_at).getTime() - new Date(left.created_at).getTime();
  })[0] ?? null;
}

function effectiveStageBatchingPolicy(stage: ReviewWorkflowStageRead): string {
  if (stage.key === "CLASSIFIER_POSITIVE_COVERAGE" || stage.key === "NO_CODE_QC") {
    return "AD_HOC_BATCH";
  }
  return stage.batching_policy;
}

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
  CLASSIFIER_POSITIVE_COVERAGE: "Creates a batch of classifier-positive documents that have not been coded by either Q&A model, runs both Q&A tasks, and shows how their issue codings compare with the classifier.",
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
  const replayMode = searchParams.get("view") === "replay";
  const requestedRunId = searchParams.get("run");
  const liveRun = preferredReviewProgramRun(runs.data ?? [], selectedProgram?.current_run_id);
  const currentRun = replayMode
    ? runs.data?.find((run) => run.id === requestedRunId) ?? liveRun
    : liveRun;
  const template = templates.data?.find((item) =>
    item.key === selectedProgram?.template_key && item.version === selectedProgram.template_version,
  ) ?? templates.data?.find((item) => item.key === selectedProgram?.template_key) ?? null;
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
    params.delete("run");
    params.delete("view");
    params.delete("stage");
    params.delete("attempt");
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  function presentRun(run: ReviewProgramRunRead) {
    const params = new URLSearchParams(searchParams.toString());
    const orderedStages = [...(template?.stages ?? [])].sort((left, right) => left.ordinal - right.ordinal);
    const firstStage = orderedStages.find((stage) =>
      run.stage_attempts?.some((attempt) => attempt.stage_key === stage.key)
      || run.actions?.some((action) => action.stage_key === stage.key),
    );
    const firstAttempt = firstStage
      ? run.stage_attempts?.filter((attempt) => attempt.stage_key === firstStage.key).sort((left, right) => left.attempt - right.attempt)[0]
      : undefined;
    params.set("program", run.program_id);
    params.set("run", run.id);
    params.set("view", "replay");
    if (firstStage) params.set("stage", firstStage.key);
    if (firstAttempt) params.set("attempt", firstAttempt.id);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  function updateReplayLocation(stageKey: string, attemptId?: string) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("view", "replay");
    params.set("stage", stageKey);
    if (attemptId) params.set("attempt", attemptId);
    else params.delete("attempt");
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  function exitReplay() {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("run");
    params.delete("view");
    params.delete("stage");
    params.delete("attempt");
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
    mutationFn: ({ programId, confirmReplaceUnfinishedRun = false }: { programId: string; confirmReplaceUnfinishedRun?: boolean }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: crypto.randomUUID(),
        confirm_replace_unfinished_run: confirmReplaceUnfinishedRun,
      }),
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
  const cancelDiscoveryQA = useMutation({
    mutationFn: ({ programId, runId }: { programId: string; runId: string }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/cancel-discovery-qa`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: crypto.randomUUID(), reason: "Canceled from the Discovery Q&A task" }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("Discovery Q&A was canceled. You can review its progress and reset it when ready."); },
    onError: mutationError("Discovery Q&A could not be canceled."),
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
  const prepareNegativePopulationQCInventory = useMutation({
    mutationFn: ({ programId, runId, action }: { programId: string; runId: string; action: ReviewProgramActionRead }) => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/actions/${action.id}/negative-population-qc-inventory`, {
      method: "POST",
      body: JSON.stringify({ preview_hash: action.preview_hash }),
    }),
    onSuccess: async (run) => { await refresh(run.program_id); toast.success("QC population calculation has started."); },
    onError: mutationError("QC populations could not be calculated."),
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
      {selectedProgram && replayMode && currentRun ? <ReviewProgramReplay
        clientId={clientId}
        matterId={matterId}
        program={selectedProgram}
        run={currentRun}
        stages={template?.stages ?? []}
        selectedStageKey={searchParams.get("stage")}
        selectedAttemptId={searchParams.get("attempt")}
        onNavigate={updateReplayLocation}
        onExit={exitReplay}
      /> : selectedProgram ? <ProgramWorkspace
        clientId={clientId}
        matterId={matterId}
        program={selectedProgram}
        run={currentRun}
        stages={template?.stages ?? []}
        canManage={canManage}
        loading={runs.isPending}
        error={runs.error}
        busy={startRun.isPending || command.isPending || resetDiscoveryQA.isPending || cancelDiscoveryQA.isPending || resolveAction.isPending || setupDefinition.isPending || setupQA.isPending || setupDiscovery.isPending || setupClassifierImprovement.isPending || setupNegativePopulationQC.isPending || prepareNegativePopulationQCInventory.isPending}
        onStart={(confirmReplaceUnfinishedRun) => startRun.mutate({ programId: selectedProgram.id, confirmReplaceUnfinishedRun })}
        onCommand={(verb) => currentRun && command.mutate({ programId: selectedProgram.id, runId: currentRun.id, verb })}
        onResetDiscovery={() => currentRun && resetDiscoveryQA.mutate({ programId: selectedProgram.id, runId: currentRun.id })}
        onCancelDiscovery={() => currentRun && cancelDiscoveryQA.mutate({ programId: selectedProgram.id, runId: currentRun.id })}
        onResolve={(action, choiceKey, reason, input) => currentRun && resolveAction.mutate({ programId: selectedProgram.id, runId: currentRun.id, action, choiceKey, reason, input })}
        onSetupDefinition={(action, payload) => currentRun && setupDefinition.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupQA={(action, payload) => currentRun && setupQA.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupDiscovery={(action, payload) => currentRun && setupDiscovery.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupClassifierImprovement={(action, payload) => currentRun && setupClassifierImprovement.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onSetupNegativePopulationQC={(action, payload) => currentRun && setupNegativePopulationQC.mutate({ programId: selectedProgram.id, runId: currentRun.id, actionId: action.id, payload })}
        onPrepareNegativePopulationQCInventory={(action) => currentRun && prepareNegativePopulationQCInventory.mutate({ programId: selectedProgram.id, runId: currentRun.id, action })}
        onPresent={() => currentRun && presentRun(currentRun)}
      /> : null}
    </div>}
  </div>;
}

function ProgramWorkspace({ clientId, matterId, program, run, stages, canManage, loading, error, busy, onStart, onCommand, onResetDiscovery, onCancelDiscovery, onResolve, onSetupDefinition, onSetupQA, onSetupDiscovery, onSetupClassifierImprovement, onSetupNegativePopulationQC, onPrepareNegativePopulationQCInventory, onPresent }: {
  clientId: string;
  matterId: string;
  program: ReviewProgramRead;
  run: ReviewProgramRunRead | null;
  stages: ReviewWorkflowStageRead[];
  canManage: boolean;
  loading: boolean;
  error: Error | null;
  busy: boolean;
  onStart: (confirmReplaceUnfinishedRun?: boolean) => void;
  onCommand: (verb: "cancel" | "retry" | "reconcile") => void;
  onResetDiscovery: () => void;
  onCancelDiscovery: () => void;
  onResolve: (action: ReviewProgramActionRead, choiceKey: string, reason?: string, input?: Record<string, unknown>) => void;
  onSetupDefinition: (action: ReviewProgramActionRead, payload: DefinitionSetupPayload) => void;
  onSetupQA: (action: ReviewProgramActionRead, payload: QASetupPayload) => void;
  onSetupDiscovery: (action: ReviewProgramActionRead, payload: DiscoverySetupPayload) => void;
  onSetupClassifierImprovement: (action: ReviewProgramActionRead, payload: ClassifierImprovementSetupPayload) => void;
  onSetupNegativePopulationQC: (action: ReviewProgramActionRead, payload: NegativePopulationQCSetupPayload) => void;
  onPrepareNegativePopulationQCInventory: (action: ReviewProgramActionRead) => void;
  onPresent: () => void;
}) {
  const openAction = run?.actions?.find((action) => action.status === "OPEN") ?? null;
  const attemptsByStage = useMemo(() => new Map(run?.stage_attempts?.map((attempt) => [attempt.stage_key, attempt]) ?? []), [run?.stage_attempts]);
  const skippedStageKeys = useMemo(() => new Set(
    (Array.isArray(run?.progress_summary?.skipped_stages) ? run.progress_summary.skipped_stages : [])
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
      .map((item) => String(item.stage_key ?? ""))
      .filter(Boolean),
  ), [run]);
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
          {run?.stage_attempts?.length ? <Button size="sm" variant="outline" onClick={onPresent}><Presentation />Present run</Button> : null}
          {run && canManage && ACTIVE_RUN_STATUSES.has(run.status) ? <><RefreshStateButton busy={busy} onClick={() => onCommand("reconcile")} /><Button size="sm" variant="outline" disabled={busy} onClick={() => onCommand("cancel")}><Ban />Stop program</Button></> : null}
          {run?.status === "FAILED" && canManage ? <ReplacementRunDialog run={run} busy={busy} onConfirm={() => onStart(true)} /> : null}
          {run && ["COMPLETED", "COMPLETED_WITH_ERRORS", "CANCELED"].includes(run.status) && canManage ? <Button size="sm" variant="outline" disabled={busy} onClick={() => onStart(false)}><Play />Start another run</Button> : null}
        </div>
      </CardHeader>
      {run ? <CardContent><div className="flex flex-wrap items-center gap-2">{run.status === "FAILED" ? <Badge variant="outline" className="border-amber-500/40 text-amber-700 dark:text-amber-300">Needs attention</Badge> : <StatusBadge status={run.status} />}<Badge variant="outline">{isDefinitionReassessment ? `Definition refinement · reassessment round ${definitionAssessmentRound}` : friendly(run.current_stage)}</Badge><span className="text-xs text-muted-foreground">Started {formatDate(run.created_at)}</span></div></CardContent> : <CardContent><div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-sm font-medium">This program has not been configured yet.</p><p className="mt-1 text-sm text-muted-foreground">Begin the guided setup to create, clone, or adopt a Matter Definition. No document analysis starts until you save the definition and choose to assess it.</p></div>{canManage ? <Button size="sm" disabled={busy} onClick={() => onStart(false)}><Play />Start Setup</Button> : null}</div></CardContent>}
    </Card>
    {run && currentAssessment ? <DefinitionAssessmentProgress clientId={clientId} matterId={matterId} run={run} assessment={currentAssessment} /> : null}
    {run?.status === "COMPLETED" || run?.status === "COMPLETED_WITH_ERRORS" ? <CompletedReviewProgramSummary run={run} /> : null}
    {run ? <ProgramClassifierModels clientId={clientId} matterId={matterId} run={run} /> : null}
    {openAction?.action_type === "MATTER_DEFINITION_SETUP" ? <DefinitionSetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDefinition(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "MATTER_DEFINITION_REFINEMENT" ? <DefinitionRefinementCard clientId={clientId} matterId={matterId} action={openAction} canManage={canManage} busy={busy} onResolve={(choice, input) => onResolve(openAction, choice, undefined, input)} /> : openAction?.action_type === "QA_TASK_SETUP" ? <QASetupCard matterId={matterId} action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupQA(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "DISCOVERY_QA_SETUP" ? <DiscoverySetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupDiscovery(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "CLASSIFIER_IMPROVEMENT_SETUP" ? <ClassifierImprovementSetupCard action={openAction} canManage={canManage} busy={busy} onSubmit={(payload) => onSetupClassifierImprovement(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "NEGATIVE_POPULATION_QC_SETUP" ? <NegativePopulationQCSetupCard key={`${openAction.id}:${String((openAction.decision_context?.negative_population_qc_inventory as { input_manifest_content_hash?: string } | undefined)?.input_manifest_content_hash ?? "pending")}`} action={openAction} run={run} clientId={clientId} matterId={matterId} canManage={canManage} busy={busy} onPrepareInventory={() => onPrepareNegativePopulationQCInventory(openAction)} onSubmit={(payload) => onSetupNegativePopulationQC(openAction, payload)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "CLASSIFIER_POSITIVE_COVERAGE" ? <ClassifierPositiveCoverageDecisionCard action={openAction} canManage={canManage} busy={busy} onSubmit={(minimumConfidence) => onResolve(openAction, "RUN_COVERAGE", undefined, { minimum_confidence: minimumConfidence })} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "REVIEW_PROGRAM_COMPLETION" ? <CompletionDecisionCard action={openAction} canManage={canManage} busy={busy} onComplete={(reason) => onResolve(openAction, "COMPLETE", reason)} onStop={() => onResolve(openAction, "STOP")} /> : openAction?.action_type === "TRAINING_LABEL_POLICY" ? <TrainingLabelPolicyCard action={openAction} matterId={matterId} canManage={canManage} busy={busy} onResolve={(choice, input) => onResolve(openAction, choice, undefined, input)} /> : openAction?.action_type === "TRAINING_LABEL_FINALIZATION" ? <TrainingLabelFinalizationCard action={openAction} matterId={matterId} canManage={canManage} busy={busy} onResolve={(choice, input) => onResolve(openAction, choice, undefined, input)} /> : openAction ? <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{openAction.title}</CardTitle><CardDescription>{openAction.explanation}</CardDescription></CardHeader><CardContent><DefinitionAssessmentSummary action={openAction} /><QAPublicationSummary action={openAction} /><IssueSupportSummary action={openAction} matterId={matterId} run={run} /><TrainingLabelSummary action={openAction} matterId={matterId} /><ClassifierReviewSummary action={openAction} /><WholeCollectionScoringSummary action={openAction} /><ClassifierPositiveCoverageSummary action={openAction} /><FinalAlignmentSummary action={openAction} run={run} clientId={clientId} matterId={matterId} /><NegativePopulationQCReport action={openAction} matterId={matterId} /><NegativePopulationQCFindingsReview action={openAction} matterId={matterId} />{openAction.stage_key === "DEFINITION_ACCEPTANCE" ? <Button asChild size="sm" variant="outline" className="mb-3"><Link href={definitionAssessmentHref(clientId, matterId, openAction)}><BookOpen />Open Matter Definition</Link></Button> : null}<div className="flex flex-wrap gap-2">{openAction.allowed_choices.map((choice, index) => {
      return <DecisionChoiceButton key={`${choice.key}-${index}`} choice={choice} variant={choice.key === "STOP" ? "destructive" : "default"} disabled={!canManage || busy} onClick={() => onResolve(openAction, choice.key)} />;
    })}</div></CardContent></Card> : null}
    {run ? <Card><CardHeader><CardTitle>Program stages</CardTitle><CardDescription>The coordinator records every stage attempt and waits only at explicit human gates.</CardDescription></CardHeader><CardContent><ol className="grid gap-2 sm:grid-cols-2">{stages.map((stage) => {
      const attempt = isDefinitionReassessment && stage.key === "DEFINITION_ASSESSMENT"
        ? completedDefinitionAssessmentAttempt
        : attemptsByStage.get(stage.key);
      const containsCurrentReassessment = isDefinitionReassessment && stage.key === "DEFINITION_REFINEMENT";
      const isCurrent = (stage.key === run.current_stage && !isDefinitionReassessment) || containsCurrentReassessment;
      const skipped = skippedStageKeys.has(stage.key)
        || (!attempt && !isDefinitionReassessment && stage.ordinal < run.current_stage_ordinal);
      const upcoming = !skipped && (isDefinitionReassessment ? stage.ordinal > 3 : stage.ordinal > run.current_stage_ordinal);
      const isRunning = attempt?.status === "RUNNING" || (isCurrent && run.status === "RUNNING" && attempt?.status !== "FAILED");
      const showsRunFailure = isCurrent && run.status === "FAILED";
      const showsAttemptFailure = isCurrent && attempt?.status === "FAILED";
      const showsStageFailure = showsRunFailure || showsAttemptFailure;
      const canRetryFailure = run.failure_retriable || stage.key === "CLASSIFIER_TRAINING";
      const showsDiscoveryTask = isCurrent && stage.key === "DISCOVERY_QA" && Boolean(run.progress_summary?.discovery_qa);
      const showsWholeCollectionScoring = isCurrent && stage.key === "WHOLE_COLLECTION_SCORING";
      const showsClassifierPositiveCoverage = isCurrent && stage.key === "CLASSIFIER_POSITIVE_COVERAGE" && Boolean(run.progress_summary?.classifier_positive_coverage);
      return <li key={stage.key} className={cn("flex items-start gap-3 rounded-lg border p-3", isCurrent && "border-primary/50 bg-primary/5", upcoming && "opacity-55", (isRunning || showsDiscoveryTask || showsWholeCollectionScoring || showsClassifierPositiveCoverage || showsStageFailure) && "sm:col-span-2")}>
        <span className="grid size-7 shrink-0 place-items-center rounded-full bg-muted text-xs font-semibold">{stage.ordinal}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2"><p className="text-sm font-medium">{stage.name}</p><ProgramStageTooltip stage={stage} /></div>
          <div className="mt-1 flex flex-wrap gap-1">{attempt ? <StatusBadge status={attempt.status} /> : skipped ? <Badge variant="outline">Skipped</Badge> : <Badge variant="outline">Upcoming</Badge>}<Badge variant="outline">{friendly(effectiveStageBatchingPolicy(stage))}</Badge>{stage.requires_human_gate && !skipped ? <Badge variant="outline">Decision</Badge> : null}</div>
          {skipped ? <p className="mt-2 text-xs text-muted-foreground">Not requested; the accepted classifiers continue directly to whole-collection scoring.</p> : null}
          {showsStageFailure ? <div className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3">
            <p className="text-sm font-medium text-destructive">{stage.name} failed</p>
            <p className="mt-1 text-sm text-destructive">{attempt?.error_summary || run.failure_summary || "This stage could not be completed."}</p>
            {stage.key === "NO_CODE_QC" ? <NegativePopulationQCFailureSummary run={run} matterId={matterId} /> : null}
            {stage.key === "CLASSIFIER_TRAINING" && !run.failure_retriable ? <p className="mt-2 text-xs text-muted-foreground">After correcting the reported cause, retry starts a new classifier-training attempt with the same frozen labels and configuration. It does not replace the program run.</p> : null}
            {canManage ? <div className="mt-3 flex flex-wrap gap-2">
              {stage.key === "CLASSIFIER_POSITIVE_COVERAGE" || stage.key === "NO_CODE_QC" ? <RefreshStateButton busy={busy} onClick={() => onCommand("reconcile")} /> : null}
              {canRetryFailure ? <Button size="sm" disabled={busy} onClick={() => onCommand("retry")}><RotateCcw />Retry {stage.name}</Button> : null}
            </div> : null}
          </div> : null}
          {showsDiscoveryTask ? <DiscoveryQAProgress clientId={clientId} matterId={matterId} run={run} canManage={canManage} busy={busy} onCancel={onCancelDiscovery} onReset={onResetDiscovery} /> : null}
          {showsWholeCollectionScoring ? <WholeCollectionScoringProgress run={run} openAction={openAction} /> : null}
          {showsClassifierPositiveCoverage ? <ClassifierPositiveCoverageProgress clientId={clientId} matterId={matterId} run={run} openAction={openAction} /> : null}
          {containsCurrentReassessment ? <div className="mt-2 flex items-center justify-between gap-2 rounded-md border border-primary/30 bg-background/80 px-2 py-1.5"><span className="text-xs font-medium">Reassessment · Round {definitionAssessmentRound}</span><StatusBadge status={currentDefinitionAssessmentAttempt?.status ?? run.status} /></div> : null}
        </div>
      </li>;
    })}</ol></CardContent></Card> : null}
  </div>;
}

function ReviewProgramReplay({ clientId, matterId, program, run, stages, selectedStageKey, selectedAttemptId, onNavigate, onExit }: {
  clientId: string;
  matterId: string;
  program: ReviewProgramRead;
  run: ReviewProgramRunRead;
  stages: ReviewWorkflowStageRead[];
  selectedStageKey: string | null;
  selectedAttemptId: string | null;
  onNavigate: (stageKey: string, attemptId?: string) => void;
  onExit: () => void;
}) {
  const replayableStages = [...stages]
    .sort((left, right) => left.ordinal - right.ordinal)
    .filter((stage) =>
      run.stage_attempts?.some((attempt) => attempt.stage_key === stage.key)
      || run.actions?.some((action) => action.stage_key === stage.key),
    );
  const selectedStage = replayableStages.find((stage) => stage.key === selectedStageKey)
    ?? replayableStages[0];
  if (!selectedStage) {
    return <Card><CardHeader><CardTitle>No replayable stages</CardTitle><CardDescription>This run has not recorded a stage attempt or decision yet.</CardDescription></CardHeader><CardContent><Button variant="outline" onClick={onExit}><X />Return to live workspace</Button></CardContent></Card>;
  }
  const attempts = (run.stage_attempts ?? [])
    .filter((attempt) => attempt.stage_key === selectedStage.key)
    .sort((left, right) => left.attempt - right.attempt);
  const selectedAttempt = attempts.find((attempt) => attempt.id === selectedAttemptId)
    ?? [...attempts].reverse().find((attempt) => attempt.status === "COMPLETED")
    ?? attempts.at(-1)
    ?? null;
  const actions = (run.actions ?? []).filter((action) =>
    action.stage_key === selectedStage.key
    && (!selectedAttempt || action.stage_attempt_id == null || action.stage_attempt_id === selectedAttempt.id),
  );
  const recordedActions = actions.filter((action) => {
    if (action.status !== "RESOLVED") return false;
    const payload = action.resolution_payload;
    return Boolean(payload && typeof payload === "object" && !Array.isArray(payload) && typeof payload.choice_key === "string");
  });
  const stageIndex = replayableStages.findIndex((stage) => stage.key === selectedStage.key);
  const previousStage = replayableStages[stageIndex - 1];
  const nextStage = replayableStages[stageIndex + 1];
  const originalQATaskRaw = selectedAttempt?.result_summary?.accepted_qa_task;
  const originalQATask = originalQATaskRaw && typeof originalQATaskRaw === "object" && !Array.isArray(originalQATaskRaw)
    ? originalQATaskRaw as Record<string, unknown>
    : {};
  const finalQATaskRaw = run.progress_summary?.accepted_qa_task;
  const finalQATask = finalQATaskRaw && typeof finalQATaskRaw === "object" && !Array.isArray(finalQATaskRaw)
    ? finalQATaskRaw as Record<string, unknown>
    : originalQATask;
  const originalQATaskVersion = Number(originalQATask.version ?? 0);
  const finalQATaskVersion = Number(finalQATask.version ?? originalQATaskVersion);
  const qaTaskVersionCount = originalQATaskVersion && finalQATaskVersion && originalQATaskVersion !== finalQATaskVersion ? 2 : 1;
  const recordedResultSummary = selectedAttempt ? recordedStageResultSummary(run, selectedAttempt) : {};
  const navigateToStage = (stage: ReviewWorkflowStageRead) => {
    const stageAttempts = (run.stage_attempts ?? [])
      .filter((attempt) => attempt.stage_key === stage.key)
      .sort((left, right) => left.attempt - right.attempt);
    const preferred = [...stageAttempts].reverse().find((attempt) => attempt.status === "COMPLETED")
      ?? stageAttempts.at(-1);
    onNavigate(stage.key, preferred?.id);
  };

  return <div className="space-y-4">
    <Card className="border-sky-500/40 bg-sky-500/5">
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><div className="mb-2 flex flex-wrap items-center gap-2"><Badge variant="active">Demo replay</Badge><Badge variant="outline">Read only</Badge><StatusBadge status={run.status} /></div><CardTitle>{program.name}</CardTitle><CardDescription>Presenting recorded stages and decisions from run {run.id}. Nothing in replay can change the source run or start new work.</CardDescription></div>
        <Button size="sm" variant="outline" onClick={onExit}><X />Exit replay</Button>
      </CardHeader>
    </Card>

    <Card>
      <CardHeader><CardTitle>Presentation position</CardTitle><CardDescription>Choose a recorded stage and, when applicable, the attempt or round to present.</CardDescription></CardHeader>
      <CardContent className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(12rem,0.45fr)_auto] sm:items-end">
        <div className="space-y-2"><Label htmlFor="replay-stage">Stage</Label><Select value={selectedStage.key} onValueChange={(stageKey) => {
          const stage = replayableStages.find((item) => item.key === stageKey);
          if (stage) navigateToStage(stage);
        }}><SelectTrigger id="replay-stage"><SelectValue /></SelectTrigger><SelectContent>{replayableStages.map((stage) => <SelectItem key={stage.key} value={stage.key}>{stage.ordinal}. {stage.name}</SelectItem>)}</SelectContent></Select></div>
        <div className="space-y-2">{selectedStage.key === "QA_TASK_SETUP" && attempts.length === 1 ? <><Label>Task configurations</Label><div className="flex h-9 items-center rounded-md border px-3 text-sm">{qaTaskVersionCount} {qaTaskVersionCount === 1 ? "version" : "versions"}{finalQATaskVersion ? ` · final version ${finalQATaskVersion}` : ""}</div></> : <><Label htmlFor="replay-attempt">Attempt</Label>{attempts.length ? <Select value={selectedAttempt?.id} onValueChange={(attemptId) => onNavigate(selectedStage.key, attemptId)}><SelectTrigger id="replay-attempt"><SelectValue /></SelectTrigger><SelectContent>{attempts.map((attempt) => <SelectItem key={attempt.id} value={attempt.id}>Attempt {attempt.attempt} · {friendly(attempt.status)}</SelectItem>)}</SelectContent></Select> : <div className="flex h-9 items-center rounded-md border px-3 text-sm text-muted-foreground">Decision only</div>}</>}</div>
        <p className="pb-2 text-sm tabular-nums text-muted-foreground">Stage {stageIndex + 1} of {replayableStages.length}</p>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">Stage {selectedStage.ordinal}</Badge>{selectedAttempt ? <StatusBadge status={selectedAttempt.status} /> : null}{selectedAttempt && attempts.length > 1 ? <Badge variant="outline">Attempt {selectedAttempt.attempt}</Badge> : null}</div><CardTitle>{selectedStage.name}</CardTitle><CardDescription>{PROGRAM_STAGE_HELP[selectedStage.key] ?? `Recorded ${friendly(selectedStage.key)} activity.`}</CardDescription></CardHeader>
      <CardContent className="space-y-4">
        {selectedAttempt?.error_summary ? <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3"><p className="text-sm font-medium text-destructive">Attempt did not complete</p><p className="mt-1 text-sm text-destructive">{selectedAttempt.error_summary}</p>{selectedAttempt.error_code ? <p className="mt-2 font-mono text-xs text-muted-foreground">{selectedAttempt.error_code}</p> : null}{run.status === "FAILED" && run.failure_retriable && selectedAttempt.stage_key === run.current_stage ? <div className="mt-3 flex flex-wrap items-center gap-3"><p className="text-xs text-muted-foreground">Replay is read only. Return to the live run to retry this stage.</p><Button size="sm" variant="outline" onClick={onExit}><RotateCcw />Exit replay to retry</Button></div> : null}</div> : null}
        <div className="grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-4"><div className="rounded-lg border bg-background/70 p-3"><p className="text-xs text-muted-foreground">Started</p><p className="font-medium">{selectedAttempt?.started_at ? formatDateTime(selectedAttempt.started_at) : "Not recorded"}</p></div><div className="rounded-lg border bg-background/70 p-3"><p className="text-xs text-muted-foreground">Ended</p><p className="font-medium">{selectedAttempt?.completed_at ? formatDateTime(selectedAttempt.completed_at) : "Not completed"}</p></div><div className="rounded-lg border bg-background/70 p-3"><p className="text-xs text-muted-foreground">Elapsed</p><p className="font-medium">{formatElapsed(selectedAttempt?.started_at, selectedAttempt?.completed_at)}</p></div><div className="rounded-lg border bg-background/70 p-3"><p className="text-xs text-muted-foreground">Batching</p><p className="font-medium">{friendly(effectiveStageBatchingPolicy(selectedStage))}</p></div></div>
        {selectedStage.key === "DEFINITION_ASSESSMENT" && selectedAttempt ? <DefinitionAssessmentReplay clientId={clientId} matterId={matterId} attempt={selectedAttempt} /> : null}
        {selectedStage.key === "QA_TASK_SETUP" && selectedAttempt ? <QATaskSetupReplay matterId={matterId} run={run} attempt={selectedAttempt} /> : null}
        {selectedStage.key === "DISCOVERY_QA" && selectedAttempt ? <DiscoveryQAReplay clientId={clientId} matterId={matterId} run={run} attempt={selectedAttempt} /> : null}
        {selectedStage.key === "DISCOVERY_ANALYSIS" && selectedAttempt ? <DiscoveryAnalysisReplay matterId={matterId} run={run} attempt={selectedAttempt} /> : null}
        {selectedStage.key === "WHOLE_COLLECTION_SCORING" && selectedAttempt ? <WholeCollectionScoringReplay clientId={clientId} matterId={matterId} attempt={selectedAttempt} /> : null}
        {(selectedStage.key === "CLASSIFIER_POSITIVE_COVERAGE" || selectedStage.key === "FINAL_ALIGNMENT") && selectedAttempt ? <ClassifierPositiveCoverageComparisonReplay clientId={clientId} matterId={matterId} run={run} /> : null}
        {selectedAttempt && (Object.keys(selectedAttempt.input_snapshot ?? {}).length || Object.keys(recordedResultSummary).length) ? <details className="rounded-lg border bg-background/70 p-3"><summary className="cursor-pointer text-sm font-medium">{selectedStage.key === "DISCOVERY_QA" ? "Technical stage record (JSON)" : "Recorded stage data"}</summary><div className="mt-3 grid gap-3 lg:grid-cols-2">{Object.keys(selectedAttempt.input_snapshot ?? {}).length ? <ReplayJsonBlock title="Inputs" value={selectedAttempt.input_snapshot} /> : null}{Object.keys(recordedResultSummary).length ? <ReplayJsonBlock title={selectedStage.key === "QA_TASK_SETUP" ? "Final results and setup history" : "Results"} value={recordedResultSummary} /> : null}</div></details> : null}
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>Recorded outcome</CardTitle><CardDescription>{recordedActions.length ? "Only the actions taken in this run are shown." : "This stage did not record a completed action for the selected attempt."}</CardDescription></CardHeader>
      <CardContent className="space-y-4">{recordedActions.length ? recordedActions.map((action) => <ReplayAction key={action.id} action={action} clientId={clientId} matterId={matterId} run={run} showStageSummary={selectedStage.key !== "QA_TASK_SETUP"} />) : <p className="text-sm text-muted-foreground">No completed action was recorded.</p>}</CardContent>
    </Card>

    <div className="flex flex-wrap items-center justify-between gap-3">
      <Button variant="outline" disabled={!previousStage} onClick={() => previousStage && navigateToStage(previousStage)}><ArrowLeft />Previous stage</Button>
      <Button variant="outline" disabled={!nextStage} onClick={() => nextStage && navigateToStage(nextStage)}>Next stage<ArrowRight /></Button>
    </div>
  </div>;
}

function ReplayAction({ action, clientId, matterId, run, showStageSummary = true }: { action: ReviewProgramActionRead; clientId: string; matterId: string; run: ReviewProgramRunRead; showStageSummary?: boolean }) {
  const resolution = action.resolution_payload && typeof action.resolution_payload === "object" && !Array.isArray(action.resolution_payload)
    ? action.resolution_payload as Record<string, unknown>
    : {};
  const selectedChoiceKey = typeof resolution.choice_key === "string" ? resolution.choice_key : "";
  const selectedChoice = action.allowed_choices.find((choice) => choice.key === selectedChoiceKey);
  return <div className="space-y-4 rounded-lg border bg-background/70 p-4">
    {selectedChoice ? <div className="rounded-lg border border-primary/40 bg-primary/5 p-3 ring-1 ring-primary/10"><div className="flex items-start justify-between gap-2"><p className="font-medium">{selectedChoice.label}</p><Badge variant="active">Completed</Badge></div><p className="mt-1 text-sm text-muted-foreground">{selectedChoice.consequence}</p></div> : <div><div className="flex flex-wrap items-center gap-2"><p className="font-medium">{action.title}</p><Badge variant="outline">{friendly(action.status)}</Badge></div>{action.status === "OPEN" ? <p className="mt-1 text-sm text-muted-foreground">{action.explanation}</p> : null}</div>}
    {showStageSummary ? <><DefinitionAssessmentSummary action={action} /><QAPublicationSummary action={action} /><IssueSupportSummary action={action} matterId={matterId} run={run} /><ClassifierReviewSummary action={action} />{action.stage_key !== "WHOLE_COLLECTION_SCORING" ? <WholeCollectionScoringSummary action={action} /> : null}<ClassifierPositiveCoverageSummary action={action} /><FinalAlignmentSummary action={action} run={run} clientId={clientId} matterId={matterId} /><NegativePopulationQCReport action={action} matterId={matterId} /></> : null}
    {action.status === "OPEN" ? <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">This decision is still open in the source run. Replay cannot resolve it.</p> : null}
    {action.resolution_reason && showStageSummary ? <div className="rounded-lg border p-3 text-sm"><p className="text-xs text-muted-foreground">Recorded rationale</p><p className="mt-1">{action.resolution_reason}</p></div> : null}
    {action.resolved_at ? <p className="text-xs text-muted-foreground">Resolved {formatDate(action.resolved_at)}{action.resolved_by_user_id ? ` · user ${action.resolved_by_user_id}` : ""}</p> : null}
  </div>;
}

function recordedStageResultSummary(run: ReviewProgramRunRead, attempt: ReviewProgramStageAttemptRead): Record<string, unknown> {
  const result = attempt.result_summary ?? {};
  if (attempt.stage_key !== "QA_TASK_SETUP") return result;
  const originalRaw = result.accepted_qa_task;
  const original = originalRaw && typeof originalRaw === "object" && !Array.isArray(originalRaw)
    ? originalRaw as Record<string, unknown>
    : {};
  const finalRaw = run.progress_summary?.accepted_qa_task;
  const final = finalRaw && typeof finalRaw === "object" && !Array.isArray(finalRaw)
    ? finalRaw as Record<string, unknown>
    : original;
  if (!Object.keys(final).length) return result;
  return {
    ...result,
    accepted_qa_task: final,
    original_stage_accepted_qa_task: original,
    qa_task_repin: run.progress_summary?.qa_task_repin ?? null,
  };
}

function ReplayJsonBlock({ title, value }: { title: string; value: unknown }) {
  const serialized = JSON.stringify(value, null, 2);
  const display = serialized.length > 12_000 ? `${serialized.slice(0, 12_000)}\n…` : serialized;
  return <div className="min-w-0"><p className="mb-2 text-xs font-medium text-muted-foreground">{title}</p><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 text-xs">{display}</pre></div>;
}

function DefinitionAssessmentReplay({ clientId, matterId, attempt }: {
  clientId: string;
  matterId: string;
  attempt: ReviewProgramStageAttemptRead;
}) {
  const assessmentId = typeof attempt.input_snapshot?.assessment_id === "string"
    ? attempt.input_snapshot.assessment_id
    : "";
  const assessment = useQuery({
    queryKey: ["definition-assessment-replay", matterId, assessmentId],
    queryFn: () => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}`),
    enabled: Boolean(assessmentId),
  });
  const questions = useQuery({
    queryKey: ["definition-assessment-replay-questions", matterId, assessmentId],
    queryFn: () => coreApi<MatterDefinitionAssessmentQuestionRead[]>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/questions`),
    enabled: Boolean(assessmentId),
  });
  if (!assessmentId) return <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">This historical attempt does not contain an assessment identifier, so its questions cannot be reconstructed.</div>;
  if (assessment.isPending || questions.isPending) return <div className="rounded-lg border p-3 text-sm text-muted-foreground">Loading assessment questions and recorded results…</div>;
  if (assessment.error || questions.error || !assessment.data) return <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">The assessment questions or recorded results are unavailable.</div>;

  const result = assessment.data;
  const definitionId = typeof attempt.input_snapshot?.matter_definition_id === "string"
    ? attempt.input_snapshot.matter_definition_id
    : result.guidance_id ?? "";
  const baseParams = new URLSearchParams();
  if (definitionId) baseParams.set("guidance", definitionId);
  baseParams.set("assessment", assessmentId);
  const assessmentHref = `/app/clients/${clientId}/matters/${matterId}/definition?${baseParams.toString()}`;
  const diffParams = new URLSearchParams(baseParams);
  diffParams.set("compare_from", result.matter_definition_revision_id);
  if (result.refined_matter_definition_revision_id) diffParams.set("compare_to", result.refined_matter_definition_revision_id);
  const diffHref = `/app/clients/${clientId}/matters/${matterId}/definition?${diffParams.toString()}`;
  const synthesis = result.synthesis_result && typeof result.synthesis_result === "object" && !Array.isArray(result.synthesis_result)
    ? result.synthesis_result as Record<string, unknown>
    : {};

  return <section className="space-y-4 rounded-lg border bg-background/70 p-4" aria-label={`Definition assessment ${attempt.attempt} recorded results`}>
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">Definition assessment round {attempt.attempt}</p><p className="mt-1 text-sm text-muted-foreground">Questions and answers are loaded from the retained assessment record.</p></div><div className="flex flex-wrap gap-2"><Button asChild size="sm" variant="outline"><Link href={assessmentHref}><BookOpen />Open assessment</Link></Button>{result.refined_matter_definition_revision_id ? <Button asChild size="sm" variant="outline"><Link href={diffHref} target="_blank" rel="noopener noreferrer" title="Open the definition comparison in a new tab and keep this replay stage open"><GitCompareArrows />View definition changes</Link></Button> : null}</div></div>
    <div className="grid gap-2 text-sm sm:grid-cols-4"><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Result</p><p className="font-medium">{friendly(result.status)}</p></div><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Documents</p><p className="font-medium">{result.summarized_count.toLocaleString()} summarized · {result.failed_count.toLocaleString()} failed</p></div><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Synthesis</p><p className="font-medium">{friendly(String(synthesis.status ?? "unavailable"))}</p></div><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Definition change</p><p className="font-medium">{result.refined_matter_definition_revision_id ? "Revision created" : "No revision created"}</p></div></div>
    <div><p className="mb-2 text-sm font-medium">Assessment questions</p>{questions.data?.length ? <div className="space-y-2">{questions.data.map((question, index) => <div key={question.id} className="rounded-lg border p-3"><div className="flex flex-wrap items-start justify-between gap-2"><p className="text-sm font-medium">{index + 1}. {question.question}</p><div className="flex flex-wrap gap-1"><Badge variant="outline">{friendly(question.priority)}</Badge>{question.blocking ? <Badge variant="accent">Blocking</Badge> : null}<StatusBadge status={question.status} /></div></div>{question.rationale ? <p className="mt-2 text-xs text-muted-foreground">{question.rationale}</p> : null}<div className="mt-3 rounded-md bg-muted/50 p-3 text-sm"><p className="text-xs font-medium text-muted-foreground">Recorded result</p>{question.status === "ANSWERED" ? <p className="mt-1 whitespace-pre-wrap">{question.answer}</p> : question.status === "DISMISSED" ? <p className="mt-1 text-muted-foreground">Question dismissed without an answer.</p> : <p className="mt-1 text-muted-foreground">No answer was recorded.</p>}{question.answered_at ? <p className="mt-2 text-xs text-muted-foreground">Recorded {formatDate(question.answered_at)}</p> : null}</div></div>)}</div> : <p className="rounded-md border p-3 text-sm text-muted-foreground">This assessment completed without generating clarification questions.</p>}</div>
  </section>;
}

function QATaskSetupReplay({ matterId, run, attempt }: {
  matterId: string;
  run: ReviewProgramRunRead;
  attempt: ReviewProgramStageAttemptRead;
}) {
  const originalRaw = attempt.result_summary?.accepted_qa_task;
  const original = originalRaw && typeof originalRaw === "object" && !Array.isArray(originalRaw)
    ? originalRaw as Record<string, unknown>
    : {};
  const finalRaw = run.progress_summary?.accepted_qa_task;
  const final = finalRaw && typeof finalRaw === "object" && !Array.isArray(finalRaw)
    ? finalRaw as Record<string, unknown>
    : original;
  const taskId = typeof final.task_id === "string"
    ? final.task_id
    : typeof original.task_id === "string" ? original.task_id : "";
  const originalVersionId = typeof original.task_version_id === "string" ? original.task_version_id : "";
  const finalVersionId = typeof final.task_version_id === "string" ? final.task_version_id : originalVersionId;
  const finalVersionNumber = Number(final.version ?? 0);
  const [selectedVersionId, setSelectedVersionId] = useState(finalVersionId);
  const versions = useQuery({
    queryKey: ["analysis-task-versions", matterId, taskId, "review-program-replay"],
    queryFn: () => coreApi<MatterAnalysisTaskVersionRead[]>(`/v1/matters/${matterId}/analysis-tasks/${taskId}/versions`),
    enabled: Boolean(taskId),
  });
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
    enabled: Boolean(taskId),
  });
  if (!taskId) return <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">This historical attempt does not identify the generated Q&amp;A task.</div>;
  if (versions.isPending || definitions.isPending) return <div className="rounded-lg border p-3 text-sm text-muted-foreground">Loading retained Q&amp;A task versions…</div>;
  if (versions.error || definitions.error) return <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">The retained Q&amp;A task configuration history is unavailable.</div>;

  const byId = new Map((definitions.data ?? []).map((definition) => [definition.id, definition]));
  const relevant = (versions.data ?? [])
    .filter((version) => finalVersionNumber ? version.version <= finalVersionNumber : version.id === originalVersionId || version.id === finalVersionId)
    .sort((left, right) => {
      if (left.id === finalVersionId && right.id !== finalVersionId) return -1;
      if (right.id === finalVersionId && left.id !== finalVersionId) return 1;
      return right.version - left.version;
    });
  const selectedVersion = relevant.find((version) => version.id === selectedVersionId)
    ?? relevant.find((version) => version.id === finalVersionId)
    ?? relevant[0];
  const changedAfterStage = Boolean(originalVersionId && finalVersionId && originalVersionId !== finalVersionId);
  return <section className="space-y-3 rounded-lg border bg-background/70 p-4" aria-label="Q&A task configuration history">
    <div><p className="font-medium">Q&amp;A task configuration history</p><p className="mt-1 text-sm text-muted-foreground">The final task used by the program is shown by default. Select an earlier retained version to review its prior configuration.</p></div>
    {changedAfterStage ? <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm"><p className="font-medium">The task changed after the original Stage 5 publication.</p><p className="mt-1 text-muted-foreground">The final accepted task version below superseded the original setup and is the version used by the program.</p></div> : null}
    {relevant.length > 1 ? <div className="max-w-sm space-y-2"><Label htmlFor="replay-qa-task-version">Task version</Label><Select value={selectedVersion?.id} onValueChange={setSelectedVersionId}><SelectTrigger id="replay-qa-task-version"><SelectValue /></SelectTrigger><SelectContent>{relevant.map((version) => <SelectItem key={version.id} value={version.id}>Version {version.version}{version.id === finalVersionId ? " · Final used by program" : version.id === originalVersionId ? " · Original Stage 5 setup" : " · Earlier version"}</SelectItem>)}</SelectContent></Select></div> : null}
    {selectedVersion ? (() => {
      const isOriginal = selectedVersion.id === originalVersionId;
      const isFinal = selectedVersion.id === finalVersionId;
      const fieldIds = selectedVersion.destination_metadata_definition_ids ?? [];
      const specification = selectedVersion.decision_specification && typeof selectedVersion.decision_specification === "object"
        ? selectedVersion.decision_specification as unknown as Record<string, unknown>
        : {};
      const questions = specification.questions && typeof specification.questions === "object" && !Array.isArray(specification.questions)
        ? Object.keys(specification.questions as Record<string, unknown>).length
        : 0;
      const policy = specification.decision_policy && typeof specification.decision_policy === "object" && !Array.isArray(specification.decision_policy)
        ? specification.decision_policy as Record<string, unknown>
        : {};
      const recommendations = policy.recommendations && typeof policy.recommendations === "object" && !Array.isArray(policy.recommendations)
        ? Object.keys(policy.recommendations as Record<string, unknown>).length
        : 0;
      return <div key={selectedVersion.id} className={cn("rounded-lg border p-3", isFinal && "border-primary bg-primary/5 ring-1 ring-primary/20")}><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="font-medium">{isFinal ? "Final task used by program" : "Earlier task setup"}</p><p className="text-xs text-muted-foreground">Task version {selectedVersion.version}</p></div><div className="flex flex-wrap gap-1">{isOriginal && !isFinal ? <Badge variant="outline">Original Stage 5 setup</Badge> : null}{isFinal ? <Badge variant="active">Final version used</Badge> : null}<StatusBadge status={selectedVersion.status} /></div></div><div className="mt-3 grid gap-2 text-sm sm:grid-cols-2"><div><p className="text-xs text-muted-foreground">Questions</p><p className="font-medium">{countLabel(questions, "question")}</p></div><div><p className="text-xs text-muted-foreground">Recommendations</p><p className="font-medium">{countLabel(recommendations, "recommendation")}</p></div></div><p className="mt-3 text-xs font-medium text-muted-foreground">Coding fields</p><div className="mt-1 flex flex-wrap gap-1.5">{fieldIds.length ? fieldIds.map((fieldId) => { const definition = byId.get(fieldId); return <Badge key={fieldId} variant="outline">{definition?.display_name ?? definition?.key ?? fieldId}</Badge>; }) : <span className="text-sm text-muted-foreground">No coding fields recorded.</span>}</div>{selectedVersion.decision_specification ? <details className="mt-3 rounded-md border bg-background/70 p-3" open={isFinal}><summary className="cursor-pointer text-sm font-medium">Decision prompt</summary><div className="mt-3"><DecisionSpecificationView specification={selectedVersion.decision_specification} /></div></details> : null}<p className="mt-3 text-xs text-muted-foreground">Published {selectedVersion.published_at ? formatDate(selectedVersion.published_at) : "not recorded"} · compilation {friendly(selectedVersion.compilation_status)}</p></div>;
    })() : <p className="rounded-md border p-3 text-sm text-muted-foreground">No retained task versions match the final program pin.</p>}
  </section>;
}

function DiscoveryQAReplay({ clientId, matterId, run, attempt }: {
  clientId: string;
  matterId: string;
  run: ReviewProgramRunRead;
  attempt: ReviewProgramStageAttemptRead;
}) {
  const runRaw = run.progress_summary?.discovery_qa;
  const runValues = runRaw && typeof runRaw === "object" && !Array.isArray(runRaw) ? runRaw as Record<string, unknown> : {};
  const resultRaw = attempt.result_summary?.discovery_qa;
  const resultValues = resultRaw && typeof resultRaw === "object" && !Array.isArray(resultRaw) ? resultRaw as Record<string, unknown> : {};
  const values = { ...runValues, ...resultValues };
  const batchSetId = typeof values.batch_set_id === "string" ? values.batch_set_id : "";
  const taskId = typeof values.task_id === "string" ? values.task_id : "";
  const batchSet = useQuery({
    queryKey: ["analysis-task-batch-set", matterId, taskId, batchSetId, "review-program-replay"],
    queryFn: () => coreApi<AnalysisTaskBatchSetRead>(`/v1/matters/${matterId}/analysis-tasks/${taskId}/batch-sets/${batchSetId}`),
    enabled: Boolean(taskId && batchSetId),
  });
  const jobs = useQuery({
    queryKey: ["matter-question-answering-jobs", matterId, batchSetId, "review-program-replay"],
    queryFn: () => coreApi<MatterQuestionAnsweringJobRead[]>(`/v1/matters/${matterId}/analysis-tasks/question-answering-jobs?batch_set_id=${encodeURIComponent(batchSetId)}`),
    enabled: Boolean(batchSetId),
  });
  const linkedJobs = jobs.data ?? [];
  const providerTotals = linkedJobs.reduce<Record<string, { successful: number; failed: number; total: number; statuses: Set<string>; model: string; startedAt: string | null; endedAt: string | null }>>((totals, job) => {
    const label = job.decision_provider === "OPENAI_DECISIONS" ? "OpenAI Decisions" : "Jev";
    const current = totals[label] ?? { successful: 0, failed: 0, total: 0, statuses: new Set<string>(), model: job.model_key, startedAt: null, endedAt: null };
    current.successful += job.successful_document_count;
    current.failed += job.failed_document_count ?? 0;
    current.total += job.document_count;
    current.statuses.add(job.status);
    if (job.started_at && (!current.startedAt || Date.parse(job.started_at) < Date.parse(current.startedAt))) current.startedAt = job.started_at;
    if (job.completed_at && (!current.endedAt || Date.parse(job.completed_at) > Date.parse(current.endedAt))) current.endedAt = job.completed_at;
    totals[label] = current;
    return totals;
  }, {});
  const selectedCount = Number(values.selected_document_count ?? batchSet.data?.population_count ?? 0);
  const targetCount = Number(values.target_document_count ?? selectedCount);
  const batchCount = batchSet.data?.batch_count ?? 0;
  const batchSize = Number(values.target_batch_size ?? batchSet.data?.target_batch_size ?? 0);
  const finalTaskRaw = run.progress_summary?.accepted_qa_task;
  const finalTask = finalTaskRaw && typeof finalTaskRaw === "object" && !Array.isArray(finalTaskRaw) ? finalTaskRaw as Record<string, unknown> : {};
  const taskVersion = Number(finalTask.version ?? 0);
  const jobsHref = matterJobsHref(clientId, matterId, "QUESTION_ANSWERING", { batch_set: batchSetId });
  const loading = Boolean(taskId && batchSetId) && (batchSet.isPending || jobs.isPending);
  const loadError = batchSet.error ?? jobs.error;

  return <section className="space-y-4 rounded-lg border bg-background/70 p-4" aria-label="Discovery Q&A recorded results">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">Discovery Q&amp;A execution</p><p className="mt-1 text-sm text-muted-foreground">One frozen population was divided into task-specific batches and reviewed by both pinned model profiles.</p></div>{batchSetId ? <Button asChild size="sm" variant="outline"><Link href={jobsHref}><ListChecks />View linked jobs</Link></Button> : null}</div>
    {loading ? <p className="rounded-md border p-3 text-sm text-muted-foreground">Loading the retained Batch Set and model runs…</p> : loadError ? <p className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">The retained Batch Set or linked model jobs could not be loaded. The frozen stage record remains available below.</p> : <>
      <div className="grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-5">
        <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Population</p><p className="font-medium">{selectedCount.toLocaleString()} selected</p><p className="text-xs text-muted-foreground">Target {targetCount.toLocaleString()}{targetCount > selectedCount ? ` · ${(targetCount - selectedCount).toLocaleString()} short` : ""}</p></div>
        <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Batch Set</p><p className="font-medium">{batchCount.toLocaleString()} {batchCount === 1 ? "batch" : "batches"}</p><p className="text-xs text-muted-foreground">Up to {batchSize.toLocaleString()} documents each</p></div>
        <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Families</p><p className="font-medium">{values.keep_families_together ? "Kept together" : "Not expanded"}</p></div>
        <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Q&amp;A task</p><p className="font-medium">{taskVersion ? `Version ${taskVersion}` : "Recorded task"}</p><p className="text-xs text-muted-foreground">Final program pin</p></div>
        <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Result</p><p className="font-medium">{friendly(attempt.status)}</p><p className="text-xs text-muted-foreground">{batchSet.data ? `Batch Set ${friendly(batchSet.data.status)}` : "Batch Set retained"}</p></div>
      </div>
      <div>
        <div className="mb-2 flex items-center justify-between gap-2"><p className="text-sm font-medium">Model execution</p><Badge variant="outline">{Object.keys(providerTotals).length} profiles</Badge></div>
        {Object.keys(providerTotals).length ? <Table><TableHeader><TableRow><TableHead>Model profile</TableHead><TableHead>Model</TableHead><TableHead>Status</TableHead><TableHead>Started</TableHead><TableHead>Ended</TableHead><TableHead>Elapsed</TableHead><TableHead className="text-right">Successful</TableHead><TableHead className="text-right">Failed</TableHead><TableHead className="text-right">Expected</TableHead></TableRow></TableHeader><TableBody>{Object.entries(providerTotals).map(([provider, counts]) => {
          const status = counts.statuses.size === 1 ? [...counts.statuses][0] : counts.statuses.has("FAILED") ? "COMPLETED_WITH_ERRORS" : "COMPLETED";
          return <TableRow key={provider}><TableCell className="font-medium">{provider}</TableCell><TableCell>{counts.model}</TableCell><TableCell><StatusBadge status={status} /></TableCell><TableCell className="whitespace-nowrap text-xs">{counts.startedAt ? formatDateTime(counts.startedAt) : "Not recorded"}</TableCell><TableCell className="whitespace-nowrap text-xs">{counts.endedAt ? formatDateTime(counts.endedAt) : "Not completed"}</TableCell><TableCell className="whitespace-nowrap tabular-nums">{formatElapsed(counts.startedAt, counts.endedAt)}</TableCell><TableCell className="text-right tabular-nums">{counts.successful.toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{counts.failed.toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{counts.total.toLocaleString()}</TableCell></TableRow>;
        })}</TableBody></Table> : <p className="rounded-md border p-3 text-sm text-muted-foreground">No linked model jobs were retained for this Batch Set.</p>}
      </div>
    </>}
  </section>;
}

function ClassifierPositiveCoverageProgress({ clientId, matterId, run, openAction }: { clientId: string; matterId: string; run: ReviewProgramRunRead; openAction: ReviewProgramActionRead | null }) {
  const raw = run.progress_summary?.classifier_positive_coverage;
  const coverage = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const reviewBatchId = typeof coverage.review_batch_id === "string" ? coverage.review_batch_id : "";
  const jobs = useQuery({
    queryKey: ["matter-question-answering-jobs", matterId, "review-batch", reviewBatchId],
    queryFn: () => coreApi<MatterQuestionAnsweringJobRead[]>(`/v1/matters/${matterId}/analysis-tasks/question-answering-jobs?review_batch_id=${encodeURIComponent(reviewBatchId)}`),
    enabled: Boolean(reviewBatchId),
    refetchInterval: (query) => query.state.data?.some((job) => !TERMINAL_DISCOVERY_JOB_STATUSES.has(job.status)) ? 1_500 : false,
  });
  const linkedJobs = jobs.data ?? [];
  const currentBatchPopulation = Number(coverage.uncovered_document_count ?? 0);
  const population = Number(coverage.coverage_population_document_count ?? currentBatchPopulation);
  const errorContextRaw = openAction?.action_type === "CLASSIFIER_POSITIVE_COVERAGE_ERRORS"
    ? openAction.decision_context?.classifier_positive_coverage_errors
    : null;
  const errorContext = errorContextRaw && typeof errorContextRaw === "object" && !Array.isArray(errorContextRaw) ? errorContextRaw as Record<string, unknown> : {};
  const cumulativeProfiles = Array.isArray(errorContext.profile_runs)
    ? errorContext.profile_runs.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const useCumulative = cumulativeProfiles.length > 0;
  const expectedReviews = useCumulative
    ? cumulativeProfiles.reduce((total, profile) => total + Number(profile.processed_document_count ?? 0), 0)
    : linkedJobs.length
    ? linkedJobs.reduce((total, job) => total + job.document_count, 0)
    : currentBatchPopulation * Number(Array.isArray(coverage.required_profiles) ? coverage.required_profiles.length : 2);
  const successful = useCumulative
    ? cumulativeProfiles.reduce((total, profile) => total + Number(profile.completed_document_count ?? 0), 0)
    : linkedJobs.reduce((total, job) => total + job.successful_document_count, 0);
  const failed = useCumulative
    ? cumulativeProfiles.reduce((total, profile) => total + Number(profile.failed_document_count ?? 0), 0)
    : linkedJobs.reduce((total, job) => total + (job.failed_document_count ?? 0), 0);
  const processed = useCumulative
    ? expectedReviews
    : linkedJobs.reduce((total, job) => total + job.processed_document_count, 0);
  const reused = useCumulative
    ? cumulativeProfiles.reduce((total, profile) => total + Number(profile.reused_document_count ?? 0), 0)
    : Math.max(0, processed - successful - failed);
  const resolved = successful + reused;
  const state = String(run.progress_summary?.classifier_positive_coverage_state ?? coverage.state ?? "");
  const allTerminal = linkedJobs.length > 0 && linkedJobs.every((job) => TERMINAL_DISCOVERY_JOB_STATUSES.has(job.status));
  const activity = state === "COMPLETED_WITH_ERRORS" ? "Completed with errors"
    : state === "MATERIALIZING_AD_HOC_BATCH" ? "Preparing ad hoc batch"
    : !linkedJobs.length ? "Starting linked model reviews"
    : failed > 0 && allTerminal ? "Completed with errors"
    : allTerminal ? "Model reviews complete"
    : "Reviewing classifier-positive documents";
  const jobsHref = matterJobsHref(clientId, matterId, "QUESTION_ANSWERING", { review_batch: reviewBatchId });
  const providerRows = useCumulative ? cumulativeProfiles.map((profile) => ({
    id: String(profile.id ?? profile.execution_profile_id),
    label: String(profile.model_name ?? profile.profile_key ?? "Q&A model"),
    status: String(profile.status ?? "COMPLETED"),
    successful: Number(profile.completed_document_count ?? 0),
    reused: Number(profile.reused_document_count ?? 0),
    failed: Number(profile.failed_document_count ?? 0),
    total: Number(profile.processed_document_count ?? 0),
  })) : linkedJobs.map((job) => {
    const jobFailed = job.failed_document_count ?? 0;
    const jobReused = Math.max(0, job.processed_document_count - job.successful_document_count - jobFailed);
    return {
      id: job.id,
      label: job.model_key,
      status: job.status,
      successful: job.successful_document_count,
      reused: jobReused,
      failed: jobFailed,
      total: job.document_count,
    };
  });

  return <div className="mt-3 border-t pt-3">
    <div className="grid gap-3 text-sm sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
      <div><p className="text-xs text-muted-foreground">Status</p><p className="font-medium">{activity}</p><p className="mt-1 text-xs text-muted-foreground">{currentBatchPopulation.toLocaleString()} documents in this coverage attempt · {population.toLocaleString()} in the original coverage population · {Number(coverage.already_covered_document_count ?? 0).toLocaleString()} already covered before this attempt</p></div>
      <div><p className="text-xs text-muted-foreground">Model reviews</p><p className="font-medium tabular-nums">{resolved.toLocaleString()} of {expectedReviews.toLocaleString()} resolved</p><p className="mt-1 text-xs text-muted-foreground">{successful.toLocaleString()} newly successful · {reused.toLocaleString()} existing results reused{failed ? ` · ${failed.toLocaleString()} failed` : ""}</p></div>
      <div className="flex justify-end">{reviewBatchId ? <Button asChild size="sm" variant="outline"><Link href={jobsHref}><ListChecks />View coverage jobs</Link></Button> : null}</div>
    </div>
    {providerRows.length ? <div className="mt-3 grid gap-2 sm:grid-cols-2">{providerRows.map((provider) => <div key={provider.id} className="rounded-md border bg-background/70 p-2"><div className="flex items-center justify-between gap-2"><p className="text-xs font-medium">{provider.label}</p><StatusBadge status={provider.status} /></div><p className="mt-1 text-xs tabular-nums text-muted-foreground">{(provider.successful + provider.reused).toLocaleString()}/{provider.total.toLocaleString()} resolved · {provider.successful.toLocaleString()} new · {provider.reused.toLocaleString()} reused{provider.failed ? ` · ${provider.failed.toLocaleString()} failed` : ""}</p></div>)}</div> : <p className="mt-2 text-xs text-muted-foreground">The two Q&amp;A jobs will appear after the ad hoc batch is ready.</p>}
  </div>;
}

function WholeCollectionScoringProgress({ run, openAction }: { run: ReviewProgramRunRead; openAction: ReviewProgramActionRead | null }) {
  const raw = run.progress_summary?.whole_collection_scoring;
  const scoring = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const state = typeof scoring.state === "string" ? scoring.state : "";
  const models = Array.isArray(scoring.models)
    ? scoring.models.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const preparationRaw = scoring.preparation_progress;
  const preparation = preparationRaw && typeof preparationRaw === "object" && !Array.isArray(preparationRaw)
    ? preparationRaw as Record<string, unknown>
    : {};
  const processed = Number(preparation.processed_document_count ?? 0);
  const population = Number(preparation.population_document_count ?? 0);
  const ready = Number(preparation.feature_ready_count ?? 0);
  const missing = Number(preparation.feature_missing_count ?? 0);
  const failed = Number(preparation.feature_failed_count ?? 0);
  const percent = population > 0 ? Math.min(100, Math.max(0, (processed / population) * 100)) : 0;
  const preparing = state === "PREPARING_INPUTS";
  const waitingForDecision = !state && openAction?.action_type === "WHOLE_COLLECTION_SCORING";
  const title = preparing ? "Preparing scoring inputs"
    : waitingForDecision ? "Waiting for your decision"
    : state === "RUNNING" ? "Scoring the whole collection"
    : friendly(state || run.status);
  const detail = preparing
    ? String(scoring.status_detail ?? "The durable worker is freezing whole-matter inputs before the classifier jobs are created.")
    : waitingForDecision
      ? "Scoring has not started. Choose Score the collection above to create the whole-matter scoring jobs."
      : state === "RUNNING"
        ? `${models.filter((model) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(String(model.status))).length} of ${models.length} classifier jobs complete.`
        : "The program is checking the linked classifier scoring jobs.";
  return <div className="mt-3 border-t pt-3">
    <p className="text-xs text-muted-foreground">Status</p>
    <p className="font-medium">{title}</p>
    <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
    {preparing && population > 0 ? <div className="mt-3 space-y-1.5">
      <div className="flex items-center justify-between gap-3 text-xs"><span>{processed.toLocaleString()} of {population.toLocaleString()} document features checked</span><span className="tabular-nums">{percent.toFixed(1)}%</span></div>
      <div className="h-2 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} /></div>
      <p className="text-xs text-muted-foreground">{ready.toLocaleString()} ready · {missing.toLocaleString()} missing/skipped · {failed.toLocaleString()} invalid</p>
    </div> : null}
    {models.length ? <div className="mt-2 flex flex-wrap gap-2">{models.map((model, index) => <span key={String(model.classification_model_id ?? index)} className="inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs"><span>{String(model.field_name ?? model.field_key ?? `Classifier ${index + 1}`)}</span><StatusBadge status={String(model.status ?? (preparing ? "PREPARING_INPUTS" : "QUEUED"))} /></span>)}</div> : null}
  </div>;
}

function ProgramClassifierModels({ clientId, matterId, run }: { clientId: string; matterId: string; run: ReviewProgramRunRead }) {
  const progress = run.progress_summary && typeof run.progress_summary === "object" && !Array.isArray(run.progress_summary)
    ? run.progress_summary as Record<string, unknown>
    : {};
  const trainingRaw = progress.classifier_training;
  const training = trainingRaw && typeof trainingRaw === "object" && !Array.isArray(trainingRaw) ? trainingRaw as Record<string, unknown> : {};
  const resultsRaw = progress.classifier_results;
  const results = resultsRaw && typeof resultsRaw === "object" && !Array.isArray(resultsRaw) ? resultsRaw as Record<string, unknown> : {};
  const resultModels = Array.isArray(results.models) ? results.models : [];
  const trainingModels = Array.isArray(training.models) ? training.models : [];
  const sourceModels = resultModels.length ? resultModels : trainingModels;
  const labelsRaw = progress.training_labels;
  const labels = labelsRaw && typeof labelsRaw === "object" && !Array.isArray(labelsRaw) ? labelsRaw as Record<string, unknown> : {};
  const selectedIdsRaw = Array.isArray(labels.classifier_metadata_definition_ids)
    ? labels.classifier_metadata_definition_ids
    : Array.isArray(labels.selected_metadata_definition_ids)
      ? labels.selected_metadata_definition_ids
      : [];
  const selectedIds = new Set(selectedIdsRaw.map(String));
  const models = [...new Map(sourceModels
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item) && item.classification_model_id))
    .filter((item) => !selectedIds.size || selectedIds.has(String(item.metadata_definition_id)))
    .map((item) => [String(item.classification_model_id), item])).values()];
  if (!models.length) return null;
  return <Card>
    <CardHeader><CardTitle>Classifier models</CardTitle><CardDescription>Models created and managed by this Review Program.</CardDescription></CardHeader>
    <CardContent className="space-y-2">{models.map((model) => {
      const modelId = String(model.classification_model_id);
      const state = model.quality ?? model.status ?? results.overall_quality ?? training.state ?? "created";
      return <div key={modelId} className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"><div><p className="text-sm font-medium">{String(model.field_name ?? model.field_key ?? "Classifier")}</p><p className="text-xs text-muted-foreground">{friendly(String(state))}</p></div><Button asChild size="sm" variant="outline"><Link href={classificationModelHref(clientId, matterId, modelId)}>Open model</Link></Button></div>;
    })}</CardContent>
  </Card>;
}

function RefreshStateButton({ busy, onClick }: { busy: boolean; onClick: () => void }) {
  const tooltipId = "review-program-refresh-state-help";
  return <span className="group/refresh-state-tooltip relative inline-flex">
    <Button size="sm" variant="outline" disabled={busy} onClick={onClick} aria-describedby={tooltipId}><RefreshCw />Refresh state</Button>
    <span id={tooltipId} role="tooltip" className="pointer-events-none invisible absolute right-0 top-full z-50 mt-2 w-max max-w-[min(20rem,calc(100vw-2rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal leading-relaxed text-popover-foreground opacity-0 shadow-md transition-opacity group-hover/refresh-state-tooltip:visible group-hover/refresh-state-tooltip:opacity-100 group-focus-within/refresh-state-tooltip:visible group-focus-within/refresh-state-tooltip:opacity-100">
      Checks pending events and completed work, then updates the program if its recorded state is behind. It does not retry work.
    </span>
  </span>;
}

function ReplacementRunDialog({ run, busy, onConfirm }: { run: ReviewProgramRunRead; busy: boolean; onConfirm: () => void }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button size="sm" variant="outline" disabled={busy}><Play />Start replacement run</Button></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Start a replacement run?</DialogTitle>
        <DialogDescription>
          This program already has an unfinished run that failed during {friendly(run.current_stage)}. Starting a replacement will make a new run current; it will not retry or delete the existing run.
        </DialogDescription>
      </DialogHeader>
      {run.failure_retriable ? <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">This failure is retriable. Use Retry to continue the existing run from its failed stage unless you intentionally want to restart setup.</p> : null}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => setOpen(false)}>Keep existing run</Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={() => { setOpen(false); onConfirm(); }}>Start replacement run</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

function DiscoveryQAProgress({ clientId, matterId, run, canManage, busy, onCancel, onReset }: { clientId: string; matterId: string; run: ReviewProgramRunRead; canManage: boolean; busy: boolean; onCancel: () => void; onReset: () => void }) {
  const discovery = run.progress_summary?.discovery_qa;
  const values = discovery && typeof discovery === "object" && !Array.isArray(discovery)
    ? discovery as Record<string, unknown>
    : {};
  const batchSetId = typeof values.batch_set_id === "string" ? values.batch_set_id : "";
  const taskId = typeof values.task_id === "string" ? values.task_id : "";
  const batchSet = useQuery({
    queryKey: ["analysis-task-batch-set", matterId, taskId, batchSetId],
    queryFn: () => coreApi<AnalysisTaskBatchSetRead>(`/v1/matters/${matterId}/analysis-tasks/${taskId}/batch-sets/${batchSetId}`),
    enabled: Boolean(taskId && batchSetId),
    refetchInterval: (query) => ["QUEUED", "BUILDING"].includes(query.state.data?.status ?? "") ? 1_500 : false,
  });
  const jobs = useQuery({
    queryKey: ["matter-question-answering-jobs", matterId, batchSetId],
    queryFn: () => coreApi<MatterQuestionAnsweringJobRead[]>(`/v1/matters/${matterId}/analysis-tasks/question-answering-jobs?batch_set_id=${encodeURIComponent(batchSetId)}`),
    enabled: Boolean(batchSetId),
    refetchInterval: (query) => query.state.data?.some((job) => !TERMINAL_DISCOVERY_JOB_STATUSES.has(job.status)) ? 1_500 : false,
  });
  const linkedJobs = jobs.data ?? [];
  const batches = new Map<string, MatterQuestionAnsweringJobRead[]>();
  for (const job of linkedJobs) {
    const key = job.review_batch_id ?? job.scope_id;
    batches.set(key, [...(batches.get(key) ?? []), job]);
  }
  let completedBatches = 0;
  let runningBatches = 0;
  let queuedBatches = 0;
  for (const batchJobs of batches.values()) {
    const providers = new Set(batchJobs.map((job) => job.decision_provider));
    if (providers.size >= 2 && batchJobs.every((job) => TERMINAL_DISCOVERY_JOB_STATUSES.has(job.status))) completedBatches += 1;
    else if (batchJobs.some((job) => job.status === "RUNNING")) runningBatches += 1;
    else queuedBatches += 1;
  }
  const totalBatches = Math.max(batchSet.data?.batch_count ?? 0, batches.size);
  queuedBatches += Math.max(totalBatches - batches.size, 0);
  const successful = linkedJobs.reduce((total, job) => total + job.successful_document_count, 0);
  const materializedExpected = linkedJobs.reduce((total, job) => total + job.document_count, 0);
  const configuredProfileCount = Array.isArray(values.execution_profile_ids)
    ? values.execution_profile_ids.length
    : 0;
  const materializedProfileCount = new Set(linkedJobs.map((job) => job.decision_provider)).size;
  const profileCount = configuredProfileCount || materializedProfileCount;
  const populationCount = batchSet.data?.population_count ?? 0;
  const expected = populationCount > 0 && profileCount > 0
    ? populationCount * profileCount
    : materializedExpected;
  const failed = linkedJobs.reduce((total, job) => total + (job.failed_document_count ?? 0), 0);
  const providerTotals = linkedJobs.reduce<Record<string, { successful: number; total: number }>>((totals, job) => {
    const label = job.decision_provider === "OPENAI_DECISIONS" ? "OpenAI Decisions" : "Jev";
    const current = totals[label] ?? { successful: 0, total: 0 };
    totals[label] = { successful: current.successful + job.successful_document_count, total: current.total + job.document_count };
    return totals;
  }, {});
  if (populationCount > 0) {
    for (const counts of Object.values(providerTotals)) counts.total = populationCount;
  }
  const state = String(run.progress_summary?.discovery_qa_state ?? "RUNNING_PROFILES");
  const activity = state === "CANCELED" ? "Canceled"
    : !linkedJobs.length
    ? state === "MATERIALIZING_BATCH_SET" || batchSet.data?.status === "BUILDING" ? "Preparing batch set" : "Starting linked jobs"
    : failed > 0 && linkedJobs.every((job) => TERMINAL_DISCOVERY_JOB_STATUSES.has(job.status)) ? "Completed with errors"
    : "Reviewing documents";
  const jobsHref = matterJobsHref(clientId, matterId, "QUESTION_ANSWERING", { batch_set: batchSetId });

  return <div className="mt-3 border-t pt-3">
    <div className="grid gap-3 text-sm sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
      <div><p className="text-xs text-muted-foreground">Status</p><p className="font-medium">{activity}</p>{totalBatches ? <p className="mt-1 text-xs text-muted-foreground">{completedBatches} of {totalBatches} batches complete · {runningBatches} running · {queuedBatches} queued</p> : <p className="mt-1 text-xs text-muted-foreground">Waiting for linked batch jobs to be created.</p>}</div>
      <div><p className="text-xs text-muted-foreground">Model reviews</p><p className="font-medium tabular-nums">{successful.toLocaleString()} of {expected.toLocaleString()} successful</p><p className="mt-1 text-xs text-muted-foreground">{Object.entries(providerTotals).map(([provider, counts]) => `${provider} ${counts.successful.toLocaleString()}/${counts.total.toLocaleString()}`).join(" · ") || "No model progress yet"}{failed ? ` · ${failed.toLocaleString()} failed` : ""}</p></div>
      <div className="flex flex-wrap justify-end gap-2">
        <Button asChild size="sm" variant="outline"><Link href={jobsHref}><ListChecks />View batch progress</Link></Button>
        {canManage && state === "CANCELED" ? <ResetDiscoveryQADialog busy={busy} onReset={onReset} /> : null}
        {canManage && ["MATERIALIZING_BATCH_SET", "RUNNING_PROFILES"].includes(state) ? <CancelDiscoveryQADialog busy={busy} onCancel={onCancel} /> : null}
      </div>
    </div>
  </div>;
}

const TERMINAL_DISCOVERY_JOB_STATUSES = new Set(["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]);

function ResetDiscoveryQADialog({ busy, onReset }: { busy: boolean; onReset: () => void }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button size="sm" variant="outline" disabled={busy}><RotateCcw />Reset Discovery Q&amp;A</Button></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Reset Discovery Q&amp;A?</DialogTitle>
        <DialogDescription>This archives the canceled Discovery Q&amp;A Batch Set and child batches, then returns this stage to setup so it can be run again. Prior records remain available for audit.</DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => setOpen(false)}>Keep current run</Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={() => { setOpen(false); onReset(); }}>Reset Discovery Q&amp;A</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

function CancelDiscoveryQADialog({ busy, onCancel }: { busy: boolean; onCancel: () => void }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button size="sm" variant="outline" disabled={busy}><Ban />Cancel Discovery Q&amp;A</Button></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Cancel Discovery Q&amp;A?</DialogTitle>
        <DialogDescription>This stops the active Batch Set and its linked model jobs. The canceled work remains visible until you choose to reset the task.</DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => setOpen(false)}>Keep running</Button>
        <Button type="button" variant="destructive" disabled={busy} onClick={() => { setOpen(false); onCancel(); }}>Cancel Discovery Q&amp;A</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}

function ProgramStageTooltip({ stage }: { stage: ReviewWorkflowStageRead }) {
  const tooltipId = `review-program-stage-${stage.ordinal}-help`;
  const description = PROGRAM_STAGE_HELP[stage.key]
    ?? `${stage.name} is a ${friendly(effectiveStageBatchingPolicy(stage))} stage${stage.requires_human_gate ? " that requires a user decision" : ""}.`;
  return <span className="group/stage-tooltip relative inline-flex shrink-0">
    <button type="button" className="rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" aria-label={`About ${stage.name}`} aria-describedby={tooltipId}>
      <CircleHelp className="size-4" aria-hidden="true" />
    </button>
    <span id={tooltipId} role="tooltip" className="pointer-events-none invisible absolute right-0 top-6 z-50 w-max max-w-[min(18rem,calc(100vw-2rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal leading-relaxed text-popover-foreground opacity-0 shadow-md transition-opacity group-hover/stage-tooltip:visible group-hover/stage-tooltip:opacity-100 group-focus-within/stage-tooltip:visible group-focus-within/stage-tooltip:opacity-100">
      {description}
    </span>
  </span>;
}

function DecisionChoiceButton({ choice, disabled, onClick, variant = "default" }: {
  choice: ReviewProgramActionRead["allowed_choices"][number];
  disabled: boolean;
  onClick: () => void;
  variant?: "default" | "outline" | "destructive";
}) {
  const tooltipId = `review-program-choice-${choice.key.toLowerCase()}-help`;
  return <span className="group/decision-tooltip relative inline-flex">
    <Button size="sm" variant={variant} disabled={disabled} onClick={onClick} aria-describedby={tooltipId}>{choice.label}</Button>
    <span id={tooltipId} role="tooltip" className="pointer-events-none invisible absolute bottom-full left-0 z-50 mb-2 w-max max-w-[min(20rem,calc(100vw-2rem))] rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal leading-relaxed text-popover-foreground opacity-0 shadow-md transition-opacity group-hover/decision-tooltip:visible group-hover/decision-tooltip:opacity-100 group-focus-within/decision-tooltip:visible group-focus-within/decision-tooltip:opacity-100">
      {choice.consequence}
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

function IssueSupportSummary({ action, matterId, run }: { action: ReviewProgramActionRead; matterId: string; run: ReviewProgramRunRead | null }) {
  if (action.stage_key !== "ISSUE_SUPPORT_DECISION") return null;
  const raw = action.decision_context?.discovery_analysis;
  const analysis = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  return <DiscoveryAnalysisSummary analysis={analysis} matterId={matterId} run={run} />;
}

function DiscoveryAnalysisReplay({ matterId, run, attempt }: { matterId: string; run: ReviewProgramRunRead; attempt: ReviewProgramStageAttemptRead }) {
  const nested = attempt.result_summary?.discovery_analysis;
  const analysis = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : attempt.result_summary ?? {};
  return <DiscoveryAnalysisSummary analysis={analysis} matterId={matterId} run={run} />;
}

function WholeCollectionScoringReplay({ clientId, matterId, attempt }: { clientId: string; matterId: string; attempt: ReviewProgramStageAttemptRead }) {
  const nested = attempt.result_summary?.whole_collection_scoring;
  const result = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : attempt.result_summary ?? {};
  const models = Array.isArray(result.models)
    ? result.models.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const aggregateRaw = result.aggregate_model_document_counts;
  const aggregate = aggregateRaw && typeof aggregateRaw === "object" && !Array.isArray(aggregateRaw)
    ? aggregateRaw as Record<string, unknown>
    : {};
  if (!models.length) return <div className="rounded-lg border bg-background/70 p-4 text-sm text-muted-foreground">This attempt did not produce a whole-matter scoring result. Select a completed attempt to review its outcome.</div>;
  return <div className="space-y-4 rounded-lg border bg-background/70 p-4">
    <div><p className="font-semibold">Whole-matter scoring outcome</p><p className="text-sm text-muted-foreground">Review the population totals, then open each classifier’s prediction distributions and threshold analysis.</p></div>
    <div className="grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-5">
      <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Population</p><p className="font-medium tabular-nums">{Number(result.collection_document_count ?? aggregate.population_document_count ?? 0).toLocaleString()}</p></div>
      <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Scored</p><p className="font-medium tabular-nums">{Number(aggregate.scored_document_count ?? 0).toLocaleString()}</p></div>
      <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Abstained</p><p className="font-medium tabular-nums">{Number(aggregate.abstained_document_count ?? 0).toLocaleString()}</p></div>
      <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Excluded</p><p className="font-medium tabular-nums">{Number(aggregate.excluded_document_count ?? 0).toLocaleString()}</p></div>
      <div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Failed</p><p className="font-medium tabular-nums">{Number(aggregate.failed_document_count ?? 0).toLocaleString()}</p></div>
    </div>
    <div className="space-y-2">{models.map((model, index) => {
      const modelId = String(model.classification_model_id ?? "");
      const scoringRunId = String(model.scoring_run_id ?? "");
      return <div key={scoringRunId || modelId || index} className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3">
        <div><div className="flex flex-wrap items-center gap-2"><p className="font-medium">{String(model.field_name ?? model.field_key ?? `Classifier ${index + 1}`)}</p><StatusBadge status={String(model.status ?? result.state ?? "COMPLETED")} /></div><p className="mt-1 text-xs text-muted-foreground">{Number(model.scored_document_count ?? 0).toLocaleString()} scored · {Number(model.abstained_document_count ?? 0).toLocaleString()} abstained · {Number(model.excluded_document_count ?? 0).toLocaleString()} excluded · {Number(model.failed_document_count ?? 0).toLocaleString()} failed</p></div>
        {modelId && scoringRunId ? <Button asChild size="sm" variant="outline"><Link href={classificationScoringRunHref(clientId, matterId, modelId, scoringRunId)} target="_blank" rel="noopener noreferrer"><BarChart3 />View scoring results</Link></Button> : null}
      </div>;
    })}</div>
  </div>;
}

function DiscoveryAnalysisSources({ analysis, matterId, run }: { analysis: Record<string, unknown>; matterId: string; run: ReviewProgramRunRead | null }) {
  const recordedSources = Array.isArray(analysis.source_batches)
    ? analysis.source_batches.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item) && typeof (item as Record<string, unknown>).id === "string"))
    : [];
  const profileRunIds = Array.isArray(analysis.profile_run_ids)
    ? new Set(analysis.profile_run_ids.filter((value): value is string => typeof value === "string"))
    : new Set<string>();
  const discoveryBatchSets = (run?.stage_attempts ?? []).flatMap((stageAttempt) => {
    if (stageAttempt.stage_key !== "DISCOVERY_QA") return [];
    const nested = stageAttempt.result_summary?.discovery_qa;
    const values = nested && typeof nested === "object" && !Array.isArray(nested) ? nested as Record<string, unknown> : stageAttempt.result_summary ?? {};
    const taskId = typeof values.task_id === "string" ? values.task_id : "";
    const batchSetId = typeof values.batch_set_id === "string" ? values.batch_set_id : "";
    return taskId && batchSetId ? [{ taskId, batchSetId }] : [];
  }).filter((value, index, values) => values.findIndex((other) => other.batchSetId === value.batchSetId) === index);
  const legacyQueries = useQueries({
    queries: discoveryBatchSets.map(({ taskId, batchSetId }) => ({
      queryKey: ["analysis-task-batch-set-runs", matterId, taskId, batchSetId, "discovery-analysis-sources"],
      queryFn: () => coreApi<AnalysisTaskBatchSetRunRead[]>(`/v1/matters/${matterId}/analysis-tasks/${taskId}/batch-sets/${batchSetId}/runs`),
      enabled: recordedSources.length === 0 && profileRunIds.size > 0,
    })),
  });
  const derivedBatchIds = [...new Set(legacyQueries.flatMap((query) => (query.data ?? [])
    .filter((profileRun) => profileRunIds.has(profileRun.id))
    .flatMap((profileRun) => profileRun.children ?? [])
    .map((child) => child.review_batch_id)))];
  const sources = recordedSources.length ? recordedSources : derivedBatchIds.map((id, index) => ({ id, name: `Discovery batch ${index + 1}` }));
  const loading = recordedSources.length === 0 && legacyQueries.some((query) => query.isPending);

  return <div className="space-y-2 rounded-md border bg-muted/20 p-3">
    <div><p className="text-sm font-semibold">Source analysis data</p><p className="text-xs text-muted-foreground">Open the retained coding comparison and document matrix for each batch included in this analysis.</p></div>
    {loading ? <p className="text-xs text-muted-foreground">Loading source batches…</p> : sources.length ? <div className="flex flex-wrap gap-2">{sources.map((source, index) => {
      const id = String(source.id);
      const documentCount = "document_count" in source && typeof source.document_count === "number" ? source.document_count : null;
      return <Button key={id} asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}/batches/${id}/statistics`} target="_blank" rel="noopener noreferrer"><ListChecks />{String(source.name ?? `Discovery batch ${index + 1}`)}{documentCount != null ? ` · ${documentCount.toLocaleString()} documents` : ""}</Link></Button>;
    })}</div> : <p className="text-xs text-muted-foreground">Source-batch links were not retained for this historical analysis.</p>}
  </div>;
}

function DiscoveryAnalysisSummary({ analysis, matterId, run }: { analysis: Record<string, unknown>; matterId: string; run: ReviewProgramRunRead | null }) {
  const overallRaw = analysis.overall;
  const overall = overallRaw && typeof overallRaw === "object" && !Array.isArray(overallRaw) ? overallRaw as Record<string, unknown> : {};
  const policyRaw = analysis.policy;
  const policy = policyRaw && typeof policyRaw === "object" && !Array.isArray(policyRaw) ? policyRaw as Record<string, unknown> : {};
  const fields = Array.isArray(analysis.fields)
    ? analysis.fields.filter((field): field is Record<string, unknown> => Boolean(field && typeof field === "object" && !Array.isArray(field)))
    : [];
  const lowSupport = Array.isArray(analysis.low_support) ? analysis.low_support : [];
  const positiveMinimum = Number(policy.minimum_positive_support ?? 0);
  const negativeMinimum = Number(policy.minimum_negative_support ?? 0);
  const analyzedProfiles = Array.isArray(analysis.execution_profiles)
    ? analysis.execution_profiles.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const acceptedTaskRaw = run?.progress_summary?.accepted_qa_task;
  const acceptedTask = acceptedTaskRaw && typeof acceptedTaskRaw === "object" && !Array.isArray(acceptedTaskRaw) ? acceptedTaskRaw as Record<string, unknown> : {};
  const acceptedProfiles = Array.isArray(acceptedTask.execution_profiles)
    ? acceptedTask.execution_profiles.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const acceptedById = new Map(acceptedProfiles.map((profile) => [String(profile.id ?? ""), profile]));
  const orderedProfiles = analyzedProfiles.length
    ? analyzedProfiles
    : [...acceptedProfiles].sort((left, right) => String(left.id ?? "").localeCompare(String(right.id ?? "")));
  const modelLabels = [0, 1].map((index) => {
    const analyzed = orderedProfiles[index] ?? {};
    const accepted = acceptedById.get(String(analyzed.execution_profile_id ?? analyzed.id ?? "")) ?? {};
    return String(
      analyzed.resolved_model
      ?? accepted.resolved_model
      ?? analyzed.name
      ?? accepted.name
      ?? analyzed.model_key
      ?? accepted.model_key
      ?? friendly(String(analyzed.decision_provider ?? accepted.decision_provider ?? `Pinned model ${index + 1}`)),
    );
  });
  return <div className="mb-4 space-y-4 rounded-lg border bg-background/70 p-3 text-sm">
    <div>
      <h3 className="font-semibold">Discovery analysis</h3>
      <p className="text-xs text-muted-foreground">Frozen comparison of the two pinned Q&amp;A profiles across {Number(analysis.batch_count ?? 0).toLocaleString()} batches.</p>
    </div>
    <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <div><p className="text-xs text-muted-foreground">Support</p><p className="font-medium">{friendly(String(analysis.support_status ?? "unknown"))}</p></div>
      <div><p className="text-xs text-muted-foreground">Exact</p><p className="font-medium tabular-nums">{Number(overall.exact_count ?? 0).toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Partial</p><p className="font-medium tabular-nums">{Number(overall.partial_count ?? 0).toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Disagreements</p><p className="font-medium tabular-nums">{Number(overall.disagreement_count ?? 0).toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Incomplete</p><p className="font-medium tabular-nums">{Number(overall.incomplete_count ?? 0).toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Low-support categories</p><p className="font-medium tabular-nums">{lowSupport.length.toLocaleString()}</p></div>
    </div>
    <div className="rounded-md border bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
      Minimum support per category: <span className="font-medium text-foreground">{positiveMinimum.toLocaleString()} positive</span> and <span className="font-medium text-foreground">{negativeMinimum.toLocaleString()} negative</span> examples. Positive means either model selected the category; negative means neither selected it in a complete comparison.
    </div>
    <DiscoveryAnalysisSources analysis={analysis} matterId={matterId} run={run} />
    {fields.length ? <div className="space-y-4">{fields.map((field) => {
      const classes = Array.isArray(field.classes)
        ? field.classes.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
        : [];
      const fieldKey = String(field.key ?? field.metadata_definition_id ?? "field");
      return <section key={fieldKey} className="space-y-2 rounded-md border bg-background p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><h4 className="font-semibold">{String(field.display_name ?? friendly(fieldKey))}</h4><p className="font-mono text-[11px] text-muted-foreground">{fieldKey}</p></div>
          <p className="text-xs tabular-nums text-muted-foreground">{Number(field.exact_count ?? 0).toLocaleString()} exact · {Number(field.partial_count ?? 0).toLocaleString()} partial · {Number(field.disagreement_count ?? 0).toLocaleString()} disagreements · {Number(field.incomplete_count ?? 0).toLocaleString()} incomplete</p>
        </div>
        <Table>
          <TableHeader><TableRow><TableHead>Category</TableHead><TableHead className="text-right">{modelLabels[0]}</TableHead><TableHead className="text-right">{modelLabels[1]}</TableHead><TableHead className="text-right">Both</TableHead><TableHead className="text-right">Positive</TableHead><TableHead className="text-right">Negative</TableHead><TableHead className="text-right">Differences</TableHead><TableHead>Support</TableHead></TableRow></TableHeader>
          <TableBody>{classes.map((item) => {
            const valueKey = String(item.value_key ?? "unknown");
            const low = String(item.support_status ?? "").toUpperCase() === "LOW_SUPPORT";
            return <TableRow key={valueKey} className={cn(low && "bg-destructive/5")}>
              <TableCell className="font-medium">{friendly(valueKey)}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.left_selected_count ?? 0).toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.right_selected_count ?? 0).toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.intersection_count ?? 0).toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.union_count ?? 0).toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.negative_count ?? 0).toLocaleString()}</TableCell>
              <TableCell className="text-right tabular-nums">{Number(item.disagreement_count ?? 0).toLocaleString()}</TableCell>
              <TableCell><Badge variant="outline" className={cn(low && "border-destructive/40 text-destructive")}>{low ? "Low support" : "Sufficient"}</Badge></TableCell>
            </TableRow>;
          })}</TableBody>
        </Table>
      </section>;
    })}</div> : <p className="text-xs text-muted-foreground">Category-level discovery statistics were not recorded for this analysis.</p>}
  </div>;
}

function TrainingLabelPolicyCard({ action, matterId, canManage, busy, onResolve }: {
  action: ReviewProgramActionRead;
  matterId: string;
  canManage: boolean;
  busy: boolean;
  onResolve: (choice: string, input?: Record<string, unknown>) => void;
}) {
  const raw = action.decision_context?.discovery_analysis;
  const analysis = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const fields = Array.isArray(analysis.fields)
    ? analysis.fields.filter((field): field is Record<string, unknown> => Boolean(field && typeof field === "object" && !Array.isArray(field)))
    : [];
  const reviewableFields = fields.filter((field) => {
    const id = field.metadata_definition_id;
    const differences = Number(field.partial_count ?? 0) + Number(field.disagreement_count ?? 0);
    return typeof id === "string" && Boolean(id) && differences > 0;
  });
  const [selectedFields, setSelectedFields] = useState(() => new Set(reviewableFields.map((field) => String(field.metadata_definition_id))));
  const reviewChoice = action.allowed_choices.find((choice) => choice.key === "REVIEW_DISAGREEMENTS");
  const otherChoices = action.allowed_choices.filter((choice) => choice.key !== "REVIEW_DISAGREEMENTS");
  function toggleField(id: string) {
    setSelectedFields((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  return <Card className="border-accent/60 bg-accent/5">
    <CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      <TrainingLabelSummary action={action} matterId={matterId} />
      {reviewChoice ? <div className="space-y-3 rounded-lg border bg-background/70 p-3">
        <div><p className="text-sm font-semibold">Fields to include in human difference review</p><p className="text-xs text-muted-foreground">One consolidated ad hoc review batch will be created for each selected field, combining disagreements from every discovery batch.</p></div>
        {reviewableFields.length ? <div className="grid gap-2 sm:grid-cols-2">{reviewableFields.map((field) => {
          const id = String(field.metadata_definition_id);
          const differences = Number(field.partial_count ?? 0) + Number(field.disagreement_count ?? 0);
          return <label key={id} className="flex cursor-pointer items-center justify-between gap-3 rounded-md border bg-background px-3 py-2 text-sm">
            <span className="flex items-center gap-2"><input type="checkbox" checked={selectedFields.has(id)} disabled={!canManage || busy} onChange={() => toggleField(id)} /><span className="font-medium">{String(field.display_name ?? friendly(String(field.key ?? "Field")))}</span></span>
            <span className="text-xs tabular-nums text-muted-foreground">{differences.toLocaleString()} differences</span>
          </label>;
        })}</div> : <p className="text-xs text-muted-foreground">No fields with model differences are available for human review.</p>}
        <DecisionChoiceButton choice={reviewChoice} disabled={!canManage || busy || selectedFields.size === 0} onClick={() => onResolve(reviewChoice.key, { metadata_definition_ids: [...selectedFields] })} />
      </div> : null}
      <div className="flex flex-wrap gap-2">{otherChoices.map((choice) => <DecisionChoiceButton key={choice.key} choice={choice} variant={choice.key === "STOP" ? "destructive" : "outline"} disabled={!canManage || busy} onClick={() => onResolve(choice.key)} />)}</div>
    </CardContent>
  </Card>;
}

function TrainingLabelFinalizationCard({ action, matterId, canManage, busy, onResolve }: {
  action: ReviewProgramActionRead;
  matterId: string;
  canManage: boolean;
  busy: boolean;
  onResolve: (choice: string, input?: Record<string, unknown>) => void;
}) {
  const previewRaw = action.decision_context?.training_label_preview;
  const preview = previewRaw && typeof previewRaw === "object" && !Array.isArray(previewRaw) ? previewRaw as Record<string, unknown> : {};
  const fields = Array.isArray(preview.fields)
    ? preview.fields.filter((field): field is Record<string, unknown> => Boolean(field && typeof field === "object" && !Array.isArray(field) && field.metadata_definition_id))
    : [];
  const defaultSelection = fields.map((field) => String(field.metadata_definition_id));
  const [selectedFields, setSelectedFields] = useState(() => new Set(defaultSelection));
  const finalizeChoice = action.allowed_choices.find((choice) => choice.key === "FINALIZE");
  const stopChoice = action.allowed_choices.find((choice) => choice.key === "STOP");
  function toggleField(id: string) {
    setSelectedFields((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  return <Card className="border-accent/60 bg-accent/5">
    <CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader>
    <CardContent className="space-y-4">
      <TrainingLabelSummary action={action} matterId={matterId} />
      <div className="space-y-3 rounded-lg border bg-background/70 p-3">
        <div><p className="text-sm font-semibold">Fields to train</p><p className="text-xs text-muted-foreground">Each selected field creates its own classifier. Unselected Q&amp;A coding fields remain available as codings but will not be trained or scored by a classifier.</p></div>
        {fields.length ? <div className="grid gap-2 sm:grid-cols-2">{fields.map((field) => {
          const id = String(field.metadata_definition_id);
          const name = String(field.display_name ?? friendly(String(field.key ?? "Field")));
          return <label key={id} className="flex cursor-pointer items-center gap-2 rounded-md border bg-background px-3 py-2 text-sm"><input type="checkbox" aria-label={`Train ${name}`} checked={selectedFields.has(id)} disabled={!canManage || busy} onChange={() => toggleField(id)} /><span className="font-medium">{name}</span></label>;
        })}</div> : <p className="text-xs text-destructive">No classifier fields are available.</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {finalizeChoice ? <DecisionChoiceButton choice={finalizeChoice} disabled={!canManage || busy || selectedFields.size === 0} onClick={() => onResolve(finalizeChoice.key, { classifier_metadata_definition_ids: [...selectedFields] })} /> : null}
        {stopChoice ? <DecisionChoiceButton choice={stopChoice} variant="destructive" disabled={!canManage || busy} onClick={() => onResolve(stopChoice.key)} /> : null}
      </div>
    </CardContent>
  </Card>;
}

function TrainingLabelSummary({ action, matterId }: { action: ReviewProgramActionRead; matterId: string }) {
  if (action.stage_key !== "DISAGREEMENT_REVIEW" && action.stage_key !== "TRAINING_LABEL_FINALIZATION") return null;
  const analysisRaw = action.decision_context?.discovery_analysis;
  const analysis = analysisRaw && typeof analysisRaw === "object" && !Array.isArray(analysisRaw) ? analysisRaw as Record<string, unknown> : {};
  const overallRaw = analysis.overall;
  const overall = overallRaw && typeof overallRaw === "object" && !Array.isArray(overallRaw) ? overallRaw as Record<string, unknown> : {};
  const resolutionRaw = action.decision_context?.training_label_resolution;
  const resolution = resolutionRaw && typeof resolutionRaw === "object" && !Array.isArray(resolutionRaw) ? resolutionRaw as Record<string, unknown> : {};
  const previewRaw = action.decision_context?.training_label_preview;
  const preview = previewRaw && typeof previewRaw === "object" && !Array.isArray(previewRaw) ? previewRaw as Record<string, unknown> : {};
  const previewFields = Array.isArray(preview.fields)
    ? preview.fields.filter((field): field is Record<string, unknown> => Boolean(field && typeof field === "object" && !Array.isArray(field)))
    : [];
  const batches = Array.isArray(resolution.difference_batches)
    ? resolution.difference_batches.filter((batch): batch is Record<string, unknown> => Boolean(batch && typeof batch === "object" && !Array.isArray(batch)))
    : [];
  const profileRuns = Array.isArray(resolution.profile_run_ids) ? resolution.profile_run_ids : [];
  const analyzedProfileRuns = Array.isArray(analysis.profile_run_ids) ? analysis.profile_run_ids : [];
  const policy = resolution.policy ? friendly(String(resolution.policy)) : "Not selected";
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Label policy</p><p className="font-medium">{policy}</p></div><div><p className="text-xs text-muted-foreground">Pinned model runs</p><p className="font-medium">{profileRuns.length || analyzedProfileRuns.length}</p></div><div><p className="text-xs text-muted-foreground">Exact agreements</p><p className="font-medium">{Number(overall.exact_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Model differences</p><p className="font-medium">{Number(overall.partial_count ?? 0) + Number(overall.disagreement_count ?? 0)}</p></div></div>
    {action.stage_key === "TRAINING_LABEL_FINALIZATION" && previewFields.length ? <div className="space-y-3">
      <div><p className="text-sm font-semibold">Final training-label counts</p><p className="text-xs text-muted-foreground">These policy-adjusted counts will be frozen for classifier training. A positive count is the number of retained documents carrying that coding value.</p></div>
      {previewFields.map((field) => {
        const values = Array.isArray(field.values)
          ? field.values.filter((value): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value)))
          : [];
        const eligible = typeof field.eligible_document_count === "number" ? field.eligible_document_count.toLocaleString() : "Pending recomputation";
        const excluded = typeof field.excluded_document_count === "number" ? field.excluded_document_count.toLocaleString() : "Pending recomputation";
        return <section key={String(field.metadata_definition_id ?? field.key)} className="space-y-2 rounded-md border bg-background p-3">
          <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="font-semibold">{String(field.display_name ?? friendly(String(field.key ?? "Field")))}</p><p className="text-xs text-muted-foreground">{String(field.basis ?? "Selected training-label policy")}</p></div><p className="text-xs tabular-nums text-muted-foreground">{eligible} eligible · {excluded} excluded</p></div>
          <Table><TableHeader><TableRow><TableHead>Coding label</TableHead><TableHead className="text-right">Retained positive documents</TableHead></TableRow></TableHeader><TableBody>{values.map((value) => <TableRow key={String(value.value_key)}><TableCell>{String(value.value_label ?? friendly(String(value.value_key ?? "Unknown")))}</TableCell><TableCell className="text-right tabular-nums">{typeof value.positive_count === "number" ? value.positive_count.toLocaleString() : "Pending recomputation"}</TableCell></TableRow>)}</TableBody></Table>
        </section>;
      })}
    </div> : null}
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
  const errorsRaw = action.decision_context?.classifier_positive_coverage_errors;
  const errors = errorsRaw && typeof errorsRaw === "object" && !Array.isArray(errorsRaw) ? errorsRaw as Record<string, unknown> : null;
  if (errors) {
    const population = Number(errors.coverage_population_document_count ?? 0);
    const unresolved = Number(errors.unresolved_document_count ?? 0);
    const rate = Number(errors.unresolved_document_rate ?? 0);
    const tolerance = Number(errors.failure_tolerance ?? 0.005);
    const profiles = Array.isArray(errors.profile_runs)
      ? errors.profile_runs.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
      : [];
    return <div className="mb-4 space-y-3 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
      <div className="grid gap-2 sm:grid-cols-4"><div><p className="text-xs text-muted-foreground">Original population</p><p className="font-medium">{population.toLocaleString()}</p></div><div><p className="text-xs text-muted-foreground">Unresolved documents</p><p className="font-medium">{unresolved.toLocaleString()}</p></div><div><p className="text-xs text-muted-foreground">Failure rate</p><p className="font-medium">{(rate * 100).toFixed(2)}%</p></div><div><p className="text-xs text-muted-foreground">Tolerance</p><p className="font-medium">Less than {(tolerance * 100).toFixed(1)}%</p></div></div>
      {profiles.length ? <div className="space-y-2">{profiles.map((profile) => <div key={String(profile.id ?? profile.execution_profile_id)} className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background p-2"><div><p className="font-medium">{String(profile.model_name ?? profile.profile_key ?? "Q&A model")}</p><p className="text-xs text-muted-foreground">{Number(profile.completed_document_count ?? 0).toLocaleString()} completed · {Number(profile.reused_document_count ?? 0).toLocaleString()} reused · {Number(profile.failed_document_count ?? 0).toLocaleString()} failed{Number(profile.run_count ?? 0) > 1 ? ` across ${Number(profile.run_count).toLocaleString()} runs` : ""}</p></div><StatusBadge status={String(profile.status ?? "COMPLETED_WITH_ERRORS")} /></div>)}</div> : null}
      <p className="text-xs text-muted-foreground">Retry creates a replacement batch for only the unresolved documents. Accepting records them as a limitation and continues without treating request fragments as additional failed documents.</p>
    </div>;
  }
  const raw = action.decision_context?.coverage_plan;
  const plan = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    <div className="grid gap-2 sm:grid-cols-3"><div><p className="text-xs text-muted-foreground">Required reviewers</p><p className="font-medium">{Number(plan.required_profile_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Candidate ceiling</p><p className="font-medium">{Number(plan.maximum_candidate_model_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Batching</p><p className="font-medium">Ad hoc batch</p></div></div>
    <p className="text-xs text-muted-foreground">The exact population is calculated from successful, non-abstained positive predictions and current-content Q&A results when you start coverage. Existing documents reviewed by both pinned profiles are excluded.</p>
  </div>;
}

function ClassifierPositiveCoverageDecisionCard({ action, canManage, busy, onSubmit, onStop }: {
  action: ReviewProgramActionRead;
  canManage: boolean;
  busy: boolean;
  onSubmit: (minimumConfidence: number) => void;
  onStop: () => void;
}) {
  const raw = action.decision_context?.coverage_plan;
  const plan = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  const initial = Number(plan.default_minimum_confidence ?? 0.5) * 100;
  const [minimumConfidence, setMinimumConfidence] = useState(String(initial));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const percentage = Number(minimumConfidence);
    if (!Number.isFinite(percentage) || percentage < 0 || percentage > 100) {
      toast.error("Minimum classifier confidence must be between 0% and 100%.");
      return;
    }
    onSubmit(percentage / 100);
  };
  return <Card className="border-accent/60 bg-accent/5"><CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>{action.explanation}</CardDescription></CardHeader><CardContent><form className="space-y-4" onSubmit={submit}><WholeCollectionScoringSummary action={action} /><ClassifierPositiveCoverageSummary action={action} /><div className="max-w-sm space-y-2"><Label htmlFor="coverage-minimum-confidence">Minimum classifier confidence %</Label><Input id="coverage-minimum-confidence" type="number" min={0} max={100} step={1} value={minimumConfidence} onChange={(event) => setMinimumConfidence(event.target.value)} required disabled={!canManage || busy} /><p className="text-xs text-muted-foreground">A document is eligible when at least one positive classifier score meets this inclusive threshold. This controls Q&A coverage only; it does not change saved classifier codings.</p></div><div className="flex flex-wrap gap-2"><Button type="submit" disabled={!canManage || busy}>{busy ? "Starting…" : "Run dual-Q&A coverage"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div></form></CardContent></Card>;
}

function ClassifierPositiveCoverageComparisonReplay({ run, clientId, matterId }: { run: ReviewProgramRunRead; clientId: string; matterId: string }) {
  const raw = run.progress_summary?.final_alignment;
  const report = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  if (!Object.keys(report).length) return null;
  return <FinalAlignmentResults report={report} run={run} clientId={clientId} matterId={matterId} title="Classifier and decision-model comparison" showIssueBreakdown />;
}

function reviewProgramQAModelNames(run: ReviewProgramRunRead | null): [string, string] {
  const taskRaw = run?.progress_summary?.accepted_qa_task;
  const task = taskRaw && typeof taskRaw === "object" && !Array.isArray(taskRaw) ? taskRaw as Record<string, unknown> : {};
  const profiles = Array.isArray(task.execution_profiles)
    ? task.execution_profiles.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const names = profiles.slice(0, 2).map((profile) => String(profile.resolved_model ?? profile.model_key ?? profile.key ?? "Q&A model"));
  return [names[0] ?? "Q&A model 1", names[1] ?? "Q&A model 2"];
}

function FinalAlignmentResults({ report, run, clientId, matterId, title, showIssueBreakdown = false }: {
  report: Record<string, unknown>;
  run: ReviewProgramRunRead | null;
  clientId: string;
  matterId: string;
  title?: string;
  showIssueBreakdown?: boolean;
}) {
  const alignmentRaw = report.alignment_counts;
  const alignment = alignmentRaw && typeof alignmentRaw === "object" && !Array.isArray(alignmentRaw) ? alignmentRaw as Record<string, unknown> : {};
  const sourceRaw = report.source_coverage_counts;
  const source = sourceRaw && typeof sourceRaw === "object" && !Array.isArray(sourceRaw) ? sourceRaw as Record<string, unknown> : {};
  const coveragePolicyRaw = report.qa_coverage_policy;
  const coveragePolicy = coveragePolicyRaw && typeof coveragePolicyRaw === "object" && !Array.isArray(coveragePolicyRaw) ? coveragePolicyRaw as Record<string, unknown> : {};
  const liveCoverageRaw = run?.progress_summary?.classifier_positive_coverage;
  const liveCoverage = liveCoverageRaw && typeof liveCoverageRaw === "object" && !Array.isArray(liveCoverageRaw) ? liveCoverageRaw as Record<string, unknown> : {};
  const issues = Array.isArray(report.issues)
    ? report.issues.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const [profileAName, profileBName] = reviewProgramQAModelNames(run);
  const currentQACount = Number(source.CURRENT ?? 0);
  const incompleteProfileCount = Number(source.PARTIAL_OR_MISSING_PROFILE ?? 0) + Number(source.STALE_OR_PARTIAL ?? 0);
  const sourceMismatchCount = Number(source.SOURCE_VERSION_MISMATCH ?? 0);
  const missingQACount = Number(source.MISSING_QA ?? 0);
  const sourceUnavailableCount = Number(source.SOURCE_UNAVAILABLE ?? 0);
  const selectedCoverageCount = Number(coveragePolicy.selected_document_count ?? liveCoverage.positive_document_count ?? 0);
  const minimumConfidence = typeof report.minimum_confidence === "number"
    ? report.minimum_confidence
    : typeof coveragePolicy.minimum_confidence === "number"
      ? coveragePolicy.minimum_confidence
      : typeof liveCoverage.minimum_confidence === "number" ? liveCoverage.minimum_confidence : null;
  const thresholdScoped = report.population_scope === "CLASSIFIER_POSITIVE_AT_OR_ABOVE_COVERAGE_THRESHOLD";
  const exactAllCount = Number(alignment.EXACT_ALL ?? 0);
  const qaPairExactCount = Number(alignment.QA_PAIR_EXACT ?? 0);
  const partialOverlapCount = Number(alignment.PARTIAL ?? 0);
  const noSharedCodingCount = Number(alignment.DISAGREEMENT ?? 0);
  const incompleteComparisonCount = Number(alignment.INCOMPLETE ?? 0);
  if (!thresholdScoped) {
    return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
      <div><p className="text-xs text-muted-foreground">Classifier-positive documents at or above {minimumConfidence != null ? `${(minimumConfidence * 100).toFixed(0)}%` : "the selected threshold"}</p><p className="font-medium">{selectedCoverageCount.toLocaleString()}</p></div>
      {run ? <Button asChild type="button" size="sm" variant="outline"><Link href={reviewProgramQAPopulationStatisticsHref(clientId, matterId, run.program_id, run.id)} target="_blank" rel="noopener noreferrer"><ListChecks />View Q&amp;A population statistics</Link></Button> : null}
    </div>;
  }
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm">
    {title ? <div><div className="flex items-center gap-2"><BarChart3 className="size-4 text-muted-foreground" /><p className="font-medium">{title}</p></div><p className="mt-1 text-xs text-muted-foreground">Classifier predictions are compared with {profileAName} and {profileBName} across the classifier-positive population selected in Step 15.</p></div> : null}
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <div><p className="text-xs text-muted-foreground">{thresholdScoped ? `Classifier-positive documents at or above ${minimumConfidence != null ? `${(minimumConfidence * 100).toFixed(0)}%` : "the selected threshold"}` : "Documents included in this historical report"}</p><p className="font-medium">{Number(report.population_document_count ?? 0).toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Classifier and both Q&amp;A models exactly agree</p><p className="font-medium">{exactAllCount.toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Both Q&amp;A models agree; classifier differs</p><p className="font-medium">{qaPairExactCount.toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Some coding values overlap</p><p className="font-medium">{partialOverlapCount.toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">No coding values overlap</p><p className="font-medium">{noSharedCodingCount.toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Missing one or both Q&amp;A results</p><p className="font-medium">{incompleteComparisonCount.toLocaleString()}</p></div>
      <div><p className="text-xs text-muted-foreground">Issue categories compared</p><p className="font-medium">{issues.length.toLocaleString()}</p></div>
    </div>
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5"><div className="rounded-md border p-2"><p className="text-xs text-muted-foreground">Both Q&amp;A profiles complete</p><p className="font-medium">{currentQACount.toLocaleString()}</p></div><div className="rounded-md border p-2"><p className="text-xs text-muted-foreground">Incomplete dual-profile Q&amp;A</p><p className="font-medium">{incompleteProfileCount.toLocaleString()}</p></div><div className="rounded-md border p-2"><p className="text-xs text-muted-foreground">No Q&amp;A result</p><p className="font-medium">{missingQACount.toLocaleString()}</p></div><div className="rounded-md border p-2"><p className="text-xs text-muted-foreground">Source unavailable</p><p className="font-medium">{sourceUnavailableCount.toLocaleString()}</p></div><div className="rounded-md border p-2"><p className="text-xs text-muted-foreground">Source-version mismatch</p><p className="font-medium">{sourceMismatchCount.toLocaleString()}</p></div></div>
    {showIssueBreakdown && issues.length ? <div className="space-y-2"><p className="font-medium">Issue-level comparison</p><div className="overflow-x-auto rounded-md border"><Table><TableHeader><TableRow><TableHead>Issue</TableHead><TableHead className="text-right">Classifier</TableHead><TableHead className="text-right">{profileAName}</TableHead><TableHead className="text-right">{profileBName}</TableHead><TableHead className="text-right">Both decision models</TableHead><TableHead className="text-right">All three</TableHead></TableRow></TableHeader><TableBody>{issues.map((issue) => {
      const issueKey = String(issue.issue_key ?? "Issue");
      const issueLabel = friendly(issueKey.includes(":") ? issueKey.split(":").at(-1)! : issueKey);
      return <TableRow key={issueKey}><TableCell className="font-medium">{issueLabel}</TableCell><TableCell className="text-right tabular-nums">{Number(issue.classifier ?? 0).toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{Number(issue.profile_a ?? 0).toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{Number(issue.profile_b ?? 0).toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{Number(issue.qa_pair ?? 0).toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{Number(issue.all_three ?? 0).toLocaleString()}</TableCell></TableRow>;
    })}</TableBody></Table></div></div> : null}
    <p className="text-xs text-muted-foreground">{thresholdScoped ? `This report uses the same confidence threshold selected for classifier-positive Q&A coverage${selectedCoverageCount ? `, which selected ${selectedCoverageCount.toLocaleString()} documents` : ""}. “No Q&A result” means a selected document still lacks a usable result.` : `This historical report used the earlier broad population rule, so its population and agreement counts are not limited to the selected confidence threshold. “No Q&A result” can include documents outside the selected classifier-positive Q&A coverage policy${selectedCoverageCount ? `, which selected ${selectedCoverageCount.toLocaleString()} documents${minimumConfidence != null ? ` at or above ${(minimumConfidence * 100).toFixed(0)}% confidence` : ""}` : ""}.`} A source-version mismatch is reported separately and should be treated as an exception. Agreement measures consistency, not accuracy.</p>
    {run ? <div className="flex flex-wrap gap-2"><Button asChild type="button" size="sm" variant="outline"><Link href={reviewProgramFinalAlignmentHref(clientId, matterId, run.program_id, run.id)} target="_blank" rel="noopener noreferrer"><GitCompareArrows />View full comparison</Link></Button><Button asChild type="button" size="sm" variant="outline"><Link href={reviewProgramQAPopulationStatisticsHref(clientId, matterId, run.program_id, run.id)} target="_blank" rel="noopener noreferrer"><ListChecks />View Q&amp;A population statistics</Link></Button></div> : null}
  </div>;
}

function FinalAlignmentSummary({ action, run, clientId, matterId }: { action: ReviewProgramActionRead; run: ReviewProgramRunRead | null; clientId: string; matterId: string }) {
  if (action.stage_key !== "NO_CODE_QC") return null;
  const raw = action.decision_context?.final_alignment;
  const report = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  return <FinalAlignmentResults report={report} run={run} clientId={clientId} matterId={matterId} />;
}

interface NegativePopulationQCSetupPayload {
  preview_hash: string;
  inventory_content_hash: string;
  selected_procedures: NegativePopulationQCProcedure[];
  selected_record_types: string[];
  random_sample_size: number;
  near_threshold_sample_size: number;
  cohort_control_sample_size: number;
  mandatory_review_limit: number;
  keep_families_together: boolean;
}

type NegativePopulationQCProcedure = "RANDOM_AUDIT" | "NEAR_THRESHOLD" | "RECORD_TYPE_CONTROL" | "PROCESSING_EXCEPTIONS";

const NEGATIVE_QC_CARDS: Array<{
  key: NegativePopulationQCProcedure;
  title: string;
  description: string;
  route: string;
  inputId: string;
}> = [
  { key: "RANDOM_AUDIT", title: "Random negative audit", description: "A representative sample of ordinary no-code documents. Only this procedure supports a population-level observed-positive estimate.", route: "Dual Q&A", inputId: "qc-random" },
  { key: "NEAR_THRESHOLD", title: "High-risk no-code review", description: "Documents whose closest positive classifier score falls within the configured band below its decision threshold.", route: "Targeted review", inputId: "qc-threshold" },
  { key: "RECORD_TYPE_CONTROL", title: "Record-type control review", description: "Ordinary no-code documents grouped by record type, allowing controls for available document cohorts.", route: "Targeted review", inputId: "qc-cohort" },
  { key: "PROCESSING_EXCEPTIONS", title: "Processing-exception review", description: "Reviewable no-code documents with classifier, Q&A, abstention, or coverage exceptions. Source-unavailable documents remain remediation work.", route: "Exception resolution", inputId: "qc-mandatory" },
];

function NegativePopulationQCSetupCard({ action, run, clientId, matterId, canManage, busy, onPrepareInventory, onSubmit, onStop }: {
  action: ReviewProgramActionRead;
  run: ReviewProgramRunRead | null;
  clientId: string;
  matterId: string;
  canManage: boolean;
  busy: boolean;
  onPrepareInventory: () => void;
  onSubmit: (payload: NegativePopulationQCSetupPayload) => void;
  onStop: () => void;
}) {
  const defaultsRaw = action.decision_context?.defaults;
  const defaults = defaultsRaw && typeof defaultsRaw === "object" && !Array.isArray(defaultsRaw) ? defaultsRaw as Record<string, unknown> : {};
  const inventoryRaw = action.decision_context?.negative_population_qc_inventory;
  const inventory = inventoryRaw && typeof inventoryRaw === "object" && !Array.isArray(inventoryRaw) ? inventoryRaw as Record<string, unknown> : {};
  const proceduresRaw = inventory.procedures;
  const procedures = proceduresRaw && typeof proceduresRaw === "object" && !Array.isArray(proceduresRaw) ? proceduresRaw as Record<string, unknown> : {};
  const inventoryReady = inventory.state === "READY" && typeof inventory.input_manifest_content_hash === "string";
  const initialRandomAvailable = Number((procedures.RANDOM_AUDIT as Record<string, unknown> | undefined)?.available_count ?? 0);
  const [randomSample, setRandomSample] = useState(String(Math.min(Number(defaults.random_sample_size ?? 200), initialRandomAvailable || Number(defaults.random_sample_size ?? 200))));
  const [nearThreshold, setNearThreshold] = useState(String(defaults.near_threshold_sample_size ?? 100));
  const [cohortControl, setCohortControl] = useState(String(defaults.cohort_control_sample_size ?? 50));
  const [mandatoryLimit, setMandatoryLimit] = useState(String(defaults.mandatory_review_limit ?? 100));
  const [keepFamilies, setKeepFamilies] = useState(Boolean(defaults.keep_families_together ?? true));
  const [selectedProcedures, setSelectedProcedures] = useState<Set<NegativePopulationQCProcedure>>(() => new Set(initialRandomAvailable > 0 ? ["RANDOM_AUDIT"] : []));
  const [selectedRecordTypes, setSelectedRecordTypes] = useState<Set<string>>(new Set());
  const inventoryRequested = useRef(false);

  useEffect(() => {
    if (!inventoryReady && canManage && !busy && !inventoryRequested.current) {
      inventoryRequested.current = true;
      onPrepareInventory();
    }
  }, [busy, canManage, inventoryReady, onPrepareInventory]);

  const sampleValues: Record<NegativePopulationQCProcedure, string> = {
    RANDOM_AUDIT: randomSample,
    NEAR_THRESHOLD: nearThreshold,
    RECORD_TYPE_CONTROL: cohortControl,
    PROCESSING_EXCEPTIONS: mandatoryLimit,
  };
  const sampleSetters: Record<NegativePopulationQCProcedure, (value: string) => void> = {
    RANDOM_AUDIT: setRandomSample,
    NEAR_THRESHOLD: setNearThreshold,
    RECORD_TYPE_CONTROL: setCohortControl,
    PROCESSING_EXCEPTIONS: setMandatoryLimit,
  };
  function procedureDetail(key: NegativePopulationQCProcedure): Record<string, unknown> {
    const value = procedures[key];
    return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  }
  function toggleProcedure(key: NegativePopulationQCProcedure) {
    const next = new Set(selectedProcedures);
    if (next.has(key)) next.delete(key); else next.add(key);
    setSelectedProcedures(next);
    if (key === "RECORD_TYPE_CONTROL" && next.has(key) && selectedRecordTypes.size === 0) {
      const counts = procedureDetail(key).record_type_counts;
      if (counts && typeof counts === "object" && !Array.isArray(counts)) setSelectedRecordTypes(new Set(Object.keys(counts)));
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const values = [randomSample, nearThreshold, cohortControl, mandatoryLimit].map(Number);
    if (!inventoryReady || selectedProcedures.size === 0 || !values.every(Number.isInteger) || values.some((value) => value < 0)) return;
    onSubmit({
      preview_hash: action.preview_hash,
      inventory_content_hash: String(inventory.input_manifest_content_hash),
      selected_procedures: [...selectedProcedures],
      selected_record_types: selectedProcedures.has("RECORD_TYPE_CONTROL") ? [...selectedRecordTypes] : [],
      random_sample_size: selectedProcedures.has("RANDOM_AUDIT") ? values[0] : 0,
      near_threshold_sample_size: selectedProcedures.has("NEAR_THRESHOLD") ? values[1] : 0,
      cohort_control_sample_size: selectedProcedures.has("RECORD_TYPE_CONTROL") ? values[2] : 0,
      mandatory_review_limit: selectedProcedures.has("PROCESSING_EXCEPTIONS") ? values[3] : 0,
      keep_families_together: keepFamilies,
    });
  }
  return <Card className="border-accent/60 bg-accent/5">
    <CardHeader><CardTitle>{action.title}</CardTitle><CardDescription>Choose only the QC procedures that are useful for the frozen populations calculated below.</CardDescription></CardHeader>
    <CardContent><form className="space-y-4" onSubmit={submit}>
      <FinalAlignmentSummary action={action} run={run} clientId={clientId} matterId={matterId} />
      {!inventoryReady ? <div className="rounded-lg border bg-background/70 p-4"><p className="font-medium">{inventory.state === "FAILED" ? "QC population calculation failed" : "Calculating QC populations…"}</p><p className="mt-1 text-sm text-muted-foreground">{inventory.state === "FAILED" ? String(inventory.error_summary ?? "The inventory could not be prepared. Retry after correcting the reported problem.") : "The inventory evaluates classifier results, existing Q&A and human coding, source availability, thresholds, record types, and processing exceptions. No model calls are made."}</p><Button type="button" size="sm" variant="outline" className="mt-3" disabled={!canManage || busy || inventory.state === "PREPARING"} onClick={onPrepareInventory}><RefreshCw />{inventory.state === "FAILED" ? "Retry calculation" : "Calculate populations"}</Button></div> : <>
        <div className="grid gap-4 lg:grid-cols-2">{NEGATIVE_QC_CARDS.map((card) => {
          const detail = procedureDetail(card.key);
          const available = Number(detail.available_count ?? 0);
          const selected = selectedProcedures.has(card.key);
          const disabled = available === 0;
          const recordTypeCountsRaw = detail.record_type_counts;
          const recordTypeCounts = recordTypeCountsRaw && typeof recordTypeCountsRaw === "object" && !Array.isArray(recordTypeCountsRaw) ? recordTypeCountsRaw as Record<string, unknown> : {};
          const reasonCountsRaw = detail.reason_counts;
          const reasonCounts = reasonCountsRaw && typeof reasonCountsRaw === "object" && !Array.isArray(reasonCountsRaw) ? reasonCountsRaw as Record<string, unknown> : {};
          const remediationCount = Number(detail.source_remediation_count ?? 0);
          return <div key={card.key} className={cn("rounded-lg border bg-background/70 p-4", selected && "border-primary ring-1 ring-primary/20", disabled && "opacity-65")}>
            <label className="flex items-start gap-3"><input type="checkbox" className="mt-1" checked={selected} disabled={disabled || !canManage || busy} onChange={() => toggleProcedure(card.key)} /><span className="min-w-0 flex-1"><span className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">{card.title}</span><Badge variant={available ? "accent" : "outline"}>{available.toLocaleString()} available</Badge></span><span className="mt-1 block text-xs text-muted-foreground">{card.description}</span><span className="mt-2 block text-xs font-medium">Route: {card.route}</span></span></label>
            {selected ? <div className="mt-3 space-y-2"><Label htmlFor={card.inputId}>Documents to select</Label><Input id={card.inputId} type="number" min={1} max={Math.min(25000, available)} value={sampleValues[card.key]} onChange={(event) => sampleSetters[card.key](event.target.value)} /></div> : null}
            {card.key === "RECORD_TYPE_CONTROL" && Object.keys(recordTypeCounts).length ? <div className="mt-3 flex flex-wrap gap-2">{Object.entries(recordTypeCounts).map(([recordType, count]) => selected ? <label key={recordType} className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs"><input type="checkbox" checked={selectedRecordTypes.has(recordType)} onChange={() => { const next = new Set(selectedRecordTypes); if (next.has(recordType)) next.delete(recordType); else next.add(recordType); setSelectedRecordTypes(next); }} />{recordType} ({Number(count).toLocaleString()})</label> : <Badge key={recordType} variant="outline">{recordType}: {Number(count).toLocaleString()}</Badge>)}</div> : null}
            {card.key === "PROCESSING_EXCEPTIONS" && Object.keys(reasonCounts).length ? <div className="mt-3 flex flex-wrap gap-2">{Object.entries(reasonCounts).map(([reason, count]) => <Badge key={reason} variant="outline">{reason.replaceAll("_", " ").toLowerCase()}: {Number(count).toLocaleString()}</Badge>)}</div> : null}
            {card.key === "PROCESSING_EXCEPTIONS" && remediationCount > 0 ? <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">{remediationCount.toLocaleString()} additional source-unavailable documents require remediation and cannot be reviewed.</p> : null}
            {disabled ? <p className="mt-2 text-xs text-muted-foreground">This procedure is unavailable because the frozen inventory contains no eligible documents.</p> : null}
          </div>;
        })}</div>
        <div className="rounded-lg border bg-background/70 p-3 text-xs text-muted-foreground"><p>Population inventory: {Number(inventory.scoring_population_document_count ?? 0).toLocaleString()} documents in the frozen classifier population · {Number(inventory.excluded_positive_document_count ?? 0).toLocaleString()} excluded from negative QC because classifier, Q&amp;A, or human coding is positive. Near-threshold means within {Math.round(Number(inventory.near_threshold_band ?? 0) * 100)} percentage points below an applicable classifier threshold.</p></div>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => setKeepFamilies(event.target.checked)} disabled={!canManage || busy} />Keep document families together</label>
      </>}
      <div className="flex flex-wrap gap-2"><Button type="submit" disabled={!inventoryReady || selectedProcedures.size === 0 || !canManage || busy}>{busy ? "Working…" : "Start selected QC procedures"}</Button><Button type="button" variant="destructive" disabled={!canManage || busy} onClick={onStop}>Stop program</Button></div>
    </form></CardContent>
  </Card>;
}

function NegativePopulationQCReport({ action, matterId }: { action: ReviewProgramActionRead; matterId: string }) {
  if (action.action_type !== "NEGATIVE_POPULATION_QC_REPORT") return null;
  const qcRaw = action.decision_context?.negative_population_qc;
  const qc = qcRaw && typeof qcRaw === "object" && !Array.isArray(qcRaw) ? qcRaw as Record<string, unknown> : {};
  const reportRaw = qc.report;
  const report = reportRaw && typeof reportRaw === "object" && !Array.isArray(reportRaw) ? reportRaw as Record<string, unknown> : {};
  const estimateRaw = report.random_audit_estimate;
  const estimate = estimateRaw && typeof estimateRaw === "object" && !Array.isArray(estimateRaw) ? estimateRaw as Record<string, unknown> : {};
  const rate = typeof estimate.observed_positive_rate === "number" ? `${(estimate.observed_positive_rate * 100).toFixed(1)}%` : "Unavailable";
  const randomPositiveCount = Number(estimate.positive_count ?? 0);
  const randomResolvedCount = Number(estimate.resolved_count ?? 0);
  const reviewBatchId = String(qc.review_batch_id ?? report.review_batch_id ?? "");
  return <div className="mb-4 space-y-3 rounded-lg border bg-background/70 p-3 text-sm"><div className="grid gap-2 sm:grid-cols-5"><div><p className="text-xs text-muted-foreground">QC documents</p><p className="font-medium">{Number(report.selected_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">New positives · all lanes</p><p className="font-medium">{Number(report.positive_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Random-audit rate</p><p className="font-medium">{rate}</p>{randomResolvedCount ? <p className="text-xs text-muted-foreground">{randomPositiveCount} of {randomResolvedCount} resolved random documents</p> : null}</div><div><p className="text-xs text-muted-foreground">Unresolved documents</p><p className="font-medium">{Number(report.unresolved_document_count ?? 0)}</p></div><div><p className="text-xs text-muted-foreground">Manual resolution</p><p className="font-medium">{Number(report.mandatory_unreviewable_count ?? 0)}</p></div></div><p className="text-xs text-muted-foreground">Only the random-audit lane supports a population estimate. Targeted-lane yield identifies risk but is not a collection-wide error rate.</p>{reviewBatchId ? <Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}/batches/${reviewBatchId}/statistics`} target="_blank" rel="noopener noreferrer"><BarChart3 />View batch statistics</Link></Button> : null}</div>;
}

function NegativePopulationQCFailureSummary({ run, matterId }: { run: ReviewProgramRunRead; matterId: string }) {
  const qcRaw = run.progress_summary?.negative_population_qc;
  const qc = qcRaw && typeof qcRaw === "object" && !Array.isArray(qcRaw) ? qcRaw as Record<string, unknown> : {};
  const profileRuns = Array.isArray(qc.profile_runs)
    ? qc.profile_runs.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const selectedCount = Number(qc.selected_document_count ?? 0);
  const failedCount = qc.unresolved_document_count == null
    ? profileRuns.reduce((total, profile) => total + Number(profile.failed_document_count ?? 0), 0)
    : Number(qc.unresolved_document_count);
  const failureRate = selectedCount > 0 ? failedCount / selectedCount : null;
  const tolerance = Number(qc.failure_tolerance ?? 0.005);
  const reviewBatchId = String(qc.review_batch_id ?? "");
  if (!selectedCount && !profileRuns.length) return null;
  return <div className="mt-3 space-y-3 rounded-md border border-border/70 bg-background/80 p-3 text-foreground">
    <div className="grid gap-2 sm:grid-cols-3">
      <div><p className="text-xs text-muted-foreground">QC population</p><p className="font-medium">{selectedCount.toLocaleString()} documents</p></div>
      <div><p className="text-xs text-muted-foreground">Unresolved profile results</p><p className="font-medium">{failedCount.toLocaleString()}{failureRate == null ? "" : ` · ${(failureRate * 100).toFixed(2)}%`}</p></div>
      <div><p className="text-xs text-muted-foreground">Completion tolerance</p><p className="font-medium">Less than {(tolerance * 100).toFixed(1)}%</p></div>
    </div>
    {profileRuns.length ? <div className="space-y-2">{profileRuns.map((profile, index) => {
      const failuresRaw = profile.failure_counts;
      const failures = failuresRaw && typeof failuresRaw === "object" && !Array.isArray(failuresRaw) ? failuresRaw as Record<string, unknown> : {};
      return <div key={String(profile.id ?? index)} className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-xs">
        <div><p className="font-medium">{String(profile.model_name ?? profile.profile_key ?? "Q&A profile")}</p><p className="text-muted-foreground">{Number(profile.completed_document_count ?? 0)} completed · {Number(profile.reused_document_count ?? 0)} reused · {Number(profile.failed_document_count ?? 0)} failed</p>{Object.keys(failures).length ? <p className="mt-1 text-muted-foreground">{Object.entries(failures).map(([code, count]) => `${friendly(code)}: ${Number(count)}`).join(" · ")}</p> : null}</div>
        <StatusBadge status={String(profile.status ?? "UNKNOWN")} />
      </div>;
    })}</div> : null}
    <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-muted-foreground">The completed Q&amp;A results are preserved. Refresh state re-evaluates these recorded results and does not rerun either model.</p>{reviewBatchId ? <Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}/batches/${reviewBatchId}/statistics`} target="_blank" rel="noopener noreferrer"><BarChart3 />View batch statistics</Link></Button> : null}</div>
  </div>;
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

function countLabel(count: number, singular: string) {
  return `${count.toLocaleString()} ${count === 1 ? singular : `${singular}s`}`;
}

function formatElapsed(start: string | null | undefined, end: string | null | undefined) {
  if (!start) return "Not recorded";
  if (!end) return "In progress";
  const milliseconds = Date.parse(end) - Date.parse(start);
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return "Unavailable";
  let seconds = Math.floor(milliseconds / 1000);
  const days = Math.floor(seconds / 86_400);
  seconds %= 86_400;
  const hours = Math.floor(seconds / 3_600);
  seconds %= 3_600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  const parts = [days ? `${days}d` : "", hours ? `${hours}h` : "", minutes ? `${minutes}m` : "", seconds || (!days && !hours && !minutes) ? `${seconds}s` : ""].filter(Boolean);
  return parts.join(" ");
}

function slug(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 100);
}
