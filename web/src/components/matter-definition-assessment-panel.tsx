"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Archive, ArchiveRestore, ExternalLink, GitCompareArrows, History, LoaderCircle, PencilLine, Play, RotateCcw, Square, Sparkles, Workflow } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useMemo, useState } from "react";
import { toast } from "sonner";

import { QueryError } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { ActionMenuButton } from "@/components/ui/action-menu-button";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  EvidenceAnalysisOutcomeRead,
  EvidenceStageRetryRead,
  MatterDefinitionAssessmentCreate,
  MatterDefinitionAssessmentQueryRead,
  MatterDefinitionAssessmentQuestionRead,
  MatterDefinitionAssessmentRead,
  MatterDefinitionAssessmentSynthesisUnitRead,
  MatterDefinitionAssessmentUpdate,
  MatterDefinitionRevisionRead,
  WorkflowExecutionRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]);
const STAGES = ["PLANNING", "RETRIEVING", "BUILDING_BATCH", "SUMMARIZING", "SYNTHESIZING", "COMPLETED"];

export function assessmentStageIndex(status?: string): number {
  if (!status) return -1;
  return STAGES.indexOf(status === "COMPLETED_WITH_ERRORS" ? "COMPLETED" : status);
}

export function assessmentCanRetry(status?: string, failedCount = 0): boolean {
  return status === "FAILED" || (status === "COMPLETED_WITH_ERRORS" && failedCount > 0);
}

