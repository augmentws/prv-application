"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { Layers3, LoaderCircle, Search, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import {
  ClassificationPredictionSummaryChart,
  type PredictionThreshold,
} from "@/components/classification-prediction-summary-chart";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type {
  ClassificationPredictionSummaryRead,
  ClassificationScoringRunRead,
  MatterAnalysisTaskRunRead,
  ReviewBatchRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { toast } from "sonner";

type ClassificationAnalysisJob = MatterAnalysisTaskRunRead & { task_name?: string };

export interface ClassificationBatchProjectionContext {
  batchId: string;
  projectionRun?: ReviewBatchRunRead;
  searchableRunId?: string | null;
  searchableStatus?: string | null;
  onProjectionChanged: (run: ReviewBatchRunRead) => void;
  onViewFilters: () => void;
}

export function ClassificationAnalysisJobDetail({
  clientId,
  matterId,
  run,
  onClose,
  batchProjection,
}: {
  clientId: string;
  matterId: string;
  run: ClassificationAnalysisJob;
  onClose?: () => void;
  batchProjection?: ClassificationBatchProjectionContext;
}) {
  const [predictionThreshold, setPredictionThreshold] = useState<PredictionThreshold>("policy");
  const [projectionDialogOpen, setProjectionDialogOpen] = useState(false);
  const [minimumConfidence, setMinimumConfidence] = useState("");
  const [maximumConfidence, setMaximumConfidence] = useState("");
  const [selectedClassKeys, setSelectedClassKeys] = useState<string[]>([]);
  const [batchName, setBatchName] = useState("");
  const [createdBatch, setCreatedBatch] = useState<ReviewBatchRead | null>(null);
  const scoringRunId = run.classification_scoring_run_id;
  const scoring = useQuery({
    queryKey: ["classification-scoring-run", matterId, run.classification_model_id, scoringRunId],
    queryFn: () => coreApi<ClassificationScoringRunRead>(`/v1/matters/${matterId}/classification-models/${run.classification_model_id}/scoring-runs/${scoringRunId}`),
    enabled: Boolean(scoringRunId),
    refetchInterval: (query) => ["QUEUED", "VALIDATING", "WAITING_FOR_RUNTIME", "SCORING", "PUBLISHING", "FINALIZING", "RECONCILING"].includes(query.state.data?.status ?? "") ? 1500 : false,
  });
  const scoringResult = scoring.data;
  const predictionsSaved = Boolean(scoringResult?.prediction_artifact_id);
  const summaryPath = `/v1/matters/${matterId}/classification-models/${run.classification_model_id}/scoring-runs/${scoringRunId}/prediction-summary${
    predictionThreshold === "policy" ? "" : `?minimum_probability=${predictionThreshold}`
  }`;
  const predictionSummary = useQuery({
    queryKey: ["classification-prediction-summary", matterId, run.classification_model_id, scoringRunId, predictionThreshold],
    queryFn: () => coreApi<ClassificationPredictionSummaryRead>(summaryPath),
    enabled: predictionsSaved && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(scoringResult?.status ?? ""),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  const policyThreshold = scoringResult?.scoring_policy.multiclass_minimum_confidence
    ?? scoringResult?.scoring_policy.binary_positive_threshold;
  const perClassThresholds = scoringResult?.scoring_policy.one_vs_rest_thresholds;
  const policy = perClassThresholds && Object.keys(perClassThresholds).length
    ? `${Object.keys(perClassThresholds).length} per-class confidence thresholds`
    : typeof policyThreshold === "number"
      ? `${Math.round(policyThreshold * 100)}% confidence threshold`
      : "Model prediction policy";
  const projectionRun = batchProjection?.projectionRun;
  const projectionSearchReady = Boolean(
    projectionRun
    && batchProjection?.searchableRunId === projectionRun.id
    && batchProjection.searchableStatus === "READY",
  );
  const openProjectionDialog = () => {
    const defaultMinimum = typeof policyThreshold === "number" ? policyThreshold : 0.9;
    setMinimumConfidence(String(Math.round(defaultMinimum * 100)));
    setMaximumConfidence("");
    setSelectedClassKeys([]);
    setBatchName(`${run.task_name ?? "Classification review"} · ≥${Math.round(defaultMinimum * 100)}%`);
    setCreatedBatch(null);
    setProjectionDialogOpen(true);
  };
  const createProjection = useMutation({
    mutationFn: async () => {
      if (!scoringResult) throw new Error("Completed classification output is required.");
      if (!batchName.trim()) throw new Error("Batch name is required.");
      const minimum = Number(minimumConfidence) / 100;
      const maximum = maximumConfidence.trim() ? Number(maximumConfidence) / 100 : null;
      if (!Number.isFinite(minimum) || minimum < 0 || minimum > 1) {
        throw new Error("Minimum confidence must be between 0% and 100%.");
      }
      if (maximum !== null && (!Number.isFinite(maximum) || maximum <= minimum || maximum > 1)) {
        throw new Error("Maximum confidence must be greater than the minimum and no more than 100%.");
      }
      return coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/from-classification-run`, {
        method: "POST",
        body: JSON.stringify({
          classification_analysis_run_id: run.id,
          name: batchName.trim(),
          minimum_confidence: minimum,
          maximum_confidence: maximum,
          selected_class_keys: selectedClassKeys,
          idempotency_key: `classification-review-batch:${run.id}:${crypto.randomUUID()}`,
        }),
      });
    },
    onSuccess: (batch) => {
      setCreatedBatch(batch);
      toast.success("The classification review batch is being created.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The classification review batch could not be created."),
  });
  const classOptions = predictionSummary.data?.class_counts ?? [];

  return <><Card>
    <CardHeader className="border-b">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <CardTitle>{run.task_name ?? "Classification analysis job"}</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            {run.scope_type === "MATTER" ? "Whole matter" : "Review batch"} · started {formatDate(run.created_at)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={run.status} />
          {onClose ? <Button type="button" size="icon" variant="ghost" aria-label="Close job results" onClick={onClose}><X /></Button> : null}
        </div>
      </div>
    </CardHeader>
    <CardContent className="space-y-4 p-4">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <OutcomeMetric label="Population" value={run.population_document_count} />
        <OutcomeMetric label="Features ready" value={run.feature_ready_count ?? 0} />
        <OutcomeMetric label="Features missing" value={run.feature_missing_count ?? 0} />
        <OutcomeMetric label="Features failed" value={run.feature_failed_count ?? 0} />
      </div>

      {scoring.isPending && scoringRunId ? <p className="text-sm text-muted-foreground">Loading classification output…</p> : null}
      {scoring.error ? <p className="text-sm text-destructive">Classification output could not be loaded: {scoring.error.message}</p> : null}
      {scoringResult ? <div className="rounded-md border bg-muted/20 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div><p className="text-sm font-semibold">Scoring output</p><p className="mt-0.5 text-xs text-muted-foreground">{policy} · isolated analysis output; matter coding was not changed.</p></div>
          <div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{friendly(scoringResult.status)}</Badge>{predictionsSaved && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(scoringResult.status) ? <Button type="button" size="sm" variant="outline" onClick={openProjectionDialog}><Layers3 />Create review batch</Button> : null}</div>
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <OutcomeMetric label="Evaluated" value={scoringResult.scored_document_count} />
          <OutcomeMetric label="Abstained" value={scoringResult.abstained_document_count} />
          <OutcomeMetric label="Excluded" value={scoringResult.excluded_document_count} />
          <OutcomeMetric label="Failed" value={scoringResult.failed_document_count} />
        </div>
        {predictionSummary.isPending && predictionsSaved ? <p className="mt-4 border-t pt-3 text-sm text-muted-foreground">Calculating per-class counts from the saved predictions…</p> : null}
        {predictionSummary.error ? <p className="mt-4 border-t pt-3 text-sm text-destructive">Per-class counts could not be loaded: {predictionSummary.error.message}</p> : null}
        {predictionSummary.data ? <ClassificationPredictionSummaryChart
          summary={predictionSummary.data}
          threshold={predictionThreshold}
          onThresholdChange={setPredictionThreshold}
        /> : null}
        {projectionRun ? <div className="mt-4 rounded-md border bg-background p-3"><div className="flex flex-wrap items-start justify-between gap-2"><div><p className="text-sm font-semibold">Batch Coding projection</p><p className="mt-0.5 text-xs text-muted-foreground">{projectionRun.execution?.stage_label ?? friendly(projectionRun.status)} · {(projectionRun.execution?.progress_completed ?? projectionRun.processed_document_count).toLocaleString()} of {(projectionRun.execution?.progress_total ?? run.population_document_count).toLocaleString()} documents</p></div><StatusBadge status={projectionRun.status} /></div>{projectionRun.result_summary ? <p className="mt-2 text-xs text-muted-foreground">{Number(projectionRun.result_summary.projected_document_count ?? 0).toLocaleString()} documents · {Number(projectionRun.result_summary.projected_value_count ?? 0).toLocaleString()} values projected</p> : null}{projectionRun.error_message ? <p className="mt-2 text-xs text-destructive">{projectionRun.error_message}</p> : null}<div className="mt-3 flex flex-wrap gap-2">{projectionSearchReady ? <Button type="button" size="sm" variant="outline" onClick={batchProjection.onViewFilters}><Search />View Batch Coding filters</Button> : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(projectionRun.status) ? <p className="text-xs text-muted-foreground">The projected values are being synchronized to Batch Coding filters.</p> : null}</div></div> : null}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
          <span>{predictionsSaved ? "Predictions saved in the scoring artifact." : "No prediction artifact was produced."}{scoringResult.excluded_document_count ? " Excluded documents received no prediction because usable features were unavailable." : ""}</span>
          <Button asChild size="sm" variant="outline"><Link href={`/app/clients/${clientId}/matters/${matterId}/classification-models/${run.classification_model_id}`}>Open model results</Link></Button>
        </div>
      </div> : !scoringRunId ? <p className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">Scoring output will become available after feature preparation completes.</p> : null}
      {run.failure_summary ? <p className="text-sm text-destructive">{run.failure_summary}</p> : null}
    </CardContent>
  </Card>
  <Dialog open={projectionDialogOpen} onOpenChange={(open) => { if (!createProjection.isPending) setProjectionDialogOpen(open); }}>
    <DialogContent className="sm:max-w-xl">
      <DialogHeader><DialogTitle>Create review batch from classifier values</DialogTitle><DialogDescription>Freeze documents matching this confidence range and expose their predicted values as isolated Batch Coding. Matter coding is not changed.</DialogDescription></DialogHeader>
      {createdBatch ? <div className="space-y-4"><div className="rounded-md border bg-muted/20 p-3"><p className="font-medium">{createdBatch.name}</p><p className="mt-1 text-sm text-muted-foreground">The batch is resolving matching predictions and preparing Batch Coding.</p></div></div> : <div className="space-y-4">
        <div className="space-y-2"><Label htmlFor="classification-batch-name">Batch name</Label><Input id="classification-batch-name" value={batchName} onChange={(event) => setBatchName(event.target.value)} /></div>
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-2"><Label htmlFor="batch-projection-minimum">Minimum confidence %</Label><Input id="batch-projection-minimum" type="number" min="0" max="100" step="1" value={minimumConfidence} onChange={(event) => setMinimumConfidence(event.target.value)} /></div>
          <div className="space-y-2"><Label htmlFor="batch-projection-maximum">Maximum confidence %</Label><Input id="batch-projection-maximum" type="number" min="0" max="100" step="1" placeholder="100" value={maximumConfidence} onChange={(event) => setMaximumConfidence(event.target.value)} /></div>
        </div>
        <p className="text-xs text-muted-foreground">The minimum is inclusive and the optional maximum is exclusive. You can project another non-overlapping confidence range later from the same saved predictions.</p>
        {classOptions.length ? <fieldset className="space-y-2"><legend className="text-sm font-medium">Classes</legend><p className="text-xs text-muted-foreground">Leave every class unselected to include all resolved classes.</p><div className="max-h-48 space-y-1 overflow-y-auto rounded-md border p-2">{classOptions.map((option) => <label key={option.class_key} className="flex items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" checked={selectedClassKeys.includes(option.class_key)} onChange={() => setSelectedClassKeys((current) => current.includes(option.class_key) ? current.filter((item) => item !== option.class_key) : [...current, option.class_key])} className="size-4 accent-primary" /><span className="min-w-0 flex-1 truncate" title={option.class_key}>{option.label}</span><span className="font-mono text-xs text-muted-foreground">{option.document_count.toLocaleString()}</span></label>)}</div></fieldset> : predictionSummary.isPending ? <p className="text-sm text-muted-foreground">Loading available classes…</p> : null}
      </div>}
      <DialogFooter>{createdBatch ? <><Button type="button" variant="outline" onClick={() => setProjectionDialogOpen(false)}>Close</Button><Button asChild><Link href={`/review/matters/${matterId}?batch=${createdBatch.id}`}>Open batch</Link></Button></> : <><Button type="button" variant="outline" onClick={() => setProjectionDialogOpen(false)} disabled={createProjection.isPending}>Cancel</Button><Button type="button" onClick={() => createProjection.mutate()} disabled={createProjection.isPending || !minimumConfidence.trim() || !batchName.trim()}>{createProjection.isPending ? <LoaderCircle className="animate-spin" /> : <Layers3 />}{createProjection.isPending ? "Creating…" : "Create batch"}</Button></>}</DialogFooter>
    </DialogContent>
  </Dialog></>;
}

function OutcomeMetric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-md border bg-background px-3 py-2"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-0.5 text-lg font-semibold tabular-nums">{value.toLocaleString()}</p></div>;
}

function friendly(value: string) {
  return value.toLowerCase().replaceAll("_", " ");
}
