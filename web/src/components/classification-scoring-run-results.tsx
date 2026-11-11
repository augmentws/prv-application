"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, BrainCircuit } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import {
  ClassificationPredictionSummaryChart,
  type PredictionThreshold,
} from "@/components/classification-prediction-summary-chart";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  ClassificationModelRead,
  ClassificationPredictionSummaryRead,
  ClassificationScoringRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { classificationModelHref } from "@/lib/matter-routes";

function OutcomeMetric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-md border bg-background px-3 py-2"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-0.5 text-lg font-semibold tabular-nums">{value.toLocaleString()}</p></div>;
}

export function ClassificationScoringRunResults({
  clientId,
  matterId,
  modelId,
  scoringRunId,
}: {
  clientId: string;
  matterId: string;
  modelId: string;
  scoringRunId: string;
}) {
  const [predictionThreshold, setPredictionThreshold] = useState<PredictionThreshold>("policy");
  const base = `/v1/matters/${matterId}/classification-models/${modelId}`;
  const model = useQuery({
    queryKey: ["classification-model", matterId, modelId],
    queryFn: () => coreApi<ClassificationModelRead>(base),
  });
  const scoring = useQuery({
    queryKey: ["classification-scoring-run", matterId, modelId, scoringRunId],
    queryFn: () => coreApi<ClassificationScoringRunRead>(`${base}/scoring-runs/${scoringRunId}`),
    refetchInterval: (query) => ["QUEUED", "VALIDATING", "WAITING_FOR_RUNTIME", "SCORING", "PUBLISHING", "FINALIZING", "RECONCILING"].includes(query.state.data?.status ?? "") ? 1500 : false,
  });
  const summaryPath = `${base}/scoring-runs/${scoringRunId}/prediction-summary${
    predictionThreshold === "policy" ? "" : `?minimum_probability=${predictionThreshold}`
  }`;
  const summary = useQuery({
    queryKey: ["classification-prediction-summary", matterId, modelId, scoringRunId, predictionThreshold],
    queryFn: () => coreApi<ClassificationPredictionSummaryRead>(summaryPath),
    enabled: Boolean(scoring.data?.prediction_artifact_id)
      && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(scoring.data?.status ?? ""),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  if (model.isPending || scoring.isPending) return <TableLoading />;
  if (model.error || scoring.error) return <QueryError message={model.error?.message ?? scoring.error?.message} />;

  const run = scoring.data;
  const completed = ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status);
  const scopeLabel = run.dataset_scope_type === "MATTER" ? "Whole-matter" : "Review-batch";
  return <div className="space-y-6">
    <div>
      <Button asChild variant="ghost" size="sm" className="-ml-3 mb-2"><Link href={classificationModelHref(clientId, matterId, modelId)}><ArrowLeft />Back to classifier</Link></Button>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold"><BrainCircuit className="size-6" />{model.data.name} scoring results</h1><p className="mt-1 text-sm text-muted-foreground">{scopeLabel} scoring run {run.id.slice(0, 8)} · model version {run.model_version_id.slice(0, 8)} · {formatDate(run.created_at)}</p></div>
        <StatusBadge status={run.status} />
      </div>
    </div>

    <Card>
      <CardHeader><CardTitle className="text-base">Matter scoring result</CardTitle></CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
          <OutcomeMetric label="Population" value={run.population_document_count} />
          <OutcomeMetric label="Evaluated" value={run.scored_document_count} />
          <OutcomeMetric label="Abstained" value={run.abstained_document_count} />
          <OutcomeMetric label="Excluded" value={run.excluded_document_count} />
          <OutcomeMetric label="Failed" value={run.failed_document_count} />
        </div>
        {run.failure_summary ? <p className="mt-4 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{run.failure_summary}</p> : null}
        {!completed ? <p className="mt-4 border-t pt-3 text-sm text-muted-foreground">Detailed prediction statistics will be available when scoring completes.</p> : null}
        {completed && !run.prediction_artifact_id ? <p className="mt-4 border-t pt-3 text-sm text-destructive">This completed scoring run has no prediction artifact.</p> : null}
        {completed && summary.isPending && run.prediction_artifact_id ? <p className="mt-4 border-t pt-3 text-sm text-muted-foreground">Loading prediction statistics…</p> : null}
        {summary.error ? <p className="mt-4 border-t pt-3 text-sm text-destructive">Prediction statistics could not be loaded: {summary.error.message}</p> : null}
        {summary.data ? <ClassificationPredictionSummaryChart summary={summary.data} threshold={predictionThreshold} onThresholdChange={setPredictionThreshold} /> : null}
      </CardContent>
    </Card>
  </div>;
}