function synthesisErrorHelp(code?: string | null): string {
  if (code === "USAGE_LIMIT_EXCEEDED") return "This bounded unit exceeded its model budget. Retry synthesis; completed units will be reused.";
  if (code === "INVALID_OUTPUT") return "The unit failed schema or evidence validation. Retry synthesis; if it repeats, inspect the cited unit input.";
  if (code === "MODEL_FAILURE") return "The model request failed. Retry synthesis; completed units will be reused.";
  return "Retry synthesis. Only unresolved units will run again.";
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function formatDuration(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const remainder = safe % 60;
  return [hours ? `${hours}h` : "", minutes || hours ? `${minutes}m` : "", `${remainder}s`]
    .filter(Boolean)
    .join(" ");
}

export function MatterDefinitionAssessmentPanel({
  matterId,
  guidanceId,
  initialAssessmentId = "",
  guidanceName = "Review Guidance",
  guidanceArchived = false,
  revisions,
  publishedRevision,
  embedded = false,
  onReviewDraft,
  onViewDraftDifferences,
}: {
  matterId: string;
  guidanceId?: string;
  initialAssessmentId?: string;
  guidanceName?: string;
  guidanceArchived?: boolean;
  revisions: MatterDefinitionRevisionRead[];
  publishedRevision: number | null;
  embedded?: boolean;
  onReviewDraft: () => void;
  onViewDraftDifferences: () => void;
}) {
  const queryClient = useQueryClient();
  const [launchOpen, setLaunchOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameName, setRenameName] = useState("");
  const [regenerateOpen, setRegenerateOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [updatingQuestionIds, setUpdatingQuestionIds] = useState<Set<string>>(() => new Set());
  const [selectedId, setSelectedId] = useState(initialAssessmentId);
  const [revision, setRevision] = useState(String(revisions[0]?.revision ?? 1));
  const [name, setName] = useState("");
  const [target, setTarget] = useState("500");
  const [controlSize, setControlSize] = useState("0");
  const [useBatching, setUseBatching] = useState(true);
  const [executionMode, setExecutionMode] = useState<"DIRECT_V1" | "EVIDENCE_FIRST_V1">("EVIDENCE_FIRST_V1");
  const [warningAcknowledged, setWarningAcknowledged] = useState(false);
  const regenerateTooltipId = useId();

  const assessmentBase = guidanceId
    ? `/v1/matters/${matterId}/guidance/${guidanceId}/assessments`
    : `/v1/matters/${matterId}/definition-assessments`;
  const runs = useQuery({
    queryKey: ["definition-assessments", matterId, guidanceId, showArchived],
    queryFn: () => coreApi<MatterDefinitionAssessmentRead[]>(`${assessmentBase}${showArchived ? "?include_archived=true" : ""}`),
    refetchInterval: (query) => query.state.data?.some((run) =>
      !TERMINAL.has(run.status) || ["QUEUED", "RUNNING"].includes(run.guidance_refinement_status)
    ) ? 2000 : false,
  });
  const effectiveId = runs.data?.some((run) => run.id === selectedId) ? selectedId : runs.data?.[0]?.id || "";
  const selected = runs.data?.find((run) => run.id === effectiveId);
  const assessedRevision = revisions.find((item) => item.id === selected?.matter_definition_revision_id);
  const refinedRevision = revisions.find((item) => item.id === selected?.refined_matter_definition_revision_id);
  const refinementPublished = Boolean(
    refinedRevision
    && publishedRevision !== null
    && assessedRevision
    && publishedRevision > assessedRevision.revision
  );
  const includeSkillRuns = selected?.status !== "COMPLETED";
  const queries = useQuery({
    queryKey: ["definition-assessment-queries", matterId, effectiveId],
    queryFn: () => coreApi<MatterDefinitionAssessmentQueryRead[]>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/queries`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });
  const questions = useQuery({
    queryKey: ["definition-assessment-questions", matterId, effectiveId],
    queryFn: () => coreApi<MatterDefinitionAssessmentQuestionRead[]>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/questions`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });
  const execution = useQuery({
    queryKey: ["definition-assessment-execution", matterId, effectiveId, includeSkillRuns],
    queryFn: () => coreApi<WorkflowExecutionRead>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/execution?include_skill_runs=${includeSkillRuns}`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });
  const synthesisUnits = useQuery({
    queryKey: ["definition-assessment-synthesis-units", matterId, effectiveId],
    queryFn: () => coreApi<MatterDefinitionAssessmentSynthesisUnitRead[]>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/synthesis-units`),
    enabled: Boolean(
      effectiveId
      && selected?.current_synthesis_attempt_id
      && selected.technical_diagnostics_visible,
    ),
    refetchInterval: selected?.synthesis_status === "RUNNING" ? 2500 : false,
  });
  const evidenceOutcomes = useQuery({
    queryKey: ["definition-assessment-evidence-outcomes", matterId, effectiveId],
    queryFn: () => coreApi<EvidenceAnalysisOutcomeRead>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/evidence-outcomes`),
    enabled: Boolean(effectiveId),
    refetchInterval: (query) => Number(query.state.data?.readiness_counts.PENDING ?? 0) > 0 ? 2500 : false,
  });
  const retryEvidenceStage = useMutation({
    mutationFn: ({ documentId, stage }: { documentId: string; stage: "EXTRACTION" | "ADJUDICATION" | "NARRATIVE" }) => coreApi<EvidenceStageRetryRead>(
      `/v1/matters/${matterId}/definition-assessments/${effectiveId}/evidence-outcomes/${documentId}/retry`,
      { method: "POST", body: JSON.stringify({ stage }) },
    ),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["definition-assessment-evidence-outcomes", matterId, effectiveId] });
      toast.success(result.operator_message);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The evidence stage could not be retried."),
  });

  const launch = useMutation({
    mutationFn: (payload: MatterDefinitionAssessmentCreate) => coreApi<MatterDefinitionAssessmentRead>(assessmentBase, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] });
      setSelectedId(created.id);
      setLaunchOpen(false);
      setName("");
      setExecutionMode("EVIDENCE_FIRST_V1");
      toast.success("Corpus assessment started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be started."),
  });
  const rename = useMutation({
    mutationFn: ({ assessmentId, payload }: { assessmentId: string; payload: MatterDefinitionAssessmentUpdate }) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}`, { method: "PATCH", body: JSON.stringify(payload) }),
    onSuccess: async (updated) => {
      queryClient.setQueriesData<MatterDefinitionAssessmentRead[]>({ queryKey: ["definition-assessments", matterId, guidanceId] }, (current) => current?.map((run) => run.id === updated.id ? updated : run));
      await queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] });
      setRenameOpen(false);
      toast.success("Assessment renamed.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be renamed."),
  });
  const retry = useMutation({
    mutationFn: (assessmentId: string) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/retry`, { method: "POST" }),
    onSuccess: async (updated) => {
      queryClient.setQueriesData<MatterDefinitionAssessmentRead[]>({ queryKey: ["definition-assessments", matterId, guidanceId] }, (current) => current?.map((run) => run.id === updated.id ? updated : run));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-execution", matterId, updated.id] }),
      ]);
      toast.success(`${updated.name} was queued to resume.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be retried."),
  });
  const cancel = useMutation({
    mutationFn: (assessmentId: string) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/cancel`, { method: "POST" }),
    onSuccess: async (updated) => {
      queryClient.setQueriesData<MatterDefinitionAssessmentRead[]>({ queryKey: ["definition-assessments", matterId, guidanceId] }, (current) => current?.map((run) => run.id === updated.id ? updated : run));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-execution", matterId, updated.id] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-evidence-outcomes", matterId, updated.id] }),
      ]);
      setCancelOpen(false);
      toast.success(`${updated.name} was canceled.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be canceled."),
  });
  const regenerateSynthesis = useMutation({
    mutationFn: (assessmentId: string) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/regenerate-synthesis`, { method: "POST" }),
    onSuccess: async (updated) => {
      queryClient.setQueriesData<MatterDefinitionAssessmentRead[]>({ queryKey: ["definition-assessments", matterId, guidanceId] }, (current) => current?.map((run) => run.id === updated.id ? updated : run));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-execution", matterId, updated.id] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-questions", matterId, updated.id] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-synthesis-units", matterId, updated.id] }),
      ]);
      setRegenerateOpen(false);
      toast.success("Refinement analysis was queued without reprocessing document summaries.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The refinement analysis could not be regenerated."),
  });
  const regenerateDocumentAnalyses = useMutation({
    mutationFn: (assessmentId: string) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/regenerate-document-analyses`, { method: "POST" }),
    onSuccess: async (updated) => {
      queryClient.setQueriesData<MatterDefinitionAssessmentRead[]>({ queryKey: ["definition-assessments", matterId, guidanceId] }, (current) => current?.map((run) => run.id === updated.id ? updated : run));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-execution", matterId, updated.id] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-questions", matterId, updated.id] }),
      ]);
      setRegenerateOpen(false);
      toast.success("The frozen batch was queued for document reanalysis and a new synthesis.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document analyses could not be regenerated."),
  });
  const updateQuestion = useMutation({
    mutationFn: ({ id, status, answer }: { id: string; status: "ANSWERED" | "DISMISSED"; answer?: string }) => coreApi<MatterDefinitionAssessmentQuestionRead>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/questions/${id}`, { method: "PUT", body: JSON.stringify({ status, answer: answer || null }) }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["definition-assessment-questions", matterId, effectiveId] }),
        queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] }),
      ]);
      toast.success("Refinement decision saved.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The clarification could not be updated."),
  });
  const archiveFailed = useMutation({
    mutationFn: () => coreApi<{ archived_count: number }>(`/v1/matters/${matterId}/definition-assessments/archive-failed`, {
      method: "POST",
      body: JSON.stringify({ guidance_id: guidanceId ?? null }),
    }),
    onSuccess: async (result) => {
      setSelectedId("");
      await queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] });
      toast.success(`${result.archived_count} failed assessment${result.archived_count === 1 ? "" : "s"} archived.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Failed assessments could not be archived."),
  });
  const restore = useMutation({
    mutationFn: (assessmentId: string) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments/${assessmentId}/restore`, { method: "POST" }),
    onSuccess: async (restored) => {
      await queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId, guidanceId] });
      setSelectedId(restored.id);
      toast.success(`${restored.name} was restored to assessment history.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be restored."),
  });

  const requested = Math.max(1, Number(target) || 500);
  const report = objectValue(selected?.synthesis_result);
  const refinement = objectValue(report.refinement_assessment);
  const configuration = objectValue(selected?.configuration_snapshot);
  const retrievalValidation = objectValue(configuration.retrieval_validation);
  const retrievalNeedsClarification = configuration.failure_kind === "RETRIEVAL_NEEDS_CLARIFICATION";
  const zeroResultTopics = Array.isArray(retrievalValidation.zero_result_topics) ? retrievalValidation.zero_result_topics.map(String) : [];
  const findings = Array.isArray(report.findings) ? report.findings.map(objectValue) : [];
  const findingTopicKeys = new Set(findings.map((finding) => String(finding.topic_key ?? "")));
  const assessmentTopics = Array.isArray(report.topics) ? report.topics.map(objectValue) : [];
  const topicsWithoutFindings = assessmentTopics.filter(
    (topic) => !findingTopicKeys.has(String(topic.topic_key ?? "")),
  );
  const reportQuestions = Array.isArray(report.clarification_questions)
    ? report.clarification_questions.map(objectValue)
    : [];
  const guidanceQuestionCount = questions.data
    ? questions.data.filter((question) => question.status === "OPEN").length
    : reportQuestions.length;
  const technicalDiagnosticsVisible = selected?.technical_diagnostics_visible === true;
  const synthesisDiagnostics = objectValue(selected?.synthesis_diagnostics);
  const finalizationError = objectValue(synthesisDiagnostics.finalization_error);
  const synthesisNormalizationWarnings = Array.isArray(synthesisDiagnostics.normalization_warnings)
    ? synthesisDiagnostics.normalization_warnings.map(objectValue)
    : [];
  const reportOtherUnmappedCriteria = objectValue(report.other_unmapped_criteria);
  const diagnosticOtherUnmappedCriteria = objectValue(synthesisDiagnostics.other_unmapped_criteria);
  const otherUnmappedCriteria = Object.keys(reportOtherUnmappedCriteria).length
    ? reportOtherUnmappedCriteria
    : diagnosticOtherUnmappedCriteria;
  const unmappedCriteriaItems = Array.isArray(otherUnmappedCriteria.items)
    ? otherUnmappedCriteria.items.map(objectValue)
    : [];
  const unmappedCriteriaSynthesis = objectValue(otherUnmappedCriteria.synthesis);
  const failedSynthesisUnits = synthesisUnits.data?.filter((unit) => unit.status === "FAILED") ?? [];
  const completedSynthesisUnits = synthesisUnits.data?.filter((unit) => unit.status === "COMPLETED").length ?? 0;
  const operatorSynthesisLabel = selected?.completion_disposition === "ACTION_REQUIRED"
    ? "action required"
    : selected?.synthesis_status === "RUNNING"
      ? "running"
      : selected?.failed_count
        ? "completed with document errors"
        : "completed";
  const stageIndex = assessmentStageIndex(selected?.status);
  const effectiveRevision = revisions.some((item) => String(item.revision) === revision)
    ? revision
    : String(revisions[0]?.revision ?? 1);
  const definitionState = publishedRevision === Number(effectiveRevision) ? "Published" : "Draft";
  const usage = execution.data;
  const synthesisNeedsAction = selected?.completion_disposition === "ACTION_REQUIRED"
    && selected.synthesis_status !== "NOT_STARTED";
  const cacheRatio = usage?.input_tokens ? Math.round((usage.cached_input_tokens / usage.input_tokens) * 100) : 0;
  const canRetry = assessmentCanRetry(selected?.status, selected?.failed_count)
    && !retrievalNeedsClarification
    && !synthesisNeedsAction;
  const evidenceActionDocuments = evidenceOutcomes.data?.documents.filter((document) =>
    document.synthesis_readiness === "ACTION_REQUIRED" || document.synthesis_readiness === "READY_WITH_FALLBACK"
  ) ?? [];
  const evidenceReadyCount = evidenceOutcomes.data
    ? Number(evidenceOutcomes.data.readiness_counts.READY ?? 0)
      + Number(evidenceOutcomes.data.readiness_counts.READY_WITH_FALLBACK ?? 0)
    : 0;
  const evidenceFailedCount = Number(evidenceOutcomes.data?.readiness_counts.ACTION_REQUIRED ?? 0);
  const displayedSummarizedCount = selected?.execution_mode === "EVIDENCE_FIRST_V1"
    ? Math.max(selected.summarized_count, evidenceReadyCount)
    : selected?.summarized_count ?? 0;
  const displayedFailedCount = selected?.execution_mode === "EVIDENCE_FIRST_V1"
    ? Math.max(selected.failed_count, evidenceFailedCount)
    : selected?.failed_count ?? 0;
  const documentProgressLabel = selected?.execution_mode === "EVIDENCE_FIRST_V1"
    && selected.status === "SUMMARIZING"
    && selected.synthesis_status === "NOT_STARTED"
    ? "Evidence created"
    : "Documents summarized";
  const statusInCoverage = Boolean(
    selected?.coverage_snapshot && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(selected.status),
  );
  const refinementQuestionNotice = questions.data && questions.data.length === 0
    ? selected?.synthesis_result
      ? <p className="rounded-md border border-success/40 bg-success/8 p-3 text-sm">Synthesis completed without generating clarification questions. No question resolution is required.</p>
      : <p className="rounded-md border bg-muted/30 p-3 text-sm text-muted-foreground">Refinement questions will appear after assessment synthesis completes.</p>
    : null;
  const archivableCount = runs.data?.filter((run) =>
    !run.archived_at && ["FAILED", "CANCELED"].includes(run.status)
  ).length ?? 0;

  useEffect(() => {
    if (selected?.guidance_refinement_status !== "COMPLETED") return;
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ["matter-guidance", matterId] }),
      queryClient.invalidateQueries({ queryKey: ["matter-guidance-revisions", matterId, guidanceId] }),
    ]);
  }, [guidanceId, matterId, queryClient, selected?.guidance_refinement_status]);

  const submitLaunch = () => {
    launch.mutate({
      name: name.trim(),
      revision: Number(effectiveRevision),
      target_document_count: requested,
      control_sample_size: Math.max(0, Number(controlSize) || 0),
      use_batching: useBatching,
      acknowledge_large_run_warning: requested > 1000 && warningAcknowledged,
      execution_mode: executionMode,
    });
  };

  const openRename = () => {
    if (!selected) return;
    setRenameName(selected.name);
    setRenameOpen(true);
  };

  const submitRename = () => {
    const nextName = renameName.trim();
    if (!selected || !nextName) return;
    rename.mutate({ assessmentId: selected.id, payload: { name: nextName } });
  };

  const submitQuestionUpdate = async (
    questionId: string,
    status: "ANSWERED" | "DISMISSED",
    answer?: string,
  ) => {
    setUpdatingQuestionIds((current) => new Set(current).add(questionId));
    try {
      await updateQuestion.mutateAsync({ id: questionId, status, answer });
    } finally {
      setUpdatingQuestionIds((current) => {
        const next = new Set(current);
        next.delete(questionId);
        return next;
      });
    }
  };

  const toolbar = (
    <>
      {runs.data?.length ? <Select value={effectiveId} onValueChange={setSelectedId}><SelectTrigger className="min-w-0 max-w-64 flex-1" aria-label="Assessment"><SelectValue /></SelectTrigger><SelectContent>{runs.data.map((run) => <SelectItem key={run.id} value={run.id}>{run.name} · {run.archived_at ? run.archive_reason === "SUPERSEDED" ? "superseded" : "archived" : run.status.toLowerCase().replaceAll("_", " ")}</SelectItem>)}</SelectContent></Select> : <p className="min-w-0 flex-1 truncate text-right text-xs text-muted-foreground">No assessments</p>}
      {selected ? <Button type="button" size="icon" className="size-8 shrink-0" variant="outline" disabled={rename.isPending} aria-label="Rename assessment" onClick={openRename}><PencilLine /></Button> : null}
      {selected?.archived_at ? <Button type="button" size="sm" className="shrink-0" variant="outline" disabled={restore.isPending} onClick={() => restore.mutate(selected.id)}>{restore.isPending ? <LoaderCircle className="animate-spin" /> : <ArchiveRestore />}Restore</Button> : null}
      {selected && !statusInCoverage ? <StatusBadge status={selected.status} /> : null}
      {selected ? <Badge variant="outline">{selected.execution_mode === "EVIDENCE_FIRST_V1" ? "Evidence-first" : "Direct"}</Badge> : null}
      {selected && !TERMINAL.has(selected.status) ? <Button type="button" size="sm" className="shrink-0" variant="destructive" disabled={cancel.isPending} onClick={() => setCancelOpen(true)}>{cancel.isPending ? <LoaderCircle className="animate-spin" /> : <Square />}Cancel</Button> : null}
      {selected?.review_batch_id ? <Button asChild size="icon" className="size-8 shrink-0" variant="outline"><Link href={`/review/matters/${matterId}?batch=${selected.review_batch_id}`} aria-label="Open assessment batch"><ExternalLink /></Link></Button> : null}
      {selected?.review_batch_id ? <Button asChild size="sm" className="shrink-0" variant="outline"><Link href={`/review/matters/${matterId}?batch=${selected.review_batch_id}&analysis=run`}><Play />Run analysis</Link></Button> : null}
      {selected && ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"].includes(selected.status) && selected.selected_count > 0 ? <span className="group relative shrink-0"><Button type="button" size="icon" className="size-8" variant="outline" aria-label="Regenerate assessment" aria-describedby={regenerateTooltipId} onClick={() => setRegenerateOpen(true)}><RotateCcw /></Button><span id={regenerateTooltipId} role="tooltip" className="pointer-events-none invisible absolute right-0 top-full z-50 mt-2 whitespace-nowrap rounded-md bg-foreground px-2 py-1 text-xs font-medium text-background opacity-0 shadow-md transition-opacity group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">Regenerate assessment</span></span> : null}
      {archivableCount > 0 ? <Button type="button" size="sm" className="shrink-0" variant="outline" disabled={archiveFailed.isPending} onClick={() => archiveFailed.mutate()}>{archiveFailed.isPending ? <LoaderCircle className="animate-spin" /> : <Archive />}Archive failed ({archivableCount})</Button> : null}
      <Button type="button" size="sm" className="shrink-0" variant="ghost" onClick={() => setShowArchived((value) => !value)}><History />{showArchived ? "Hide archived" : "Show archived"}</Button>
      <Button type="button" size="sm" className="shrink-0" onClick={() => setLaunchOpen(true)} disabled={!revisions.length || guidanceArchived}><Play />New</Button>
    </>
  );

  return <Card className={cn("overflow-hidden", embedded && "flex h-full min-h-0 flex-col rounded-none border-0 shadow-none")}>
    <div className="flex shrink-0 flex-wrap items-center gap-2 border-b p-2" aria-label="Assessment controls">{toolbar}</div>
    <div className={cn(embedded && "min-h-0 flex-1 overflow-y-auto overscroll-contain")}>
      {runs.error ? <div className="p-4"><QueryError message={runs.error.message} /></div> : runs.isPending ? <p className="p-4 text-sm text-muted-foreground">Loading assessment history…</p> : !runs.data?.length ? <p className="p-4 text-sm text-muted-foreground">No corpus assessments have been run for this Matter Definition.</p> : <div className="flex flex-col gap-4 p-4">
      {selected ? <>
        <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">{STAGES.map((stage, index) => <div key={stage} className={`rounded-md border px-2.5 py-2 text-center text-[11px] font-semibold ${stageIndex >= 0 && index <= stageIndex ? "border-primary/40 bg-primary/8 text-primary" : "text-muted-foreground"}`}>{stage.toLowerCase().replaceAll("_", " ")}</div>)}</div>
        {retrievalNeedsClarification ? <div className="rounded-lg border border-warning/40 bg-warning/8 p-3 text-sm"><p className="font-semibold">Retrieval needs clarification</p><p className="mt-1 text-xs text-muted-foreground">{selected.error_message}</p>{zeroResultTopics.length ? <p className="mt-2 text-xs"><strong>Topics with no results:</strong> {zeroResultTopics.join(", ")}</p> : null}<p className="mt-2 text-xs">Clarify the affected terminology or scope in Review Guidance, then start a new assessment.</p></div> : canRetry ? <div className={`flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3 text-sm ${selected.status === "FAILED" ? "border-destructive/40 bg-destructive/5 text-destructive" : "border-warning/40 bg-warning/8"}`}><div><p className="font-semibold">{selected.status === "FAILED" ? "Assessment failed" : "Assessment completed with document errors"}</p><p className="mt-1 text-xs">{selected.status === "FAILED" ? selected.error_message || "The workflow did not provide an error message." : `${selected.failed_count} failed document${selected.failed_count === 1 ? "" : "s"} can be retried without reprocessing the ${selected.summarized_count} completed summaries.`}</p></div><Button type="button" size="sm" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate(selected.id)}>{retry.isPending ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}{selected.status === "FAILED" ? "Retry assessment" : "Retry failed documents"}</Button></div> : null}
        <div className="grid grid-cols-[repeat(auto-fit,minmax(5.5rem,1fr))] gap-2">
          <Metric label="Selected" value={selected.selected_count} />
          <Metric label={documentProgressLabel} value={`${displayedSummarizedCount.toLocaleString()} / ${selected.selected_count.toLocaleString()}`} />
          <Metric label="Documents skipped" value={selected.skipped_count} />
          <Metric label="Documents failed" value={displayedFailedCount} />
          <Metric label="Estimated input" value={selected.estimated_input_tokens?.toLocaleString() ?? "Pending"} />
          <Metric label="Cache reads" value={`${cacheRatio}%`} />
        </div>
        {selected.skipped_count > 0 ? <p className="rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">{selected.skipped_count.toLocaleString()} selected document{selected.skipped_count === 1 ? " was" : "s were"} skipped because no analyzable text was available. {selected.skipped_count === 1 ? "This was" : "These were"} not a model failure.</p> : null}
        {selected.synthesis_status && selected.synthesis_status !== "NOT_STARTED" ? (
          <section className={`rounded-lg border p-3 text-sm ${selected.completion_disposition === "ACTION_REQUIRED" ? "border-destructive/40 bg-destructive/5" : "border-success/40 bg-success/8"}`}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="font-semibold">Assessment synthesis · {operatorSynthesisLabel}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {selected.synthesis_status === "RUNNING"
                    ? "The assessment conclusions are being prepared."
                    : `${findings.length} finding${findings.length === 1 ? "" : "s"} · ${guidanceQuestionCount} guidance question${guidanceQuestionCount === 1 ? "" : "s"} requiring action · ${selected.failed_count} failed document${selected.failed_count === 1 ? "" : "s"}.`}
                </p>
                {selected.completion_disposition === "ACTION_REQUIRED" ? <><p className="mt-2 text-xs">Document analysis is complete, but required synthesis work failed. Do not rely on the conclusions until the failure is resolved.</p>{finalizationError.error_message ? <p className="mt-2 rounded border border-destructive/30 bg-background/70 p-2 text-xs"><strong>Why:</strong> {String(finalizationError.error_message)}</p> : null}</> : guidanceQuestionCount ? <p className="mt-2 text-xs">Review and answer the guidance questions below before continuing.</p> : selected.synthesis_status === "COMPLETED" ? <p className="mt-2 text-xs">No guidance decision is required from this assessment.</p> : null}
              </div>
              {selected.completion_disposition === "ACTION_REQUIRED" ? <Button type="button" size="sm" variant="outline" disabled={regenerateSynthesis.isPending || selected.summarized_count === 0} onClick={() => regenerateSynthesis.mutate(selected.id)}>{regenerateSynthesis.isPending ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}Retry synthesis</Button> : null}
            </div>
          </section>
        ) : null}
        {evidenceActionDocuments.length ? <details className="rounded-md border bg-background p-3"><summary className="cursor-pointer text-sm font-semibold">{evidenceActionDocuments.length} document outcome{evidenceActionDocuments.length === 1 ? "" : "s"} needing attention</summary><ul className="mt-2 space-y-2">{evidenceActionDocuments.slice(0, 50).map((document) => {
              const failedStages = [
                ["Extraction", document.extraction],
                ["Adjudication", document.adjudication],
                ["Document decision", document.document_decision],
                ["Compatibility projection", document.compatibility_projection],
                ["Narrative", document.narrative],
              ] as const;
              const stageMessages = failedStages
                .filter(([, stage]) => stage.status === "FAILED" || stage.status === "COMPLETED_WITH_ERRORS")
                .map(([label, stage]) => `${label}: ${stage.error_summary || "The stage did not complete successfully."}`);
              const primaryFailure = failedStages.find(([, stage]) => stage.status === "FAILED" || stage.status === "COMPLETED_WITH_ERRORS");
              const failureLabel = primaryFailure?.[0] ?? "Document analysis";
              const failureSummary = primaryFailure?.[1].error_summary || "The stage ended without producing a synthesis-safe result, and no more specific cause was recorded.";
              const missingTextSource = failureSummary.toLowerCase().includes("no preferred text source");
              return <li key={document.matter_document_id} className="rounded-md border bg-muted/20 p-3 text-xs"><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0 flex-1"><p className="font-mono font-semibold">Document …{document.matter_document_id.slice(-8)}</p><p className="mt-2"><strong>Why it needs attention:</strong> {failureLabel} failed. {failureSummary}</p><p className="mt-1 text-muted-foreground"><strong>Impact:</strong> This document was not included in the assessment synthesis; the {displayedSummarizedCount.toLocaleString()} completed document results remain usable.</p><p className="mt-1"><strong>Next step:</strong> {missingTextSource ? "Open the document to confirm the missing content, add or regenerate its preferred text source, then retry the failed document." : document.retryable_stages.length ? `Retry ${document.retryable_stages.map((stage) => stage.toLowerCase()).join(", ")}. Completed stages will be reused.` : "Correct the recorded input or workflow problem, then retry the failed document."}</p></div><div className="flex flex-wrap items-center gap-2">{selected.review_batch_id ? <Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}?batch=${selected.review_batch_id}&document=${document.matter_document_id}`} target="_blank" rel="noopener noreferrer">Open document<ExternalLink /></Link></Button> : null}{document.retryable_stages.length && evidenceOutcomes.data?.technical_diagnostics_visible ? <ActionMenuButton label={`document ${document.matter_document_id.slice(-8)}`} actions={document.retryable_stages.map((stage) => ({ id: stage, label: `Retry ${stage.toLowerCase()}`, disabled: retryEvidenceStage.isPending, onSelect: () => retryEvidenceStage.mutate({ documentId: document.matter_document_id, stage }) }))} /> : document.retryable_stages.length ? <span className="text-muted-foreground">A tenant administrator can queue this stage retry.</span> : canRetry ? <Button type="button" size="sm" variant="outline" disabled={retry.isPending || missingTextSource} onClick={() => retry.mutate(selected.id)}>{retry.isPending ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}Retry failed document</Button> : null}</div></div>{stageMessages.length > 1 ? <details className="mt-2 border-t pt-2 text-muted-foreground"><summary className="cursor-pointer font-medium">Additional stage details</summary><ul className="mt-1 space-y-1">{stageMessages.slice(1).map((message) => <li key={message}>{message}</li>)}</ul></details> : null}</li>;
            })}</ul>{evidenceActionDocuments.length > 50 ? <p className="mt-2 text-xs text-muted-foreground">Showing the first 50 affected documents.</p> : null}</details> : null}
        {technicalDiagnosticsVisible && selected.synthesis_status && selected.synthesis_status !== "NOT_STARTED" ? (
          <details className="order-last rounded-lg border border-warning/40 bg-warning/5 p-4 text-sm" aria-labelledby="technical-diagnostics-heading">
            <summary className="cursor-pointer list-none">
              <span className="flex flex-wrap items-start justify-between gap-3">
                <span>
                  <span id="technical-diagnostics-heading" role="heading" aria-level={3} className="block font-semibold">Technical diagnostics</span>
                  <span className="mt-1 block text-xs text-muted-foreground">Model execution details for tenant administrators. Expand only when troubleshooting.</span>
                </span>
                <Badge variant="outline">Tenant administrators</Badge>
              </span>
            </summary>
            <div className="mt-3 rounded-md border bg-background/70 p-3">
              <p className="font-semibold">Synthesis health · {(selected.completion_disposition ?? selected.synthesis_status).toLowerCase().replaceAll("_", " ")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{synthesisUnits.data?.length ? `${completedSynthesisUnits} of ${synthesisUnits.data.length} bounded synthesis units completed.` : selected.synthesis_status === "RUNNING" ? "The staged synthesis is running." : "No unit-level records were returned."}</p>
              {failedSynthesisUnits.length ? <details className="mt-3 border-t pt-2"><summary className="cursor-pointer text-xs font-semibold">{failedSynthesisUnits.length} failed unit{failedSynthesisUnits.length === 1 ? "" : "s"}</summary><ul className="mt-2 space-y-2">{failedSynthesisUnits.map((unit) => <li key={unit.id} className="rounded bg-muted/30 p-2 text-xs"><p className="font-mono font-semibold">{unit.unit_type.toLowerCase()} · {unit.unit_key}</p><p className="mt-1 text-muted-foreground">{unit.error_code ?? "MODEL_FAILURE"}: {unit.error_message ?? "No error details were recorded."}</p><p className="mt-1">{synthesisErrorHelp(unit.error_code)}</p><p className="mt-1 text-muted-foreground">Attempt {unit.attempt_count}</p></li>)}</ul></details> : Number(synthesisDiagnostics.optional_failure_count ?? 0) > 0 || Number(synthesisDiagnostics.required_failure_count ?? 0) > 0 ? <p className="mt-2 text-xs text-muted-foreground">Refresh to load individual unit errors.</p> : null}
            </div>
            {evidenceOutcomes.data?.technical_diagnostics_visible && evidenceOutcomes.data.source_workflow_count > 0 ? <details className="mt-3 rounded-md border bg-background/70 p-3"><summary className="cursor-pointer font-semibold">Evidence-first workflow diagnostics</summary><p className="mt-2 text-xs text-muted-foreground">{evidenceOutcomes.data.source_workflow_count.toLocaleString()} workflows · {evidenceOutcomes.data.source_artifact_count.toLocaleString()} durable artifacts · {(evidenceOutcomes.data.usage?.request_count ?? 0).toLocaleString()} model requests</p><div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">{Object.entries(evidenceOutcomes.data.stage_status_counts).map(([stage, counts]) => <div key={stage} className="rounded border bg-muted/20 p-2 text-xs"><p className="font-semibold">{stage.replaceAll("_", " ")}</p><p className="mt-1 text-muted-foreground">{Object.entries(counts).map(([status, count]) => `${status.toLowerCase().replaceAll("_", " ")} ${count}`).join(" · ")}</p></div>)}</div></details> : null}
            {synthesisNormalizationWarnings.length ? <details className="mt-3 rounded-md border bg-background/70 p-3"><summary className="cursor-pointer font-semibold">{synthesisNormalizationWarnings.length} model output adjustment{synthesisNormalizationWarnings.length === 1 ? "" : "s"}</summary><p className="mt-2 text-xs text-muted-foreground">The system automatically removed or corrected unsupported model output before assembling the assessment. No action is needed unless an adjustment changed a substantive conclusion.</p><ul className="mt-2 space-y-2">{synthesisNormalizationWarnings.map((warning, index) => <li key={`${String(warning.unit_type ?? "unit")}-${String(warning.unit_key ?? "unknown")}-${String(warning.code ?? "normalized")}-${String(warning.path ?? "output")}-${index}`} className="rounded bg-muted/30 p-2 text-xs"><p className="font-mono font-semibold">{String(warning.unit_type ?? "synthesis").toLowerCase()} · {String(warning.unit_key ?? "unknown")}</p><p className="mt-1">{String(warning.message ?? "Model output was normalized.")}</p><p className="mt-1 text-muted-foreground">{String(warning.code ?? "OUTPUT_NORMALIZED")}{warning.path ? ` · ${String(warning.path)}` : ""}</p></li>)}</ul></details> : null}
            {unmappedCriteriaItems.length ? (
          <section className="mt-3 rounded-md border bg-background/70 p-3" aria-labelledby="unmapped-criteria-heading">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 id="unmapped-criteria-heading" className="font-semibold">Other / unmapped criteria</h3>
                <p className="mt-1 max-w-3xl text-xs text-muted-foreground">
                  These document-analysis labels could not be safely mapped to an authoritative Review Guidance criterion. They are retained for review but are not counted as guidance topics and do not recode documents.
                </p>
              </div>
              <Badge variant="outline">Technical reporting only</Badge>
            </div>
            <p className="mt-3 text-sm">
              {Number(otherUnmappedCriteria.occurrence_count ?? 0).toLocaleString()} occurrence{Number(otherUnmappedCriteria.occurrence_count ?? 0) === 1 ? "" : "s"} across {Number(otherUnmappedCriteria.document_count ?? 0).toLocaleString()} document{Number(otherUnmappedCriteria.document_count ?? 0) === 1 ? "" : "s"}.
            </p>
            {unmappedCriteriaSynthesis.disposition ? (
              <div className="mt-3 rounded-md border bg-background/70 p-3 text-sm">
                <p className="font-semibold">Synthesis recommendation · {String(unmappedCriteriaSynthesis.disposition).toLowerCase().replaceAll("_", " ")}</p>
                <p className="mt-1 text-xs text-muted-foreground">{String(unmappedCriteriaSynthesis.rationale ?? "")}</p>
                {unmappedCriteriaSynthesis.disposition === "GUIDANCE_SUFFICIENT" ? <p className="mt-2 text-xs font-medium">No operator action is required; the existing guidance covers these labels.</p> : null}
                {unmappedCriteriaSynthesis.question ? <p className="mt-2 text-xs"><strong>Proposed guidance question:</strong> {String(unmappedCriteriaSynthesis.question)}</p> : null}
              </div>
            ) : null}
            <div className="mt-3 space-y-2">
              {unmappedCriteriaItems.map((item, itemIndex) => {
                const matchTypeCounts = objectValue(item.match_type_counts);
                const examples = Array.isArray(item.representative_evidence) ? item.representative_evidence : [];
                return (
                  <details key={`${String(item.source_key ?? "unmapped")}-${itemIndex}`} className="rounded-md border bg-background/70 p-3">
                    <summary className="cursor-pointer text-sm font-semibold">
                      <span className="font-mono">{String(item.source_key ?? "UNSPECIFIED")}</span>
                      <span className="ml-2 font-sans text-xs font-normal text-muted-foreground">
                        {Number(item.occurrence_count ?? 0).toLocaleString()} occurrence{Number(item.occurrence_count ?? 0) === 1 ? "" : "s"} · {Number(item.document_count ?? 0).toLocaleString()} document{Number(item.document_count ?? 0) === 1 ? "" : "s"}
                      </span>
                    </summary>
                    {Object.keys(matchTypeCounts).length ? <p className="mt-2 text-xs text-muted-foreground">Match types: {Object.entries(matchTypeCounts).map(([key, value]) => `${key.toLowerCase().replaceAll("_", " ")} ${Number(value)}`).join(" · ")}</p> : null}
                    {examples.length ? <div className="mt-2 space-y-2">{examples.map((value, exampleIndex) => {
                      const example = objectValue(value);
                      const evidence = Array.isArray(example.evidence) ? example.evidence.map(objectValue) : [];
                      return <div key={`${String(example.matter_document_id ?? "document")}-${exampleIndex}`} className="rounded bg-muted/30 p-2 text-xs"><p className="break-all font-mono text-muted-foreground">Document {String(example.matter_document_id ?? "unknown")}</p><p className="mt-1">{String(example.label ?? item.source_key ?? "Unmapped criterion")} · {String(example.match_type ?? "UNKNOWN").toLowerCase().replaceAll("_", " ")} · {Math.round(Number(example.confidence ?? 0) * 100)}% confidence</p>{evidence.map((entry, evidenceIndex) => <p key={evidenceIndex} className="mt-1 text-muted-foreground">{String(entry.reason ?? "Evidence retained")} {Array.isArray(entry.paragraph_ids) && entry.paragraph_ids.length ? `(${entry.paragraph_ids.map(String).join(", ")})` : ""}</p>)}</div>;
                    })}</div> : <p className="mt-2 text-xs text-muted-foreground">No citable representative passage was available for this label.</p>}
                  </details>
                );
              })}
            </div>
          </section>
            ) : null}
          </details>
        ) : null}
        {selected.coverage_snapshot ? <div className={`rounded-lg border p-3 text-sm ${objectValue(selected.coverage_snapshot).status === "INSUFFICIENT" ? "border-conflict/40 bg-conflict/8" : "bg-muted/30"}`}><div className="flex flex-wrap items-center justify-between gap-2"><p className="font-semibold">Coverage: {String(objectValue(selected.coverage_snapshot).status ?? "pending").toLowerCase()}</p>{statusInCoverage ? <StatusBadge status={selected.status} /> : null}</div><p className="mt-1 text-xs text-muted-foreground">{selected.summarized_count} successful of {selected.selected_count} selected · {selected.partial_coverage_count} partial · {selected.invalid_result_count} invalid</p></div> : null}

        {queries.data?.length ? <section><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Retrieval plan</h3><div className="space-y-2">{queries.data.map((item) => <div key={item.id} className="rounded-lg border p-3"><div className="flex items-center justify-between gap-3"><p className="text-sm font-semibold">{item.criterion_label}</p><Badge variant="outline">{item.result_count} results</Badge></div><p className="mt-1 text-xs text-muted-foreground">{item.rationale}</p></div>)}</div></section> : null}

        {report.narrative || selected.synthesis_status === "COMPLETED" ? (
          <section className="rounded-lg border bg-muted/20 p-4" aria-labelledby="assessment-conclusions-heading">
            <h3 id="assessment-conclusions-heading" className="font-semibold">Assessment conclusions</h3>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
              <Metric label="Findings" value={findings.length} />
              <Metric label="Guidance questions" value={guidanceQuestionCount} />
              <Metric label="Criteria without findings" value={topicsWithoutFindings.length} />
              <Metric label="Skipped documents" value={selected.skipped_count} />
              <Metric label="Failed documents" value={selected.failed_count} />
            </div>
            {report.narrative ? <p className="mt-3 whitespace-pre-wrap text-sm leading-6">{String(report.narrative)}</p> : null}
            {findings.length ? <div className="mt-4 space-y-3">{findings.map((finding, index) => {
              const evidence = Array.isArray(finding.evidence) ? finding.evidence.map(objectValue) : [];
              const limitations = Array.isArray(finding.limitations) ? finding.limitations.map(String) : [];
              return <article key={`${String(finding.topic_key ?? "finding")}-${index}`} className="rounded-md border bg-background p-3"><div className="flex flex-wrap items-center justify-between gap-2"><h4 className="font-semibold">{String(finding.label ?? finding.topic_key ?? `Finding ${index + 1}`)}</h4><Badge variant="outline">{evidence.length} evidence item{evidence.length === 1 ? "" : "s"}</Badge></div><p className="mt-2 text-sm leading-6">{String(finding.finding ?? "")}</p>{limitations.length ? <div className="mt-3"><p className="text-xs font-semibold">Limitations</p><ul className="mt-1 list-disc space-y-1 pl-5 text-xs text-muted-foreground">{limitations.map((limitation, limitationIndex) => <li key={limitationIndex}>{limitation}</li>)}</ul></div> : null}</article>;
            })}</div> : <p className="mt-3 text-sm text-muted-foreground">No evidence-supported findings were produced.</p>}
            {topicsWithoutFindings.length ? <details className="mt-4 rounded-md border bg-background p-3"><summary className="cursor-pointer text-sm font-semibold">{topicsWithoutFindings.length} {topicsWithoutFindings.length === 1 ? "criterion" : "criteria"} with no separate finding</summary><p className="mt-2 text-xs text-muted-foreground">The assessment evaluated these criteria but did not find enough supported material for a separate conclusion.</p><ul className="mt-2 list-disc space-y-1 pl-5 text-xs">{topicsWithoutFindings.map((topic, index) => <li key={`${String(topic.topic_key ?? "topic")}-${index}`}>{String(topic.label ?? topic.topic_key ?? "Unnamed criterion")}</li>)}</ul></details> : null}
          </section>
        ) : null}

        {refinement.outcome ? <section className="rounded-lg border p-4"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">Matter Definition refinement</h3><Badge variant="outline">{String(refinement.outcome).toLowerCase().replaceAll("_", " ")}</Badge></div><p className="mt-2 max-w-3xl text-sm leading-6 text-muted-foreground">{String(refinement.rationale ?? "")}</p></section> : null}

        {questions.data?.length ? (
          <section>
            <h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Matter Definition refinement questions</h3>
            <div className="space-y-3">
              {questions.data.map((question) => (
                <ClarificationQuestion
                  key={question.id}
                  question={question}
                  matterId={matterId}
                  reviewBatchId={selected.review_batch_id}
                  disabled={updatingQuestionIds.has(question.id)}
                  onUpdate={(status, answer) => void submitQuestionUpdate(question.id, status, answer)}
                />
              ))}
            </div>
            {selected.guidance_refinement_status === "QUEUED" || selected.guidance_refinement_status === "RUNNING" ? (
              <p className="mt-3 flex items-center gap-2 rounded-md border bg-muted/30 p-3 text-sm"><LoaderCircle className="size-4 animate-spin" />All questions are resolved. Creating a revised guidance draft…</p>
            ) : selected.guidance_refinement_status === "COMPLETED" && refinementPublished ? (
              <div className="mt-3 rounded-md border border-success/40 bg-success/8 p-3 text-sm"><p className="font-semibold">Revised Matter Definition revision {publishedRevision} is published.</p><p className="mt-1 text-xs text-muted-foreground">Return to the Review Program and continue to reassessment.</p></div>
            ) : selected.guidance_refinement_status === "COMPLETED" ? (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-success/40 bg-success/8 p-3 text-sm"><div><p className="font-semibold">A revised Matter Definition draft is ready.</p><p className="mt-1 text-xs text-muted-foreground">Review the generated changes and explicitly publish the draft before returning to the Review Program.</p></div><div className="flex flex-wrap gap-2"><Button type="button" size="sm" variant="outline" onClick={onViewDraftDifferences}><GitCompareArrows />View differences</Button><Button type="button" size="sm" onClick={onReviewDraft}><Sparkles />Review draft and publish</Button></div></div>
            ) : selected.guidance_refinement_status === "NOT_REQUIRED" ? (
              <p className="mt-3 rounded-md border p-3 text-sm text-muted-foreground">All questions were dismissed, so no new guidance draft was created.</p>
            ) : selected.guidance_refinement_status === "FAILED" ? (
              <p className="mt-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">The revised guidance draft could not be created: {selected.guidance_refinement_error_message ?? "No error details were provided."}</p>
            ) : null}
          </section>
        ) : refinementQuestionNotice}

        {usage ? <details className="rounded-lg border"><summary className="cursor-pointer p-3 text-sm font-semibold"><span className="inline-flex items-center gap-2"><Workflow className="size-4" />Workflow execution · {usage.request_count} model requests</span></summary><div className="border-t p-3"><div className="mb-3 grid gap-2 sm:grid-cols-3"><Metric label="Processing time" value={formatDuration(usage.processing_seconds)} /><Metric label="Operator wait" value={formatDuration(usage.operator_wait_seconds)} /><Metric label="Total elapsed" value={formatDuration(usage.elapsed_seconds)} /></div><p className="mb-3 text-xs text-muted-foreground">Processing time excludes the recorded gap between a terminal synthesis attempt and an operator-requested retry.</p><div className="mb-3 grid gap-2 sm:grid-cols-4"><Metric label="Input tokens" value={usage.input_tokens.toLocaleString()} /><Metric label="Cached input" value={usage.cached_input_tokens.toLocaleString()} /><Metric label="Cache writes" value={usage.cache_write_tokens.toLocaleString()} /><Metric label="Output tokens" value={usage.output_tokens.toLocaleString()} /></div><ol className="space-y-2">{usage.steps.map((step) => <li key={step.id} className="flex items-center justify-between rounded-md bg-muted/30 px-3 py-2 text-xs"><span><strong>{step.ordinal}. {step.role_key.replaceAll("_", " ")}</strong> · {step.completed_count}/{step.total_count}</span><StatusBadge status={step.status} /></li>)}</ol>{usage.skill_runs.length ? <div className="mt-3 border-t pt-3"><p className="text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Skill runs & artifacts</p><p className="mt-1 text-xs text-muted-foreground">{usage.skill_runs.length} runs · {usage.skill_runs.filter((run) => run.output_artifact_id).length} immutable output artifacts · {usage.skill_runs.filter((run) => run.status === "FAILED").length} failed attempts</p>{usage.skill_runs.filter((run) => run.output_artifact_id || run.error_message).length ? <div className="mt-2 max-h-40 space-y-1 overflow-auto">{usage.skill_runs.filter((run) => run.output_artifact_id || run.error_message).map((run) => <div key={run.id} className="rounded bg-muted/30 px-2 py-1.5 font-mono text-[10px]"><span>{run.scope_type.toLowerCase()}</span>{run.output_artifact_id ? <span className="block truncate text-muted-foreground" title={run.output_artifact_id}>artifact {run.output_artifact_id}</span> : null}{run.error_message ? <span className="block text-destructive">{run.error_message}</span> : null}</div>)}</div> : null}</div> : null}</div></details> : null}
      </> : null}
      </div>}
    </div>

    <Dialog open={launchOpen} onOpenChange={setLaunchOpen}><DialogContent><DialogHeader><DialogTitle>Assess {guidanceName} against corpus</DialogTitle><DialogDescription>The selected guidance profile, revision, and workflow bindings are pinned for the full run. The resulting batch and analyses remain isolated from matter-wide metadata.</DialogDescription></DialogHeader><div className="space-y-4">
      <div><label className="text-sm font-medium" htmlFor="assessment-name">Assessment name</label><Input id="assessment-name" className="mt-1" value={name} maxLength={200} placeholder="Example: Initial hurricane coverage review" onChange={(event) => setName(event.target.value)} /><p className="mt-1 text-xs text-muted-foreground">This name is also used for the generated review batch.</p></div>
      <div><label className="text-sm font-medium" htmlFor="assessment-revision">Definition revision</label><Select value={effectiveRevision} onValueChange={setRevision}><SelectTrigger id="assessment-revision" className="mt-1"><SelectValue /></SelectTrigger><SelectContent>{revisions.map((item) => <SelectItem key={item.id} value={String(item.revision)}>Revision {item.revision}{publishedRevision === item.revision ? " · published" : " · draft"}</SelectItem>)}</SelectContent></Select><p className="mt-1 text-xs text-muted-foreground">{definitionState} revision</p></div>
      <div className="grid grid-cols-2 gap-3"><div><label className="text-sm font-medium" htmlFor="assessment-target">Target documents</label><Input id="assessment-target" className="mt-1" type="number" min={1} value={target} onChange={(event) => { setTarget(event.target.value); setWarningAcknowledged(false); }} /><p className="mt-1 text-xs text-muted-foreground">The assessment starts only after retrieval can fill this target.</p></div><div><label className="text-sm font-medium" htmlFor="assessment-control">Control sample</label><Input id="assessment-control" className="mt-1" type="number" min={0} value={controlSize} onChange={(event) => setControlSize(event.target.value)} /></div></div>
      <div><label className="text-sm font-medium" htmlFor="assessment-execution-mode">Processing path</label><Select value={executionMode} onValueChange={(value) => setExecutionMode(value as "DIRECT_V1" | "EVIDENCE_FIRST_V1")}><SelectTrigger id="assessment-execution-mode" className="mt-1"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="EVIDENCE_FIRST_V1">Evidence-first · default</SelectItem><SelectItem value="DIRECT_V1">Direct analysis · rollback</SelectItem></SelectContent></Select><p className="mt-1 text-xs text-muted-foreground">Evidence-first uses durable extraction and criterion adjudication and requires tenant-administrator authorization. Direct analysis remains available as an explicit rollback path.</p></div>
      <label className="flex cursor-pointer items-start gap-2 rounded-lg border p-3 text-sm"><input className="mt-1 size-4 accent-primary" type="checkbox" checked={useBatching} onChange={(event) => setUseBatching(event.target.checked)} /><span><strong>Use Batch API</strong><span className="mt-0.5 block text-xs text-muted-foreground">Submit eligible document summaries asynchronously through the model provider&apos;s lower-cost Batch API. Oversized map/reduce documents continue through real-time processing.</span></span></label>
      {requested > 1000 ? <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-conflict/40 bg-conflict/8 p-3 text-sm"><input className="mt-1 size-4 accent-primary" type="checkbox" checked={warningAcknowledged} onChange={(event) => setWarningAcknowledged(event.target.checked)} /><span><strong className="flex items-center gap-1"><AlertTriangle className="size-4" />Large analysis</strong>Continue with {requested.toLocaleString()} documents. This can use substantial model capacity.</span></label> : null}
    </div><DialogFooter><Button type="button" variant="outline" onClick={() => setLaunchOpen(false)}>Cancel</Button><Button type="button" disabled={launch.isPending || !name.trim() || (requested > 1000 && !warningAcknowledged)} onClick={submitLaunch}>{launch.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Start assessment</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={renameOpen} onOpenChange={(open) => { if (!rename.isPending) setRenameOpen(open); }}><DialogContent><DialogHeader><DialogTitle>Rename assessment</DialogTitle><DialogDescription>Change how this assessment appears in assessment history. Its frozen batch and results are unchanged.</DialogDescription></DialogHeader><div className="space-y-2"><label className="text-sm font-medium" htmlFor="rename-assessment-name">Assessment name</label><Input id="rename-assessment-name" autoFocus maxLength={200} value={renameName} onChange={(event) => setRenameName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); submitRename(); } }} /></div><DialogFooter><Button type="button" variant="outline" disabled={rename.isPending} onClick={() => setRenameOpen(false)}>Cancel</Button><Button type="button" disabled={!renameName.trim() || rename.isPending} onClick={submitRename}>{rename.isPending ? <LoaderCircle className="animate-spin" /> : <PencilLine />}Rename</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={cancelOpen} onOpenChange={(open) => { if (!cancel.isPending) setCancelOpen(open); }}><DialogContent><DialogHeader><DialogTitle>Cancel assessment?</DialogTitle><DialogDescription>Stop the assessment and all active child workflows. Completed model requests and durable artifacts remain available for diagnostics, but synthesis will not continue.</DialogDescription></DialogHeader><div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm"><p className="font-semibold">{selected?.name ?? "Selected assessment"}</p><p className="mt-1 text-xs text-muted-foreground">This cannot resume the same run. After correcting the issue, start a new assessment or use an eligible retry action.</p></div><DialogFooter><Button type="button" variant="outline" disabled={cancel.isPending} onClick={() => setCancelOpen(false)}>Keep running</Button><Button type="button" variant="destructive" disabled={!selected || cancel.isPending} onClick={() => selected && cancel.mutate(selected.id)}>{cancel.isPending ? <LoaderCircle className="animate-spin" /> : <Square />}Cancel assessment</Button></DialogFooter></DialogContent></Dialog>

    <Dialog open={regenerateOpen} onOpenChange={(open) => { if (!regenerateSynthesis.isPending && !regenerateDocumentAnalyses.isPending) setRegenerateOpen(open); }}><DialogContent><DialogHeader><DialogTitle>Regenerate assessment</DialogTitle><DialogDescription>Choose whether to reuse the current document analyses or run the current managed skills over the same frozen batch.</DialogDescription></DialogHeader><div className="space-y-3">
      <div className="rounded-lg border p-4"><h3 className="font-semibold">Reuse current document analyses</h3><p className="mt-1 text-sm text-muted-foreground">Regenerates the refinement questions and topic synthesis from the {selected?.summarized_count.toLocaleString() ?? 0} existing analyses. Documents are not processed again.</p><Button type="button" className="mt-3" variant="outline" disabled={!selected || selected.summarized_count === 0 || regenerateSynthesis.isPending || regenerateDocumentAnalyses.isPending} onClick={() => selected && regenerateSynthesis.mutate(selected.id)}>{regenerateSynthesis.isPending ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}Reuse analyses</Button></div>
      {selected?.execution_mode === "EVIDENCE_FIRST_V1" ? <div className="rounded-lg border border-warning/40 bg-warning/8 p-4"><h3 className="font-semibold">Evidence-first durable results</h3><p className="mt-1 text-sm text-muted-foreground">Use the document-level stage actions to retry failed extraction, adjudication, or narrative work. To reprocess the entire corpus under current bindings, start a new controlled-rollout assessment so this run&apos;s frozen lineage remains intact.</p></div> : <div className="rounded-lg border border-warning/40 bg-warning/8 p-4"><h3 className="font-semibold">Reanalyze the frozen batch</h3><p className="mt-1 text-sm text-muted-foreground">Processes all {selected?.selected_count.toLocaleString() ?? 0} frozen documents with the currently published document-analysis skill, creates new immutable summary artifacts, and then regenerates synthesis. Previous artifacts remain available in execution history. This can use substantial model capacity.</p><Button type="button" className="mt-3" disabled={!selected || regenerateSynthesis.isPending || regenerateDocumentAnalyses.isPending} onClick={() => selected && regenerateDocumentAnalyses.mutate(selected.id)}>{regenerateDocumentAnalyses.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}Reanalyze documents</Button></div>}
    </div><DialogFooter><Button type="button" variant="outline" disabled={regenerateSynthesis.isPending || regenerateDocumentAnalyses.isPending} onClick={() => setRegenerateOpen(false)}>Cancel</Button></DialogFooter></DialogContent></Dialog>
  </Card>;
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="min-w-0 rounded-lg border bg-card p-2.5"><p className="text-[10px] font-bold uppercase leading-3 tracking-[0.03em] text-muted-foreground">{label}</p><p className="mt-1 whitespace-nowrap text-sm font-semibold leading-5 tabular-nums">{value}</p></div>;
}

type QuestionEvidence = {
  matterDocumentId: string;
  paragraphIds: string[];
  reason: string;
};

function parseQuestionEvidence(evidence: MatterDefinitionAssessmentQuestionRead["evidence"]): QuestionEvidence[] {
  return evidence.flatMap((item) => {
    const value = objectValue(item);
    const matterDocumentId = typeof value.matter_document_id === "string" ? value.matter_document_id.trim() : "";
    if (!matterDocumentId) return [];
    return [{
      matterDocumentId,
      paragraphIds: Array.isArray(value.paragraph_ids)
        ? value.paragraph_ids.filter((paragraphId): paragraphId is string => typeof paragraphId === "string" && paragraphId.trim().length > 0)
        : [],
      reason: typeof value.reason === "string" ? value.reason.trim() : "",
    }];
  });
}

function questionEvidenceHref(matterId: string, reviewBatchId: string, evidence: QuestionEvidence): string {
  const parameters = new URLSearchParams({
    batch: reviewBatchId,
    document: evidence.matterDocumentId,
  });
  if (evidence.paragraphIds.length) parameters.set("paragraph", evidence.paragraphIds.join(","));
  return `/review/matters/${matterId}?${parameters.toString()}`;
}

function ClarificationQuestion({ question, matterId, reviewBatchId, disabled, onUpdate }: { question: MatterDefinitionAssessmentQuestionRead; matterId: string; reviewBatchId?: string | null; disabled: boolean; onUpdate: (status: "ANSWERED" | "DISMISSED", answer?: string) => void }) {
  const [answer, setAnswer] = useState(question.answer ?? "");
  const evidence = useMemo(() => parseQuestionEvidence(question.evidence), [question.evidence]);
  return <div className="rounded-lg border p-3" aria-busy={disabled}><div className="flex flex-wrap items-start justify-between gap-2"><p className="max-w-3xl text-sm font-semibold">{question.question}</p><div className="flex gap-1"><Badge variant="outline">{question.priority.toLowerCase()}</Badge><StatusBadge status={question.status} /></div></div><p className="mt-1 text-xs leading-5 text-muted-foreground">{question.rationale}</p>{evidence.length ? <details className="mt-2 rounded-md border bg-muted/20"><summary className="cursor-pointer px-3 py-2 text-xs font-medium">Supporting documents ({evidence.length})</summary><ol className="space-y-2 border-t p-3">{evidence.map((item, index) => <li key={`${item.matterDocumentId}-${index}`} className="rounded-md bg-background p-2 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">Document {index + 1}{item.paragraphIds.length ? ` · ${item.paragraphIds.join(", ")}` : ""}</span>{reviewBatchId ? <Link className="inline-flex items-center gap-1 font-medium text-primary hover:underline" href={questionEvidenceHref(matterId, reviewBatchId, item)} target="_blank" rel="noopener noreferrer">Open cited document<ExternalLink className="size-3" /></Link> : null}</div>{item.reason ? <p className="mt-1 leading-5 text-muted-foreground">{item.reason}</p> : null}</li>)}</ol></details> : null}{question.status === "OPEN" ? <div className="mt-3 space-y-3">{question.suggested_answers.length ? <div><p className="mb-1.5 text-xs font-medium text-muted-foreground">Suggested answers</p><div className="flex flex-wrap gap-2">{question.suggested_answers.map((suggestion) => <Button key={suggestion} type="button" size="sm" variant={answer === suggestion ? "default" : "outline"} disabled={disabled} className="h-auto whitespace-normal text-left" onClick={() => setAnswer(suggestion)}>{suggestion}</Button>)}</div></div> : null}<div className="flex items-end gap-2"><Textarea className="min-h-20" disabled={disabled} placeholder="Select a suggestion or enter a custom answer…" value={answer} onChange={(event) => setAnswer(event.target.value)} /><div className="flex shrink-0 flex-col gap-2"><Button type="button" size="sm" disabled={disabled || !answer.trim()} onClick={() => onUpdate("ANSWERED", answer)}>Answer</Button><Button type="button" size="sm" variant="ghost" disabled={disabled} onClick={() => onUpdate("DISMISSED")}>Dismiss</Button></div></div></div> : question.answer ? <p className="mt-3 rounded-md bg-muted/40 p-2 text-sm">{question.answer}</p> : null}</div>;
}
