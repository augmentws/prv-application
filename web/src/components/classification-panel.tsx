"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, BrainCircuit, Check, Database, LoaderCircle, Play, Plus, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type {
  ClassificationDatasetSnapshotCreateResponse,
  ClassificationDatasetSnapshotPreview,
  ClassificationDatasetSnapshotRead,
  ClassificationFeatureSplitCreateResponse,
  ClassificationModelBuildCreateResponse,
  ClassificationModelBuildPreview,
  ClassificationModelConfigurationCreate,
  ClassificationModelConfigurationRead,
  ClassificationModelDecisionRead,
  ClassificationModelRead,
  ClassificationModelVersionRead,
  ClassificationPredictionPreviewRead,
  ClassificationPromotionPreview,
  ClassificationPromotionResponse,
  ClassificationScoringCreateResponse,
  ClassificationScoringPreview,
  ClassificationScoringRunRead,
  ClassificationTrainingRunRead,
  MatterEmbeddingJobRead,
  MetadataDefinitionRead,
  ReviewBatchRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

type Strategy = "BINARY" | "MULTICLASS" | "ONE_VS_REST";
type ScopeType = "MATTER" | "REVIEW_BATCH";

function mutationError(error: unknown, fallback: string) {
  toast.error(error instanceof Error ? error.message : fallback);
}

function shortId(value: string | null | undefined) {
  return value ? value.slice(0, 8) : "—";
}

function metricEntries(version: ClassificationModelVersionRead) {
  return Object.entries(version.metric_summary).filter(([, value]) => value !== null).slice(0, 4);
}

