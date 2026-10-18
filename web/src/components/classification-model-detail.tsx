"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, BrainCircuit, Layers3, LoaderCircle, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type {
  ClassificationEvaluationReport,
  ClassificationModelDecisionRead,
  ClassificationModelRead,
  ClassificationModelVersionRead,
  ClassificationPromotionPreview,
  ClassificationPromotionResponse,
  ClassificationScoringRunRead,
  ClassificationTrainingJobRead,
  ClassificationTrainingRunRead,
  JobExecutionRead,
  ReviewBatchClassifierRunCreate,
  ReviewBatchRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { matterHref } from "@/lib/matter-routes";

function shortId(value: string) {
  return value.slice(0, 8);
}

function metricLabel(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function metricValue(value: number | null | undefined) {
  if (value === null || value === undefined) return "—";
  return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(3);
}

function stageLabel(value: string) {
  return value.replaceAll("_", " ").toLowerCase().replace(/^./, (character) => character.toUpperCase());
}

function ExcludedClasses({ exclusions }: { exclusions?: ClassificationModelVersionRead["excluded_classes"] }) {
  if (!exclusions?.length) return null;
  return (
    <div className="rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
      <p className="font-medium">{exclusions.length.toLocaleString()} class{exclusions.length === 1 ? "" : "es"} excluded from this training run</p>
      <p className="mt-1 text-xs text-muted-foreground">The observed classes remain on the dataset snapshot, but no classifier or prediction is produced for these classes.</p>
      <ul className="mt-2 space-y-2">
        {exclusions.map((exclusion) => <li key={exclusion.class_key}>
          <span className="font-medium">{exclusion.class_key}</span>
          <span className="text-muted-foreground"> · {exclusion.feature_ready_positive_count.toLocaleString()} positives / {exclusion.feature_ready_negative_count.toLocaleString()} negatives in {exclusion.positive_group_count.toLocaleString()} / {exclusion.negative_group_count.toLocaleString()} leakage-safe groups</span>
          <p className="text-xs text-muted-foreground">{exclusion.message}</p>
        </li>)}
      </ul>
    </div>
  );
}

function ConfusionMatrix({ report }: { report: ClassificationEvaluationReport }) {
  const matrix = report.confusion_matrix ?? [];
  const classes = report.ordered_class_keys;
  const square = report.classification_strategy !== "ONE_VS_REST"
    && matrix.length === classes.length
    && matrix.every((row) => row.length === classes.length);
  if (!matrix.length) return <p className="text-sm text-muted-foreground">No confusion matrix was produced.</p>;
  if (!square) {
    return (
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="border-b text-xs text-muted-foreground"><tr><th className="py-2 pr-3">Class</th><th className="py-2 pr-3">TN</th><th className="py-2 pr-3">FP</th><th className="py-2 pr-3">FN</th><th className="py-2">TP</th></tr></thead>
          <tbody>{matrix.map((row, index) => <tr key={classes[index]} className="border-b last:border-0"><td className="py-2 pr-3 font-medium">{classes[index]}</td>{row.map((value, column) => <td key={column} className="py-2 pr-3 tabular-nums">{value.toLocaleString()}</td>)}</tr>)}</tbody>
        </table>
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="text-center text-sm">
        <thead><tr><th className="p-2 text-left text-xs text-muted-foreground">Actual ↓ / Predicted →</th>{classes.map((value) => <th key={value} className="p-2 font-medium">{value}</th>)}</tr></thead>
        <tbody>{matrix.map((row, rowIndex) => <tr key={classes[rowIndex]} className="border-t"><th className="p-2 text-left font-medium">{classes[rowIndex]}</th>{row.map((value, columnIndex) => <td key={classes[columnIndex]} className={rowIndex === columnIndex ? "bg-primary/10 p-2 tabular-nums font-semibold" : "p-2 tabular-nums"}>{value.toLocaleString()}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

export function ClassificationModelDetail({
  clientId,
  matterId,
  modelId,
}: {
  clientId: string;
  matterId: string;
  modelId: string;
}) {
  const queryClient = useQueryClient();
  const [chosenVersionId, setChosenVersionId] = useState("");
  const [projectionScoringRun, setProjectionScoringRun] = useState<ClassificationScoringRunRead | null>(null);
  const [projectionBatchId, setProjectionBatchId] = useState("");
  const [minimumConfidence, setMinimumConfidence] = useState("90");
  const [maximumConfidence, setMaximumConfidence] = useState("");
  const [selectedClassKeys, setSelectedClassKeys] = useState<string[]>([]);
  const [latestProjection, setLatestProjection] = useState<{ runId: string; batchId: string; batchName: string } | null>(null);
  const [governanceReason, setGovernanceReason] = useState("");
  const [activationPreview, setActivationPreview] = useState<ClassificationPromotionPreview | null>(null);
  const [pendingActivationVersionId, setPendingActivationVersionId] = useState<string | null>(null);
  const base = `/v1/matters/${matterId}/classification-models/${modelId}`;
  const model = useQuery({
    queryKey: ["classification-model", matterId, modelId],
    queryFn: () => coreApi<ClassificationModelRead>(base),
    refetchInterval: (query) => pendingActivationVersionId && query.state.data?.active_model_version_id !== pendingActivationVersionId ? 1500 : false,
  });
  const trainingJobs = useQuery({
    queryKey: ["classification-training-jobs", matterId, modelId],
    queryFn: () => coreApi<ClassificationTrainingJobRead[]>(`${base}/training-jobs`),
    refetchInterval: (query) => query.state.data?.some((job) => !["COMPLETED", "FAILED", "CANCELED"].includes(job.status)) ? 2000 : false,
  });
  const builds = useQuery({
    queryKey: ["classification-builds", matterId, modelId],
    queryFn: () => coreApi<ClassificationTrainingRunRead[]>(`${base}/builds`),
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 2000 : false,
  });
  const versions = useQuery({
    queryKey: ["classification-versions", matterId, modelId],
    queryFn: () => coreApi<ClassificationModelVersionRead[]>(`${base}/versions`),
    refetchInterval: (query) => {
      const hasRunningBuild = builds.data?.some(
        (run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status),
      );
      const hasCompletedBuild = builds.data?.some(
        (run) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status),
      );
      return hasRunningBuild || (hasCompletedBuild && !query.state.data?.length) ? 2000 : false;
    },
  });
  const scoringRuns = useQuery({
    queryKey: ["classification-scoring-runs", matterId, modelId],
    queryFn: () => coreApi<ClassificationScoringRunRead[]>(`${base}/scoring-runs`),
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 2000 : false,
  });
  const batches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
  });
  const selectedVersion = versions.data?.find((version) => version.id === chosenVersionId) ?? versions.data?.[0] ?? null;
  const evaluation = useQuery({
    queryKey: ["classification-evaluation", selectedVersion?.id],
    queryFn: () => coreApi<ClassificationEvaluationReport>(`${base}/versions/${selectedVersion?.id}/evaluation`),
    enabled: Boolean(selectedVersion?.id),
  });
  const projectionActivity = useQuery({
    queryKey: ["classifier-projection-run", matterId, latestProjection?.batchId, latestProjection?.runId],
    queryFn: async () => {
      const runs = await coreApi<ReviewBatchRunRead[]>(`/v1/matters/${matterId}/review-batches/${latestProjection!.batchId}/runs`);
      const run = runs.find((item) => item.id === latestProjection!.runId);
      if (!run) throw new Error("Classifier projection run was not found.");
      return run;
    },
    enabled: Boolean(latestProjection),
    refetchInterval: (query) => ["QUEUED", "RUNNING"].includes(query.state.data?.status ?? "") ? 1000 : false,
  });
  useEffect(() => {
    if (!latestProjection || !projectionActivity.data || ["QUEUED", "RUNNING"].includes(projectionActivity.data.status)) return;
    queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] });
  }, [latestProjection, matterId, projectionActivity.data, queryClient]);
  useEffect(() => {
    if (!pendingActivationVersionId || model.data?.active_model_version_id !== pendingActivationVersionId) return;
    const completionNotice = window.setTimeout(() => {
      setPendingActivationVersionId(null);
      setGovernanceReason("");
      void queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] });
      toast.success("Model version activated.");
    }, 0);
    return () => window.clearTimeout(completionNotice);
  }, [matterId, model.data?.active_model_version_id, pendingActivationVersionId, queryClient]);
  const projectionVersion = projectionScoringRun
    ? versions.data?.find((version) => version.id === projectionScoringRun.model_version_id)
    : null;
  const projectionClasses = projectionVersion?.trained_class_keys ?? projectionVersion?.ordered_class_keys ?? [];
  const compatibleBatches = (batches.data ?? []).filter((batch) => {
    const hasTarget = batch.coding_groups.some((group) => group.fields.some((field) => field.metadata_definition_id === model.data?.target_metadata_definition_id));
    const sourceMatches = projectionScoringRun?.dataset_scope_type === "REVIEW_BATCH"
      ? batch.id === projectionScoringRun.dataset_scope_id
      : batch.selection_type === "ALL_MATTER";
    return batch.status === "READY" && batch.search_status === "READY" && hasTarget && sourceMatches && batch.document_count === projectionScoringRun?.population_document_count;
  });
  const approveVersion = useMutation({
    mutationFn: (versionId: string) => coreApi<ClassificationModelDecisionRead>(`${base}/versions/${versionId}/approve`, {
      method: "POST",
      body: JSON.stringify({ reason: governanceReason.trim() }),
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["classification-model", matterId, modelId] }),
        queryClient.invalidateQueries({ queryKey: ["classification-versions", matterId, modelId] }),
        queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] }),
      ]);
      setGovernanceReason("");
      toast.success("Model version approved. It can now be activated.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The model version could not be approved."),
  });
  const previewActivation = useMutation({
    mutationFn: (versionId: string) => coreApi<ClassificationPromotionPreview>(`${base}/activation/preview`, {
      method: "POST",
      body: JSON.stringify({ model_version_id: versionId }),
    }),
    onSuccess: setActivationPreview,
    onError: (error) => toast.error(error instanceof Error ? error.message : "Activation could not be previewed."),
  });
  const activateVersion = useMutation({
    mutationFn: (preview: ClassificationPromotionPreview) => coreApi<ClassificationPromotionResponse>(`${base}/activation`, {
      method: "POST",
      body: JSON.stringify({
        model_version_id: preview.model_version.id,
        expected_model_row_version: preview.expected_model_row_version,
        preview_token: preview.preview_token,
        reason: governanceReason.trim(),
      }),
    }),
    onSuccess: async (response) => {
      if (response.model) queryClient.setQueryData(["classification-model", matterId, modelId], response.model);
      setActivationPreview(null);
      if (response.status === "COMPLETED") {
        setPendingActivationVersionId(null);
        setGovernanceReason("");
        toast.success("Model version activated.");
      } else {
        setPendingActivationVersionId(response.model_version_id);
        toast.success("Model activation was queued.");
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["classification-model", matterId, modelId] }),
        queryClient.invalidateQueries({ queryKey: ["classification-versions", matterId, modelId] }),
        queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] }),
      ]);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The model version could not be activated."),
  });
  const createProjection = useMutation({
    mutationFn: async () => {
      if (!projectionScoringRun || !projectionBatchId) throw new Error("Choose a compatible review batch.");
      const minimum = Number(minimumConfidence) / 100;
      const maximum = maximumConfidence.trim() ? Number(maximumConfidence) / 100 : null;
      if (!Number.isFinite(minimum) || minimum < 0 || minimum > 1) throw new Error("Minimum confidence must be between 0% and 100%.");
      if (maximum !== null && (!Number.isFinite(maximum) || maximum <= minimum || maximum > 1)) throw new Error("Maximum confidence must be greater than the minimum and no more than 100%.");
      const payload: ReviewBatchClassifierRunCreate = {
        classification_scoring_run_id: projectionScoringRun.id,
        minimum_confidence: minimum,
        maximum_confidence: maximum,
        selected_class_keys: selectedClassKeys,
        idempotency_key: `classifier-projection:${projectionScoringRun.id}:${crypto.randomUUID()}`,
      };
      return coreApi<ReviewBatchRunRead>(`/v1/matters/${matterId}/review-batches/${projectionBatchId}/classifier-runs`, { method: "POST", body: JSON.stringify(payload) });
    },
    onSuccess: (run) => {
      const batchName = compatibleBatches.find((batch) => batch.id === run.review_batch_id)?.name ?? "Review batch";
      queryClient.invalidateQueries({ queryKey: ["review-batch-runs", matterId, run.review_batch_id] });
      queryClient.invalidateQueries({ queryKey: ["review-batch", matterId, run.review_batch_id] });
      setLatestProjection({ runId: run.id, batchId: run.review_batch_id, batchName });
      setProjectionScoringRun(null);
      toast.success("Classifier projection was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Classifier projection could not be created."),
  });
  const cancelTrainingJob = useMutation({
    mutationFn: (jobId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_TRAINING_JOB/${jobId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["classification-training-jobs", matterId, modelId] });
      void queryClient.invalidateQueries({ queryKey: ["classification-builds", matterId, modelId] });
      toast.success("Training was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Training could not be canceled."),
  });
  const retryTrainingJob = useMutation({
    mutationFn: (jobId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_TRAINING_JOB/${jobId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["classification-training-jobs", matterId, modelId] });
      void queryClient.invalidateQueries({ queryKey: ["classification-builds", matterId, modelId] });
      toast.success("Training was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Training could not be retried."),
  });
  const cancelScoringRun = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_SCORING_RUN/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["classification-scoring-runs", matterId, modelId] });
      toast.success("Scoring was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Scoring could not be canceled."),
  });
  const retryScoringRun = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_SCORING_RUN/${runId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["classification-scoring-runs", matterId, modelId] });
      toast.success("Scoring was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Scoring could not be retried."),
  });
  const retryProjection = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_PROJECTION/${runId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["classifier-projection-run", matterId, latestProjection?.batchId, latestProjection?.runId] });
      toast.success("Classifier projection was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Classifier projection could not be retried."),
  });

  const openProjection = (run: ClassificationScoringRunRead) => {
    setProjectionScoringRun(run);
    setProjectionBatchId("");
    setMinimumConfidence("90");
    setMaximumConfidence("");
    setSelectedClassKeys([]);
  };

  if (model.isPending || trainingJobs.isPending || builds.isPending || versions.isPending || scoringRuns.isPending || batches.isPending) return <TableLoading />;
  if (model.error || trainingJobs.error || builds.error || versions.error || scoringRuns.error || batches.error) return <QueryError message={model.error?.message ?? trainingJobs.error?.message ?? builds.error?.message ?? versions.error?.message ?? scoringRuns.error?.message ?? batches.error?.message} />;
  const currentTrainingJobs = trainingJobs.data.slice(0, 1);
  const previousTrainingJobs = trainingJobs.data.slice(1);
  const selectedVersionIsActive = selectedVersion?.id === model.data.active_model_version_id;

  return (
    <div className="space-y-6">
      <div>
        <Button asChild variant="ghost" size="sm" className="-ml-3 mb-2"><Link href={matterHref(clientId, matterId, "classification")}><ArrowLeft />Classification</Link></Button>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><BrainCircuit className="size-6" />{model.data.name}</h1><p className="mt-1 text-sm text-muted-foreground">Predicts {model.data.target_field.display_name} · {model.data.current_configuration?.classification_strategy.toLowerCase().replaceAll("_", " ")}{model.data.target_field.hierarchy_separator && model.data.current_configuration ? ` · ${model.data.current_configuration.hierarchy_level ? `hierarchy level ${model.data.current_configuration.hierarchy_level}` : "full hierarchy path"}` : ""}</p></div>
          <Badge variant={model.data.status === "ACTIVE" && model.data.active_model_version_id ? "accent" : "outline"}>
            {model.data.status === "SUSPENDED" ? "Suspended" : model.data.status === "ARCHIVED" ? "Archived" : model.data.active_model_version_id ? "Activated" : "Not activated"}
          </Badge>
        </div>
      </div>

      <Card>
        <CardHeader><CardTitle className="text-base">Processing status</CardTitle></CardHeader>
        <CardContent>
          {currentTrainingJobs.length ? <div className="space-y-3">{currentTrainingJobs.map((job) => {
            const snapshot = job.dataset_snapshot;
            const run = job.training_run;
            const progress = job.progress_total > 0
              ? Math.min(100, Math.round((job.progress_completed / job.progress_total) * 100))
              : 0;
            return <div key={job.id} className="rounded-lg border p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="font-medium">Training job {shortId(job.id)}</p>
                  <p className="mt-1 text-sm text-muted-foreground">{stageLabel(run?.current_stage ?? job.current_stage)}</p>
                </div>
                <div className="flex items-center gap-2"><Badge variant="outline">{run?.status ?? job.status}</Badge>{job.execution?.available_actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelTrainingJob.isPending} onClick={() => cancelTrainingJob.mutate(job.id)}><Ban />Cancel</Button> : job.execution?.available_actions.includes("RETRY") ? <Button size="sm" variant="outline" disabled={retryTrainingJob.isPending} onClick={() => retryTrainingJob.mutate(job.id)}><RotateCcw />Retry</Button> : null}</div>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-label={`${job.progress_completed} of ${job.progress_total} processing stages completed`}>
                <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${progress}%` }} />
              </div>
              <div className="mt-2 flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
                <span>{run ? `Runtime stage ${run.progress_completed} of ${run.progress_total}` : `${job.progress_completed} of ${job.progress_total} preparation and training stages completed`}</span>
                <span>{job.execution ? `${job.execution.stage_label} · attempt ${job.execution.attempt_number} · ` : ""}Updated {formatDate(job.updated_at)}</span>
              </div>
              {snapshot ? <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2 xl:grid-cols-4">
                <div><span className="text-muted-foreground">In scope</span><p className="font-medium tabular-nums">{snapshot.population_count.toLocaleString()}</p></div>
                <div><span className="text-muted-foreground">Labeled</span><p className="font-medium tabular-nums">{snapshot.labeled_count.toLocaleString()}</p></div>
                <div>
                  <span className="text-muted-foreground">Features processed</span>
                  <p className="font-medium tabular-nums">{((snapshot.feature_ready_count ?? 0) + (snapshot.feature_missing_count ?? 0) + (snapshot.feature_failed_count ?? 0)).toLocaleString()} / {snapshot.labeled_count.toLocaleString()}</p>
                  <p className="text-xs text-muted-foreground">{(snapshot.feature_ready_count ?? 0).toLocaleString()} ready · {(snapshot.feature_missing_count ?? 0).toLocaleString()} missing · {(snapshot.feature_failed_count ?? 0).toLocaleString()} failed</p>
                </div>
                <div><span className="text-muted-foreground">Training eligible</span><p className="font-medium tabular-nums">{snapshot.training_eligible_count.toLocaleString()}</p></div>
              </div> : null}
              {snapshot?.excluded_classes?.length ? <div className="mt-3"><ExcludedClasses exclusions={snapshot.excluded_classes} /></div> : null}
              {job.failure_summary ? <div className="mt-3 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"><p className="font-medium">{job.failure_code ? stageLabel(job.failure_code) : "Training failed"}</p><p className="mt-1">{job.failure_summary}</p>{job.failure_retriable ? <p className="mt-1 text-xs">This failure is marked as retryable.</p> : null}</div> : null}
            </div>;
          })}</div> : <p className="text-sm text-muted-foreground">No durable training job has been launched for this model.</p>}
          {previousTrainingJobs.length ? <details className="mt-4 rounded-lg border px-4 py-3">
            <summary className="cursor-pointer text-sm font-medium">Previous attempts ({previousTrainingJobs.length})</summary>
            <div className="mt-3 space-y-2">{previousTrainingJobs.map((job) => <div key={job.id} className="flex flex-wrap items-start justify-between gap-2 rounded-md bg-muted/30 p-3 text-sm">
              <div><p className="font-medium">Training job {shortId(job.id)}</p><p className="mt-1 text-xs text-muted-foreground">{formatDate(job.created_at)}{job.failure_summary ? ` · ${job.failure_summary}` : ""}</p></div>
              <Badge variant="outline">{job.status}</Badge>
            </div>)}</div>
          </details> : null}
        </CardContent>
      </Card>

      {latestProjection ? <Card>
        <CardHeader><CardTitle className="text-base">Projection activity</CardTitle></CardHeader>
        <CardContent>
          {projectionActivity.isPending ? <p className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin" />Loading projection status…</p> : projectionActivity.error ? <QueryError message={projectionActivity.error.message} /> : projectionActivity.data ? <div className="space-y-3">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="font-medium">{latestProjection.batchName}</p><p className="mt-1 text-xs text-muted-foreground">Projection {shortId(projectionActivity.data.id)} · attempt {projectionActivity.data.execution?.attempt_number ?? 1} · {formatDate(projectionActivity.data.created_at)}</p>{projectionActivity.data.execution ? <p className="mt-1 text-xs text-muted-foreground">{projectionActivity.data.execution.stage_label} · {projectionActivity.data.execution.progress_completed.toLocaleString()} of {(projectionActivity.data.execution.progress_total ?? projectionActivity.data.processed_document_count).toLocaleString()} documents</p> : null}</div><Badge variant="outline">{projectionActivity.data.status}</Badge></div>
            {projectionActivity.data.result_summary ? <div className="grid gap-2 text-sm sm:grid-cols-3"><div><span className="text-muted-foreground">Projected documents</span><p className="font-medium tabular-nums">{Number(projectionActivity.data.result_summary.projected_document_count ?? 0).toLocaleString()}</p></div><div><span className="text-muted-foreground">Projected values</span><p className="font-medium tabular-nums">{Number(projectionActivity.data.result_summary.projected_value_count ?? 0).toLocaleString()}</p></div><div><span className="text-muted-foreground">Failed rows</span><p className="font-medium tabular-nums">{Number(projectionActivity.data.result_summary.failed_document_count ?? 0).toLocaleString()}</p></div></div> : <p className="text-sm text-muted-foreground">The durable worker is verifying the artifact and materializing the selected confidence window.</p>}
            {projectionActivity.data.error_message ? <p className="text-sm text-destructive">{projectionActivity.data.error_message}</p> : null}
            <div className="flex flex-wrap gap-2">{projectionActivity.data.execution?.available_actions.includes("RETRY") ? <Button size="sm" variant="outline" disabled={retryProjection.isPending} onClick={() => retryProjection.mutate(projectionActivity.data.id)}><RotateCcw />Retry projection</Button> : null}<Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}?batch=${latestProjection.batchId}`}>Open review batch</Link></Button></div>
          </div> : null}
        </CardContent>
      </Card> : null}

      <Card>
        <CardHeader><CardTitle className="text-base">Training activity</CardTitle></CardHeader>
        <CardContent>{builds.data.length ? <div className="space-y-2">{builds.data.map((run) => <div key={run.id} className="rounded-lg border p-3"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-medium">Run {shortId(run.id)}</span><Badge variant="outline">{run.status}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{run.current_stage} · seed {run.random_seed} · {run.validation_specification.fold_count ?? 5}-fold validation · {formatDate(run.created_at)}</p>{run.failure_summary ? <p className="mt-2 text-sm text-destructive">{run.failure_summary}</p> : null}</div>)}</div> : <p className="text-sm text-muted-foreground">No training run has been launched.</p>}</CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Matter scoring</CardTitle></CardHeader>
        <CardContent>
          {scoringRuns.data.length ? <div className="space-y-2">{scoringRuns.data.map((run) => <div key={run.id} className="rounded-lg border p-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><p className="font-medium">Scoring run {shortId(run.id)}</p><p className="mt-1 text-xs text-muted-foreground">Model version {versions.data.find((version) => version.id === run.model_version_id)?.version_number ?? shortId(run.model_version_id)} · {run.population_document_count.toLocaleString()} documents · {formatDate(run.created_at)}</p></div>
              <div className="flex items-center gap-2"><Badge variant="outline">{run.status}</Badge>{run.execution?.available_actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelScoringRun.isPending} onClick={() => cancelScoringRun.mutate(run.id)}><Ban />Cancel</Button> : run.execution?.available_actions.includes("RETRY") ? <Button size="sm" variant="outline" disabled={retryScoringRun.isPending} onClick={() => retryScoringRun.mutate(run.id)}><RotateCcw />Retry</Button> : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status) ? <Button size="sm" variant="outline" onClick={() => openProjection(run)}><Layers3 />Create projection</Button> : null}</div>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">{run.execution ? `${run.execution.stage_label} · attempt ${run.execution.attempt_number} · ` : ""}{run.scored_document_count.toLocaleString()} scored · {run.abstained_document_count.toLocaleString()} abstained · {run.excluded_document_count.toLocaleString()} excluded · {run.failed_document_count.toLocaleString()} failed</p>
            {run.failure_summary ? <p className="mt-2 text-sm text-destructive">{run.failure_summary}</p> : null}
          </div>)}</div> : <p className="text-sm text-muted-foreground">No scoring run is available yet. Complete a scoring run for a frozen matter or review-batch population before creating review projections.</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Model quality</CardTitle></CardHeader>
        <CardContent className="space-y-5">
          {!versions.data.length ? <p className="text-sm text-muted-foreground">Quality results will appear after training completes.</p> : <>
            <div className="max-w-sm space-y-2"><Label>Model version</Label><Select value={selectedVersion?.id ?? ""} onValueChange={(value) => { setChosenVersionId(value); setGovernanceReason(""); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{versions.data.map((version) => <SelectItem key={version.id} value={version.id}>Version {version.version_number} · {version.status.toLowerCase()} · {formatDate(version.created_at)}</SelectItem>)}</SelectContent></Select></div>
            {selectedVersion ? <div className="rounded-lg border bg-muted/20 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="font-semibold">Model governance</h2><p className="mt-1 text-sm text-muted-foreground">Analysis Tasks can use a version only after it is approved and activated.</p></div><div className="flex items-center gap-2"><Badge variant="outline">{selectedVersion.status}</Badge>{selectedVersionIsActive ? <Badge variant="accent">Active version</Badge> : null}</div></div>
              {selectedVersion.status === "CANDIDATE" || (selectedVersion.status === "APPROVED" && !selectedVersionIsActive) ? <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end"><div className="space-y-1.5"><Label htmlFor="classification-governance-reason">Decision reason</Label><Input id="classification-governance-reason" value={governanceReason} onChange={(event) => setGovernanceReason(event.target.value)} placeholder={selectedVersion.status === "CANDIDATE" ? "Why this evaluation supports approval" : "Why this version should become active"} /></div>{selectedVersion.status === "CANDIDATE" ? <Button onClick={() => approveVersion.mutate(selectedVersion.id)} disabled={!governanceReason.trim() || approveVersion.isPending}>{approveVersion.isPending ? <LoaderCircle className="animate-spin" /> : null}{approveVersion.isPending ? "Approving…" : "Approve version"}</Button> : <Button onClick={() => previewActivation.mutate(selectedVersion.id)} disabled={!governanceReason.trim() || previewActivation.isPending || pendingActivationVersionId === selectedVersion.id}>{previewActivation.isPending || pendingActivationVersionId === selectedVersion.id ? <LoaderCircle className="animate-spin" /> : null}{pendingActivationVersionId === selectedVersion.id ? "Activation queued…" : previewActivation.isPending ? "Checking…" : "Review activation"}</Button>}</div> : null}
              {selectedVersionIsActive ? <p className="mt-3 text-sm text-muted-foreground">This is the version offered when creating a Classification Analysis Task.</p> : null}
              {["REJECTED", "RETIRED"].includes(selectedVersion.status) ? <p className="mt-3 text-sm text-muted-foreground">This version cannot be activated. Train or select another candidate.</p> : null}
            </div> : null}
            {selectedVersion ? <ExcludedClasses exclusions={selectedVersion.excluded_classes} /> : null}
            {evaluation.isPending ? <TableLoading /> : evaluation.error ? <QueryError message={evaluation.error.message} /> : evaluation.data ? <>
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{Object.entries(evaluation.data.metrics).filter(([, value]) => value !== null).map(([key, value]) => <div key={key} className="rounded-lg border p-3"><p className="text-xs text-muted-foreground">{metricLabel(key)}</p><p className="mt-1 text-xl font-semibold tabular-nums">{metricValue(value)}</p></div>)}</div>
              <div><h2 className="mb-2 font-semibold">Class quality</h2><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="border-b text-xs text-muted-foreground"><tr><th className="py-2 pr-3">Class</th><th className="py-2 pr-3">Support</th><th className="py-2 pr-3">Precision</th><th className="py-2 pr-3">Recall</th><th className="py-2 pr-3">F1</th><th className="py-2">PR AUC</th></tr></thead><tbody>{evaluation.data.ordered_class_keys.map((classKey) => { const metrics = evaluation.data?.per_class_metrics?.[classKey] ?? {}; return <tr key={classKey} className="border-b last:border-0"><td className="py-2 pr-3 font-medium">{classKey}</td><td className="py-2 pr-3">{metricValue(metrics.positive_support ?? metrics.support)}</td><td className="py-2 pr-3">{metricValue(metrics.precision)}</td><td className="py-2 pr-3">{metricValue(metrics.recall)}</td><td className="py-2 pr-3">{metricValue(metrics.f1)}</td><td className="py-2">{metricValue(metrics.pr_auc)}</td></tr>; })}</tbody></table></div></div>
              <div><h2 className="mb-2 font-semibold">Confusion matrix</h2><ConfusionMatrix report={evaluation.data} /></div>
              {evaluation.data.warnings?.length ? <div><h2 className="mb-2 font-semibold">Warnings</h2><ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">{evaluation.data.warnings.map((warning) => <li key={warning}>{warning.replaceAll("_", " ").toLowerCase()}</li>)}</ul></div> : null}
            </> : null}
          </>}
        </CardContent>
      </Card>

      <Dialog open={Boolean(activationPreview)} onOpenChange={(open) => { if (!open && !activateVersion.isPending) setActivationPreview(null); }}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader><DialogTitle>Activate model version?</DialogTitle><DialogDescription>Activation makes this approved version available to Classification Analysis Tasks. Existing task versions remain pinned to their original model version.</DialogDescription></DialogHeader>
          {activationPreview ? <div className="space-y-3 text-sm"><div className="grid gap-2 rounded-lg border p-3 sm:grid-cols-2"><div><span className="text-muted-foreground">Version</span><p className="font-medium">Version {activationPreview.model_version.version_number}</p></div><div><span className="text-muted-foreground">Currently active</span><p className="font-medium">{activationPreview.current_active_model_version_id ? shortId(activationPreview.current_active_model_version_id) : "None"}</p></div></div>{activationPreview.blocking_reasons.length ? <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-destructive"><p className="font-medium">Activation is blocked</p><ul className="mt-1 list-disc pl-5">{activationPreview.blocking_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></div> : <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3">This changes the classifier&apos;s active pointer. Reason: <strong>{governanceReason.trim()}</strong></p>}</div> : null}
          <DialogFooter><Button variant="outline" onClick={() => setActivationPreview(null)} disabled={activateVersion.isPending}>Cancel</Button><Button onClick={() => activationPreview && activateVersion.mutate(activationPreview)} disabled={!activationPreview?.can_promote || activateVersion.isPending}>{activateVersion.isPending ? <LoaderCircle className="animate-spin" /> : null}{activateVersion.isPending ? "Activating…" : "Activate version"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(projectionScoringRun)} onOpenChange={(open) => { if (!open && !createProjection.isPending) setProjectionScoringRun(null); }}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader><DialogTitle>Create classifier projection</DialogTitle><DialogDescription>Reuse this completed scoring artifact as isolated batch coding. This does not change the matter coding field or rerun the model.</DialogDescription></DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2"><Label>Review batch</Label><Select value={projectionBatchId} onValueChange={setProjectionBatchId}><SelectTrigger aria-label="Review batch"><SelectValue placeholder="Choose a compatible batch" /></SelectTrigger><SelectContent>{compatibleBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} documents</SelectItem>)}</SelectContent></Select>{projectionScoringRun && !compatibleBatches.length ? <p className="text-xs text-amber-600">No ready batch has the same frozen scope and includes {model.data.target_field.display_name} in its coding configuration.</p> : <p className="text-xs text-muted-foreground">The worker verifies exact document membership before materializing any values.</p>}</div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-2"><Label htmlFor="projection-minimum">Minimum confidence %</Label><Input id="projection-minimum" type="number" min="0" max="100" step="1" value={minimumConfidence} onChange={(event) => setMinimumConfidence(event.target.value)} /></div>
              <div className="space-y-2"><Label htmlFor="projection-maximum">Maximum confidence %</Label><Input id="projection-maximum" type="number" min="0" max="100" step="1" placeholder="100" value={maximumConfidence} onChange={(event) => setMaximumConfidence(event.target.value)} /></div>
            </div>
            <p className="text-xs text-muted-foreground">The minimum is inclusive and the optional maximum is exclusive. For example, 80 to 90 creates the non-overlapping interval [80%, 90%). Predictions outside this interval remain in the scoring artifact and can be projected later.</p>
            {projectionClasses.length ? <fieldset className="space-y-2"><legend className="text-sm font-medium">Classes</legend><p className="text-xs text-muted-foreground">Leave every class unselected to include all resolved classes.</p><div className="max-h-48 space-y-1 overflow-y-auto rounded-md border p-2">{projectionClasses.map((classKey) => <label key={classKey} className="flex items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" checked={selectedClassKeys.includes(classKey)} onChange={() => setSelectedClassKeys((current) => current.includes(classKey) ? current.filter((item) => item !== classKey) : [...current, classKey])} className="size-4 accent-primary" /><span className="break-all">{classKey}</span></label>)}</div></fieldset> : null}
          </div>
          <DialogFooter><Button variant="outline" onClick={() => setProjectionScoringRun(null)} disabled={createProjection.isPending}>Cancel</Button><Button onClick={() => createProjection.mutate()} disabled={!projectionBatchId || createProjection.isPending}>{createProjection.isPending ? <LoaderCircle className="animate-spin" /> : <Layers3 />}Create projection</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