export function ClassificationPanel({ matterId, definitions, reviewBatches, embeddingJobs }: {
  matterId: string;
  definitions: MetadataDefinitionRead[];
  reviewBatches: ReviewBatchRead[];
  embeddingJobs: MatterEmbeddingJobRead[];
}) {
  const queryClient = useQueryClient();
  const [chosenModelId, setChosenModelId] = useState<string | null>(null);
  const [newModelName, setNewModelName] = useState("");
  const [targetFieldId, setTargetFieldId] = useState("");
  const [strategy, setStrategy] = useState<Strategy>("BINARY");
  const [positiveValue, setPositiveValue] = useState("");
  const [labelSource, setLabelSource] = useState<"ADJUDICATED_REVIEW" | "HUMAN_REVIEW" | "MATTER_ASSERTED">("MATTER_ASSERTED");
  const [labelRunId, setLabelRunId] = useState("");
  const [chosenEmbeddingJobId, setChosenEmbeddingJobId] = useState<string | null>(null);
  const [scopeType, setScopeType] = useState<ScopeType>("MATTER");
  const [scopeBatchId, setScopeBatchId] = useState("");
  const [datasetPreview, setDatasetPreview] = useState<ClassificationDatasetSnapshotPreview | null>(null);
  const [chosenSnapshotId, setChosenSnapshotId] = useState<string | null>(null);
  const [decisionReason, setDecisionReason] = useState("Validated through the MVP classification workspace.");
  const [chosenVersionId, setChosenVersionId] = useState<string | null>(null);
  const [predictionRunId, setPredictionRunId] = useState<string | null>(null);
  const [predictionOffset, setPredictionOffset] = useState(0);

  const models = useQuery({
    queryKey: ["classification-models", matterId],
    queryFn: () => coreApi<ClassificationModelRead[]>(`/v1/matters/${matterId}/classification-models`),
  });
  const modelId = chosenModelId ?? models.data?.[0]?.id ?? "";
  const model = models.data?.find((item) => item.id === modelId) ?? null;
  const base = modelId ? `/v1/matters/${matterId}/classification-models/${modelId}` : "";

  const snapshots = useQuery({
    queryKey: ["classification-snapshots", matterId, modelId],
    queryFn: () => coreApi<ClassificationDatasetSnapshotRead[]>(`${base}/dataset-snapshots`),
    enabled: Boolean(modelId),
  });
  const builds = useQuery({
    queryKey: ["classification-builds", matterId, modelId],
    queryFn: () => coreApi<ClassificationTrainingRunRead[]>(`${base}/builds`),
    enabled: Boolean(modelId),
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 2000 : false,
  });
  const versions = useQuery({
    queryKey: ["classification-versions", matterId, modelId],
    queryFn: () => coreApi<ClassificationModelVersionRead[]>(`${base}/versions`),
    enabled: Boolean(modelId),
  });
  const scoringRuns = useQuery({
    queryKey: ["classification-scoring-runs", matterId, modelId],
    queryFn: () => coreApi<ClassificationScoringRunRead[]>(`${base}/scoring-runs`),
    enabled: Boolean(modelId),
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 2000 : false,
  });
  const predictions = useQuery({
    queryKey: ["classification-predictions", matterId, modelId, predictionRunId, predictionOffset],
    queryFn: () => coreApi<ClassificationPredictionPreviewRead>(`${base}/scoring-runs/${predictionRunId}/predictions?offset=${predictionOffset}&limit=25`),
    enabled: Boolean(modelId && predictionRunId),
  });
  const labelRuns = useQuery({
    queryKey: ["classification-label-runs", matterId, reviewBatches.map((batch) => batch.id)],
    queryFn: async () => (await Promise.all(reviewBatches.filter((batch) => batch.status === "READY").map(async (batch) => {
      const runs = await coreApi<ReviewBatchRunRead[]>(`/v1/matters/${matterId}/review-batches/${batch.id}/runs`);
      return runs.map((run) => ({ ...run, batch_name: batch.name }));
    }))).flat(),
    enabled: Boolean(modelId && reviewBatches.length),
  });

  const eligibleDefinitions = definitions.filter((definition) => definition.status === "ACTIVE" && definition.reviewable && (definition.type === "BOOLEAN" || (definition.type === "ENUM" && (definition.allowed_values?.filter((value) => value.active !== false).length ?? 0) >= 2)));
  const completedEmbeddingJobs = embeddingJobs.filter((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0);
  const embeddingJobId = chosenEmbeddingJobId ?? completedEmbeddingJobs[0]?.id ?? "";
  const embeddingJob = completedEmbeddingJobs.find((job) => job.id === embeddingJobId);
  const readyBatches = reviewBatches.filter((batch) => batch.status === "READY");
  const eligibleLabelRuns = (labelRuns.data ?? []).filter((run) => run.run_type === "HUMAN" && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status) && (labelSource !== "ADJUDICATED_REVIEW" || run.purpose === "REFERENCE"));
  const snapshot = snapshots.data?.find((item) => item.id === chosenSnapshotId) ?? snapshots.data?.[0] ?? null;
  const readySnapshot = snapshot?.status === "READY_FOR_TRAINING" ? snapshot : snapshots.data?.find((item) => item.status === "READY_FOR_TRAINING") ?? null;
  const approvedVersions = versions.data?.filter((version) => version.status === "APPROVED") ?? [];
  const scoringVersionId = chosenVersionId ?? model?.active_model_version_id ?? approvedVersions[0]?.id ?? "";

  const invalidateModelData = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] }),
      queryClient.invalidateQueries({ queryKey: ["classification-snapshots", matterId, modelId] }),
      queryClient.invalidateQueries({ queryKey: ["classification-builds", matterId, modelId] }),
      queryClient.invalidateQueries({ queryKey: ["classification-versions", matterId, modelId] }),
      queryClient.invalidateQueries({ queryKey: ["classification-scoring-runs", matterId, modelId] }),
    ]);
  };

  const createModel = useMutation({
    mutationFn: () => coreApi<ClassificationModelRead>(`/v1/matters/${matterId}/classification-models`, {
      method: "POST",
      body: JSON.stringify({ target_metadata_definition_id: targetFieldId, name: newModelName }),
    }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] });
      setChosenModelId(created.id);
      setNewModelName("");
      setTargetFieldId("");
      toast.success(`${created.name} was created.`);
    },
    onError: (error) => mutationError(error, "The classifier could not be created."),
  });

  const classValues = useMemo(() => {
    if (!model) return [];
    return model.target_field.metadata_type === "BOOLEAN" ? ["false", "true"] : model.target_field.active_value_keys;
  }, [model]);
  const selectedPositiveValue = classValues.includes(positiveValue) ? positiveValue : classValues.at(-1) ?? "";
  const effectiveStrategy: Strategy = model?.target_field.metadata_type === "BOOLEAN" ? "BINARY" : strategy;

  const configure = useMutation({
    mutationFn: async () => {
      if (!model || !embeddingJob) throw new Error("A completed embedding job is required.");
      if (labelSource !== "MATTER_ASSERTED" && !labelRunId) throw new Error("Choose a completed human review run for the label source.");
      const keys = classValues;
      const binary = effectiveStrategy === "BINARY";
      const payload: ClassificationModelConfigurationCreate = {
        classification_strategy: effectiveStrategy,
        ordered_class_keys: binary ? ["negative", "positive"] : keys,
        binary_mapping: binary ? {
          negative_class_key: "negative",
          positive_class_key: "positive",
          negative_value_keys: keys.filter((key) => key !== selectedPositiveValue),
          positive_value_keys: [selectedPositiveValue],
        } : null,
        label_policy: {
          allowed_sources: [{ kind: labelSource, authority: labelSource === "ADJUDICATED_REVIEW" ? "GOLD" : "SILVER", source_weight: 1, ...(labelSource === "MATTER_ASSERTED" ? {} : { source_reference_id: labelRunId }) }],
          missing_label_action: "EXCLUDE",
          source_conflict_action: "REQUIRE_ADJUDICATION",
          target_value_conflict_action: "EXCLUDE",
        },
        feature_contract: {
          dimensions: embeddingJob.embedding_dimensions,
          embedding_model: embeddingJob.embedding_model,
          embedding_model_revision: embeddingJob.embedding_model_revision,
          text_source_policy: "active_text",
        },
        prediction_resolution_policy: binary
          ? { binary_positive_threshold: 0.5 }
          : effectiveStrategy === "MULTICLASS"
            ? { multiclass_minimum_confidence: 0.5 }
            : {
                one_vs_rest_thresholds: Object.fromEntries(keys.map((key) => [key, 0.5])),
                ...(model.target_field.cardinality === "SINGLE" ? { zero_match_action: "ABSTAIN" as const, multiple_match_action: "HIGHEST_PROBABILITY" as const } : {}),
              },
        trainer_specification: { kind: "sklearn_logistic_regression", parameters: { regularization: "l2", c_values: [0.1, 1, 10], maximum_iterations: 1000 } },
      };
      return coreApi<ClassificationModelConfigurationRead>(`${base}/configurations`, { method: "POST", body: JSON.stringify(payload) });
    },
    onSuccess: async () => { await invalidateModelData(); toast.success("A new immutable configuration revision was created."); },
    onError: (error) => mutationError(error, "The classifier configuration could not be saved."),
  });

  function snapshotRequest() {
    if (!model?.current_configuration_id) throw new Error("Configure the classifier first.");
    if (scopeType === "REVIEW_BATCH" && !scopeBatchId) throw new Error("Choose a ready review batch.");
    return {
      configuration_id: model.current_configuration_id,
      purpose: "TRAINING" as const,
      training_scope: { scope_type: scopeType, scope_id: scopeType === "MATTER" ? matterId : scopeBatchId },
    };
  }

  const previewDataset = useMutation({
    mutationFn: () => coreApi<ClassificationDatasetSnapshotPreview>(`${base}/dataset-snapshots/preview`, { method: "POST", body: JSON.stringify(snapshotRequest()) }),
    onSuccess: setDatasetPreview,
    onError: (error) => mutationError(error, "The training set could not be previewed."),
  });
  const createDataset = useMutation({
    mutationFn: () => coreApi<ClassificationDatasetSnapshotCreateResponse>(`${base}/dataset-snapshots`, { method: "POST", body: JSON.stringify(snapshotRequest()) }),
    onSuccess: async (result) => {
      await queryClient.invalidateQueries({ queryKey: ["classification-snapshots", matterId, modelId] });
      setChosenSnapshotId(result.snapshot.id);
      setDatasetPreview(null);
      toast.success(result.created ? "The immutable training snapshot was created." : "The matching training snapshot already exists.");
    },
    onError: (error) => mutationError(error, "The training snapshot could not be created."),
  });
  const materializeFeatures = useMutation({
    mutationFn: () => {
      if (!snapshot || !embeddingJobId) throw new Error("Choose a snapshot and completed embedding job.");
      return coreApi<ClassificationFeatureSplitCreateResponse>(`${base}/dataset-snapshots/${snapshot.id}/features-and-splits`, { method: "POST", body: JSON.stringify({ embedding_job_id: embeddingJobId }) });
    },
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ["classification-snapshots", matterId, modelId] }); toast.success("Features and validation splits are ready."); },
    onError: (error) => mutationError(error, "Features and splits could not be materialized."),
  });
  const launchBuild = useMutation({
    mutationFn: async () => {
      if (!readySnapshot) throw new Error("Choose a training-ready dataset snapshot.");
      const preview = await coreApi<ClassificationModelBuildPreview>(`${base}/builds/preview`, { method: "POST", body: JSON.stringify({ dataset_snapshot_id: readySnapshot.id }) });
      if (!preview.can_launch) throw new Error(preview.blocking_errors.map((issue) => issue.message).join(" ") || "The build preview blocked launch.");
      return coreApi<ClassificationModelBuildCreateResponse>(`${base}/builds`, { method: "POST", body: JSON.stringify({ dataset_snapshot_id: readySnapshot.id, idempotency_key: crypto.randomUUID() }) });
    },
    onSuccess: async (result) => { await queryClient.invalidateQueries({ queryKey: ["classification-builds", matterId, modelId] }); toast.success(result.created ? "The model build was queued." : "The matching model build already exists."); },
    onError: (error) => mutationError(error, "The model build could not be launched."),
  });

  const decideVersion = useMutation({
    mutationFn: ({ versionId, action }: { versionId: string; action: "approve" | "reject" | "retire" }) => {
      if (!decisionReason.trim()) throw new Error("Enter a decision reason.");
      return coreApi<ClassificationModelDecisionRead>(`${base}/versions/${versionId}/${action}`, { method: "POST", body: JSON.stringify({ reason: decisionReason }) });
    },
    onSuccess: async (decision) => { await invalidateModelData(); toast.success(`${decision.decision_type.toLowerCase()} was recorded.`); },
    onError: (error) => mutationError(error, "The model-version decision could not be recorded."),
  });
  const activateVersion = useMutation({
    mutationFn: async (versionId: string) => {
      if (!decisionReason.trim()) throw new Error("Enter a decision reason.");
      const preview = await coreApi<ClassificationPromotionPreview>(`${base}/activation/preview`, { method: "POST", body: JSON.stringify({ model_version_id: versionId }) });
      if (!preview.can_promote) throw new Error(preview.blocking_reasons.join(" ") || "Activation is blocked.");
      return coreApi<ClassificationPromotionResponse>(`${base}/activation`, { method: "POST", body: JSON.stringify({ model_version_id: versionId, expected_model_row_version: preview.expected_model_row_version, preview_token: preview.preview_token, reason: decisionReason }) });
    },
    onSuccess: async () => { await invalidateModelData(); toast.success("Model activation was accepted."); },
    onError: (error) => mutationError(error, "The model version could not be activated."),
  });
  const launchScoring = useMutation({
    mutationFn: async () => {
      if (!readySnapshot || !scoringVersionId) throw new Error("Choose a ready snapshot and approved model version.");
      const request = { dataset_snapshot_id: readySnapshot.id, model_version_id: scoringVersionId };
      const preview = await coreApi<ClassificationScoringPreview>(`${base}/scoring-runs/preview`, { method: "POST", body: JSON.stringify(request) });
      if (!preview.can_launch) throw new Error(preview.blocking_errors.map((issue) => issue.message).join(" ") || "The scoring preview blocked launch.");
      return coreApi<ClassificationScoringCreateResponse>(`${base}/scoring-runs`, { method: "POST", body: JSON.stringify({ ...request, idempotency_key: crypto.randomUUID() }) });
    },
    onSuccess: async (result) => { await queryClient.invalidateQueries({ queryKey: ["classification-scoring-runs", matterId, modelId] }); toast.success(result.created ? "The scoring run was queued." : "The matching scoring run already exists."); },
    onError: (error) => mutationError(error, "The scoring run could not be launched."),
  });

  if (models.isPending) return <TableLoading />;
  if (models.error) return <QueryError message={models.error.message} />;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Classification validation workspace</h2>
        <p className="text-sm text-muted-foreground">Admin-only controls for exercising the statistical-classification backend. Scoring results remain previews and do not update document metadata.</p>
      </div>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-base"><BrainCircuit className="size-4" />1. Classifier registry</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          {models.data.length ? <div className="space-y-2"><Label>Classifier</Label><Select value={modelId} onValueChange={(value) => { setChosenModelId(value); setChosenSnapshotId(null); setChosenVersionId(null); setPredictionRunId(null); setDatasetPreview(null); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{models.data.map((item) => <SelectItem key={item.id} value={item.id}>{item.name} · {item.target_field.display_name}</SelectItem>)}</SelectContent></Select></div> : <p className="text-sm text-muted-foreground">No classifiers have been registered for this matter.</p>}
          <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
            <div className="space-y-2"><Label htmlFor="classifier-name">New classifier name</Label><Input id="classifier-name" value={newModelName} onChange={(event) => setNewModelName(event.target.value)} placeholder="Responsiveness classifier" /></div>
            <div className="space-y-2"><Label>Target field</Label><Select value={targetFieldId} onValueChange={setTargetFieldId}><SelectTrigger><SelectValue placeholder="Choose a reviewable field" /></SelectTrigger><SelectContent>{eligibleDefinitions.map((definition) => <SelectItem key={definition.id} value={definition.id}>{definition.display_name} · {definition.type}/{definition.cardinality}</SelectItem>)}</SelectContent></Select></div>
            <Button className="self-end" onClick={() => createModel.mutate()} disabled={!newModelName.trim() || !targetFieldId || createModel.isPending}>{createModel.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}Create</Button>
          </div>
        </CardContent>
      </Card>

      {!model ? null : <>
        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><ShieldCheck className="size-4" />2. Immutable configuration</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap gap-2"><Badge variant="outline">Field: {model.target_field.display_name}</Badge><Badge variant="outline">Key: {model.target_field.stable_key}</Badge><Badge variant="outline">Revision: {model.current_configuration?.revision ?? "not configured"}</Badge><Badge variant="outline">Active version: {shortId(model.active_model_version_id)}</Badge></div>
            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <div className="space-y-2"><Label>Strategy</Label><Select value={effectiveStrategy} onValueChange={(value) => setStrategy(value as Strategy)} disabled={model.target_field.metadata_type === "BOOLEAN"}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="BINARY">Binary</SelectItem><SelectItem value="MULTICLASS" disabled={model.target_field.cardinality === "MULTIPLE"}>Multiclass</SelectItem><SelectItem value="ONE_VS_REST">One vs rest</SelectItem></SelectContent></Select></div>
              {effectiveStrategy === "BINARY" ? <div className="space-y-2"><Label>Positive value</Label><Select value={selectedPositiveValue} onValueChange={setPositiveValue}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{classValues.map((key) => <SelectItem key={key} value={key}>{key}</SelectItem>)}</SelectContent></Select></div> : null}
              <div className="space-y-2"><Label>Label source</Label><Select value={labelSource} onValueChange={(value) => { setLabelSource(value as typeof labelSource); setLabelRunId(""); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="MATTER_ASSERTED">Current matter coding</SelectItem><SelectItem value="ADJUDICATED_REVIEW">Adjudicated review run</SelectItem><SelectItem value="HUMAN_REVIEW">Human review run</SelectItem></SelectContent></Select></div>
              {labelSource !== "MATTER_ASSERTED" ? <div className="space-y-2"><Label>Pinned label run</Label><Select value={labelRunId} onValueChange={setLabelRunId}><SelectTrigger><SelectValue placeholder={labelRuns.isPending ? "Loading runs…" : "Choose a completed run"} /></SelectTrigger><SelectContent>{eligibleLabelRuns.map((run) => <SelectItem key={run.id} value={run.id}>{run.batch_name} · {run.purpose} · {shortId(run.id)}</SelectItem>)}</SelectContent></Select></div> : null}
              <div className="space-y-2"><Label>Embedding contract</Label><Select value={embeddingJobId} onValueChange={setChosenEmbeddingJobId}><SelectTrigger><SelectValue placeholder="Choose completed embeddings" /></SelectTrigger><SelectContent>{completedEmbeddingJobs.map((job) => <SelectItem key={job.id} value={job.id}>{job.embedding_model} · {job.embedding_dimensions}d · {shortId(job.id)}</SelectItem>)}</SelectContent></Select></div>
            </div>
            <div className="flex items-center justify-between gap-3"><p className="text-xs text-muted-foreground">MVP defaults: weight 1.0, missing labels excluded, conflicts require adjudication, logistic regression with C 0.1/1/10.</p><Button onClick={() => configure.mutate()} disabled={!embeddingJob || configure.isPending}>{configure.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}Save revision</Button></div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Database className="size-4" />3. Training dataset</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2"><Label>Scope</Label><Select value={scopeType} onValueChange={(value) => { setScopeType(value as ScopeType); setDatasetPreview(null); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="MATTER">Entire matter</SelectItem><SelectItem value="REVIEW_BATCH">Ready review batch</SelectItem></SelectContent></Select></div>
              {scopeType === "REVIEW_BATCH" ? <div className="space-y-2"><Label>Review batch</Label><Select value={scopeBatchId} onValueChange={(value) => { setScopeBatchId(value); setDatasetPreview(null); }}><SelectTrigger><SelectValue placeholder="Choose a batch" /></SelectTrigger><SelectContent>{readyBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()}</SelectItem>)}</SelectContent></Select></div> : null}
              <div className="flex items-end gap-2"><Button variant="outline" onClick={() => previewDataset.mutate()} disabled={!model.current_configuration_id || previewDataset.isPending}>{previewDataset.isPending ? <LoaderCircle className="animate-spin" /> : null}Preview</Button><Button onClick={() => createDataset.mutate()} disabled={!datasetPreview?.can_materialize || createDataset.isPending}>Create snapshot</Button></div>
            </div>
            {datasetPreview ? <div className="rounded-lg border bg-muted/30 p-3 text-sm"><div className="flex flex-wrap gap-4"><span>{datasetPreview.population_count.toLocaleString()} documents</span><span>{datasetPreview.labeled_count.toLocaleString()} labeled</span><span>{datasetPreview.excluded_count.toLocaleString()} excluded</span><span>{datasetPreview.conflicting_count.toLocaleString()} conflicts</span></div>{datasetPreview.blocking_errors.map((issue) => <p key={issue.code} className="mt-2 text-destructive">{issue.message}</p>)}</div> : null}
            {snapshots.isPending ? <TableLoading /> : snapshots.error ? <QueryError message={snapshots.error.message} /> : snapshots.data?.length ? <div className="space-y-2"><Label>Snapshot used by the next build or score</Label><Select value={snapshot?.id ?? ""} onValueChange={setChosenSnapshotId}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{snapshots.data.map((item) => <SelectItem key={item.id} value={item.id}>{item.status} · {item.population_count.toLocaleString()} documents · {shortId(item.id)}</SelectItem>)}</SelectContent></Select>{snapshot ? <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><Badge variant="outline">{snapshot.status}</Badge><span>{snapshot.labeled_count.toLocaleString()} labeled</span><span>{snapshot.training_eligible_count.toLocaleString()} training eligible</span>{snapshot.failure_summary ? <span className="text-destructive">{snapshot.failure_summary}</span> : null}{snapshot.status === "READY_FOR_FEATURES" ? <Button size="sm" variant="outline" onClick={() => materializeFeatures.mutate()} disabled={!embeddingJobId || materializeFeatures.isPending}>Materialize features + splits</Button> : null}</div> : null}</div> : <p className="text-sm text-muted-foreground">No immutable dataset snapshots yet.</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Activity className="size-4" />4. Build and govern versions</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-muted-foreground">Launch performs a server-side preview before queueing the durable training workflow.</p><Button onClick={() => launchBuild.mutate()} disabled={!readySnapshot || launchBuild.isPending}>{launchBuild.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Build model</Button></div>
            {builds.error ? <QueryError message={builds.error.message} /> : builds.data?.length ? <div className="grid gap-2 md:grid-cols-2">{builds.data.map((run) => <div key={run.id} className="rounded-lg border p-3 text-sm"><div className="flex items-center justify-between"><span className="font-medium">Build {shortId(run.id)}</span><Badge variant="outline">{run.status}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{run.current_stage} · attempt {run.attempt_number} · {formatDate(run.created_at)}</p>{run.failure_summary ? <p className="mt-2 text-xs text-destructive">{run.failure_summary}</p> : null}</div>)}</div> : <p className="text-sm text-muted-foreground">No model builds yet.</p>}
            <div className="space-y-2"><Label htmlFor="classification-decision-reason">Decision reason</Label><Input id="classification-decision-reason" value={decisionReason} onChange={(event) => setDecisionReason(event.target.value)} /></div>
            {versions.error ? <QueryError message={versions.error.message} /> : versions.data?.length ? <div className="space-y-3">{versions.data.map((version) => <div key={version.id} className="rounded-lg border p-3"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex items-center gap-2"><span className="font-semibold">Version {version.version_number}</span><Badge variant="outline">{version.status}</Badge>{model.active_model_version_id === version.id ? <Badge>Active</Badge> : null}</div><div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">{metricEntries(version).map(([key, value]) => <span key={key}>{key}: {typeof value === "number" ? value.toFixed(3) : value}</span>)}</div></div><div className="flex flex-wrap gap-2">{version.status === "CANDIDATE" ? <><Button size="sm" onClick={() => decideVersion.mutate({ versionId: version.id, action: "approve" })}>Approve</Button><Button size="sm" variant="outline" onClick={() => decideVersion.mutate({ versionId: version.id, action: "reject" })}>Reject</Button></> : null}{version.status === "APPROVED" && model.active_model_version_id !== version.id ? <Button size="sm" onClick={() => activateVersion.mutate(version.id)}>Activate</Button> : null}{version.status === "APPROVED" && model.active_model_version_id !== version.id ? <Button size="sm" variant="outline" onClick={() => decideVersion.mutate({ versionId: version.id, action: "retire" })}>Retire</Button> : null}</div></div></div>)}</div> : <p className="text-sm text-muted-foreground">A completed build will create a candidate version here.</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Play className="size-4" />5. Score and inspect</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 md:grid-cols-[1fr_auto]"><div className="space-y-2"><Label>Approved version</Label><Select value={scoringVersionId} onValueChange={setChosenVersionId}><SelectTrigger><SelectValue placeholder="Choose an approved version" /></SelectTrigger><SelectContent>{approvedVersions.map((version) => <SelectItem key={version.id} value={version.id}>Version {version.version_number}{model.active_model_version_id === version.id ? " · active" : ""}</SelectItem>)}</SelectContent></Select></div><Button className="self-end" onClick={() => launchScoring.mutate()} disabled={!readySnapshot || !scoringVersionId || launchScoring.isPending}>{launchScoring.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Score snapshot</Button></div>
            {scoringRuns.error ? <QueryError message={scoringRuns.error.message} /> : scoringRuns.data?.length ? <div className="space-y-2">{scoringRuns.data.map((run) => <div key={run.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3 text-sm"><div><div className="flex items-center gap-2"><span className="font-medium">Score {shortId(run.id)}</span><Badge variant="outline">{run.status}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{run.scored_document_count.toLocaleString()} scored · {run.abstained_document_count.toLocaleString()} abstained · {run.excluded_document_count.toLocaleString()} excluded · {run.failed_document_count.toLocaleString()} failed</p></div>{["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(run.status) ? <Button size="sm" variant="outline" onClick={() => { setPredictionRunId(run.id); setPredictionOffset(0); }}>Inspect predictions</Button> : null}</div>)}</div> : <p className="text-sm text-muted-foreground">No scoring runs yet.</p>}
            {predictionRunId ? <div className="space-y-3 rounded-lg border p-3"><div className="flex items-center justify-between"><div><p className="font-medium">Prediction sample</p><p className="text-xs text-muted-foreground">Read-only, hash-verified rows from the immutable prediction artifact.</p></div><Button size="sm" variant="outline" onClick={() => { setPredictionRunId(null); setPredictionOffset(0); }}>Close</Button></div>{predictions.isPending ? <TableLoading /> : predictions.error ? <QueryError message={predictions.error.message} /> : predictions.data ? <><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="border-b text-xs text-muted-foreground"><tr><th className="py-2 pr-3">Document</th><th className="py-2 pr-3">Status</th><th className="py-2 pr-3">Prediction</th><th className="py-2 pr-3">Probabilities</th><th className="py-2">Uncertainty</th></tr></thead><tbody>{predictions.data.items.map((row) => <tr key={row.matter_document_id} className="border-b last:border-0"><td className="py-2 pr-3 font-mono text-xs">{shortId(row.matter_document_id)}</td><td className="py-2 pr-3"><Badge variant="outline">{row.result_status}</Badge></td><td className="py-2 pr-3">{row.predicted_class_keys.join(", ") || (row.abstained ? "Abstained" : "—")}</td><td className="py-2 pr-3 text-xs">{row.probabilities.map((value, index) => `${predictions.data?.ordered_class_keys[index] ?? index}: ${value.toFixed(3)}`).join(" · ") || "—"}</td><td className="py-2">{row.uncertainty === null ? "—" : row.uncertainty.toFixed(3)}</td></tr>)}</tbody></table></div><div className="flex items-center justify-between"><span className="text-xs text-muted-foreground">Rows {predictions.data.total ? predictions.data.offset + 1 : 0}–{Math.min(predictions.data.offset + predictions.data.items.length, predictions.data.total)} of {predictions.data.total}</span><div className="flex gap-2"><Button size="sm" variant="outline" onClick={() => setPredictionOffset(Math.max(0, predictionOffset - 25))} disabled={predictionOffset === 0}>Previous</Button><Button size="sm" variant="outline" onClick={() => setPredictionOffset(predictionOffset + 25)} disabled={predictionOffset + predictions.data.items.length >= predictions.data.total}>Next</Button></div></div></> : null}</div> : null}
          </CardContent>
        </Card>
      </>}
    </div>
  );
}
