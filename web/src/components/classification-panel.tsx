"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BrainCircuit, ChartColumn, ChevronRight, Database, LoaderCircle, Play, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import {
  ClassificationDistributionChart,
  type ClassificationDistribution,
} from "@/components/classification-distribution-chart";
import { QueryError, TableLoading } from "@/components/query-state";
import { JobOutcomesDialog } from "@/components/job-outcomes-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type {
  ClassificationModelConfigurationCreate,
  ClassificationModelConfigurationRead,
  ClassificationModelRead,
  ClassificationTargetDistributionRead,
  ClassificationTrainingJobCreateResponse,
  MatterFacetValuesResponse,
  MatterEmbeddingJobRead,
  MetadataDefinitionRead,
  ReviewBatchRead,
} from "@/generated/models";
import { ApiError, coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

type Strategy = "BINARY" | "MULTICLASS" | "ONE_VS_REST";
type ScopeType = "MATTER" | "REVIEW_BATCH";
type TrainingStage = "REGISTERING" | "LAUNCHING" | null;

const validationOptions = [
  { value: "10", folds: 10, label: "90% train / 10% validation" },
  { value: "20", folds: 5, label: "80% train / 20% validation" },
  { value: "25", folds: 4, label: "75% train / 25% validation" },
  { value: "50", folds: 2, label: "50% train / 50% validation" },
] as const;

function defaultStrategy(field: MetadataDefinitionRead): Strategy {
  if (field.type === "BOOLEAN") return "BINARY";
  if (field.cardinality === "MULTIPLE") return "ONE_VS_REST";
  return "MULTICLASS";
}

function stageLabel(stage: TrainingStage) {
  if (stage === "REGISTERING") return "Creating model and configuration…";
  if (stage === "LAUNCHING") return "Queueing durable training job…";
  return "Train model";
}

function parsePositiveNumbers(value: string) {
  const values = value.split(",").map((entry) => Number(entry.trim()));
  if (!values.length || values.some((entry) => !Number.isFinite(entry) || entry <= 0)) {
    throw new Error("C candidates must be positive numbers separated by commas.");
  }
  return [...new Set(values)];
}

function hasUsableEmbeddingOutput(job: MatterEmbeddingJobRead) {
  if (job.chunk_count <= 0) return false;
  if (["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status)) return true;
  return job.status === "CANCELED" && job.total_count > 0 && job.processed_count === job.total_count;
}

function facetDistribution(
  field: MetadataDefinitionRead,
  response: MatterFacetValuesResponse,
): ClassificationDistribution {
  const configured = field.type === "BOOLEAN"
    ? [{ key: "false", label: "False" }, { key: "true", label: "True" }]
    : (field.allowed_values ?? []).filter((value) => value.active !== false);
  const configuredLabels = new Map(configured.map((value) => [value.key, value.label]));
  const counts = new Map<string, { label: string; document_count: number }>();
  for (const item of response.values) {
    const classKey = typeof item.value === "boolean"
      ? String(item.value).toLowerCase()
      : String(item.value);
    counts.set(classKey, {
      label: item.label ?? configuredLabels.get(classKey) ?? classKey,
      document_count: item.count,
    });
  }
  for (const value of configured) {
    if (!counts.has(value.key)) counts.set(value.key, { label: value.label, document_count: 0 });
  }
  const totalCount = response.total_count ?? 0;
  const missingCount = response.missing_count ?? 0;
  const labeledCount = Math.max(0, totalCount - missingCount);
  return {
    population_count: totalCount,
    labeled_document_count: labeledCount,
    unlabeled_document_count: missingCount,
    conflicting_document_count: 0,
    class_counts: [...counts].map(([class_key, value]) => ({ class_key, ...value })),
  };
}

export function ClassificationPanel({
  clientId,
  matterId,
  definitions,
  reviewBatches,
  embeddingJobs,
}: {
  clientId: string;
  matterId: string;
  definitions: MetadataDefinitionRead[];
  reviewBatches: ReviewBatchRead[];
  embeddingJobs: MatterEmbeddingJobRead[];
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [selectedFieldId, setSelectedFieldId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [strategy, setStrategy] = useState<Strategy>("MULTICLASS");
  const [hierarchyLevel, setHierarchyLevel] = useState("");
  const [scopeType, setScopeType] = useState<ScopeType>("MATTER");
  const [scopeBatchId, setScopeBatchId] = useState("");
  const [embeddingJobId, setEmbeddingJobId] = useState("");
  const [validationPercent, setValidationPercent] = useState("20");
  const [randomSeed, setRandomSeed] = useState("42");
  const [minimumLabels, setMinimumLabels] = useState("20");
  const [minimumPerClass, setMinimumPerClass] = useState("5");
  const [cValues, setCValues] = useState("0.1, 1, 10");
  const [maximumIterations, setMaximumIterations] = useState("1000");
  const [trainingStage, setTrainingStage] = useState<TrainingStage>(null);

  const models = useQuery({
    queryKey: ["classification-models", matterId],
    queryFn: () => coreApi<ClassificationModelRead[]>(`/v1/matters/${matterId}/classification-models`),
  });

  const codingFields = useMemo(
    () => definitions.filter((definition) => (
      definition.status === "ACTIVE"
      && definition.value_source === "ASSERTED"
      && ["BOOLEAN", "ENUM", "TEXT", "LONG_TEXT"].includes(definition.type)
    )),
    [definitions],
  );
  const selectedField = codingFields.find((field) => field.id === selectedFieldId) ?? null;
  const fieldModels = (models.data ?? []).filter(
    (model) => model.target_metadata_definition_id === selectedFieldId,
  );
  const usableEmbeddingJobs = embeddingJobs.filter(hasUsableEmbeddingOutput);
  const activeEmbeddingJobId = usableEmbeddingJobs.some((job) => job.id === embeddingJobId)
    ? embeddingJobId
    : usableEmbeddingJobs[0]?.id ?? "";
  const activeEmbeddingJob = usableEmbeddingJobs.find((job) => job.id === activeEmbeddingJobId);
  const runningEmbeddingJob = embeddingJobs.find((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status));
  const readyBatches = reviewBatches.filter((batch) => batch.status === "READY");
  const distributionScopeId = scopeType === "MATTER" ? matterId : scopeBatchId;
  const waitingForDistributionScope = scopeType === "REVIEW_BATCH" && !scopeBatchId;
  const parsedHierarchyLevel = Number(hierarchyLevel);
  const distributionHierarchyLevel = hierarchyLevel.trim()
    && Number.isInteger(parsedHierarchyLevel)
    && parsedHierarchyLevel >= 1
    && parsedHierarchyLevel <= 32
    ? parsedHierarchyLevel
    : null;
  const distribution = useQuery({
    queryKey: ["classification-target-distribution", matterId, selectedFieldId, scopeType, distributionScopeId, distributionHierarchyLevel],
    queryFn: async (): Promise<ClassificationDistribution> => {
      if (!selectedField) throw new Error("Choose a coding field.");
      if (selectedField.hierarchy_separator) {
        return coreApi<ClassificationTargetDistributionRead>(
          `/v1/matters/${matterId}/classification-models/targets/${selectedField.id}/distribution`,
          {
            method: "POST",
            body: JSON.stringify({
              training_scope: {
                scope_type: scopeType,
                scope_id: distributionScopeId,
              },
              hierarchy_level: distributionHierarchyLevel,
            }),
          },
        );
      }
      const path = scopeType === "MATTER"
        ? `/v1/matters/${matterId}/facets/${selectedField.key}/values`
        : `/v1/matters/${matterId}/review-batches/${distributionScopeId}/facets/${selectedField.key}/values`;
      try {
        const response = await coreApi<MatterFacetValuesResponse>(path, {
          method: "POST",
          body: JSON.stringify({
            search: {
              query: null,
              search_mode: "KEYWORD",
              minimum_similarity: null,
              candidate_limit: null,
              filters: [],
              facets: [],
              sort: [{ field: "created_at", direction: "DESC" }],
              offset: 0,
              size: 50,
            },
            query: null,
            size: 50,
            parent: null,
          }),
        });
        return facetDistribution(selectedField, response);
      } catch (error) {
        // Facet counts are also what the Filter panel displays. Only use the
        // database distribution path when the selected field cannot be faceted.
        if (
          !(error instanceof ApiError)
          || error.status !== 422
          || !error.message.includes("Unknown or non-facetable field")
        ) throw error;
      }
      return coreApi<ClassificationTargetDistributionRead>(
        `/v1/matters/${matterId}/classification-models/targets/${selectedField.id}/distribution`,
        {
          method: "POST",
          body: JSON.stringify({
            training_scope: {
              scope_type: scopeType,
              scope_id: distributionScopeId,
            },
            hierarchy_level: null,
          }),
        },
      );
    },
    enabled: Boolean(selectedFieldId && distributionScopeId),
  });

  const prepareEmbeddings = useMutation({
    mutationFn: () => coreApi<MatterEmbeddingJobRead>(`/v1/matters/${matterId}/embedding-jobs`, { method: "POST" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["matter-embedding-jobs", matterId] });
      toast.success("Document embedding preparation was started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Embeddings could not be started."),
  });

  const trainModel = useMutation({
    mutationFn: async () => {
      if (!selectedField) throw new Error("Choose a coding field.");
      if (!name.trim()) throw new Error("Enter a model name.");
      if (!activeEmbeddingJob) throw new Error("Complete document embeddings before training.");
      if (scopeType === "REVIEW_BATCH" && !scopeBatchId) throw new Error("Choose a ready review batch.");
      const seed = Number(randomSeed);
      const minimumLabeledCount = Number(minimumLabels);
      const minimumLabeledCountPerClass = Number(minimumPerClass);
      const selectedHierarchyLevel = Number(hierarchyLevel);
      const iterations = Number(maximumIterations);
      if (!Number.isInteger(seed) || seed < 0 || seed > 2 ** 32 - 1) throw new Error("Seed must be an integer from 0 to 4,294,967,295.");
      if (!Number.isInteger(minimumLabeledCount) || minimumLabeledCount < 2) throw new Error("Minimum labeled documents must be at least 2.");
      if (!Number.isInteger(minimumLabeledCountPerClass) || minimumLabeledCountPerClass < 1) throw new Error("Minimum documents per class must be at least 1.");
      if (selectedField.hierarchy_separator && hierarchyLevel.trim() && (!Number.isInteger(selectedHierarchyLevel) || selectedHierarchyLevel < 1 || selectedHierarchyLevel > 32)) throw new Error("Hierarchy level must be an integer from 1 to 32, or blank to use the full path.");
      if (!Number.isInteger(iterations) || iterations < 1 || iterations > 100_000) throw new Error("Maximum iterations must be between 1 and 100,000.");
      const candidates = parsePositiveNumbers(cValues);
      const folds = validationOptions.find((option) => option.value === validationPercent)?.folds ?? 5;

      setTrainingStage("REGISTERING");
      const model = await coreApi<ClassificationModelRead>(`/v1/matters/${matterId}/classification-models`, {
        method: "POST",
        body: JSON.stringify({
          target_metadata_definition_id: selectedField.id,
          name: name.trim(),
        }),
      });
      const effectiveStrategy = selectedField.type === "BOOLEAN" ? "BINARY" : strategy;
      const configurationPayload: ClassificationModelConfigurationCreate = {
        classification_strategy: effectiveStrategy,
        hierarchy_level: selectedField.hierarchy_separator && hierarchyLevel.trim() ? selectedHierarchyLevel : null,
        ordered_class_keys: [],
        binary_mapping: null,
        label_policy: {
          allowed_sources: [{ kind: "MATTER_ASSERTED", authority: "SILVER", source_weight: 1 }],
          missing_label_action: "EXCLUDE",
          source_conflict_action: "REQUIRE_ADJUDICATION",
          target_value_conflict_action: "EXCLUDE",
        },
        feature_contract: {
          dimensions: activeEmbeddingJob.embedding_dimensions,
          embedding_model: activeEmbeddingJob.embedding_model,
          embedding_model_revision: activeEmbeddingJob.embedding_model_revision,
          text_source_policy: "active_text",
        },
        prediction_resolution_policy: effectiveStrategy === "BINARY"
          ? { binary_positive_threshold: 0.5 }
          : effectiveStrategy === "MULTICLASS"
            ? { multiclass_minimum_confidence: 0.5 }
            : {
                one_vs_rest_thresholds: {},
                ...(selectedField.cardinality === "SINGLE"
                  ? { zero_match_action: "ABSTAIN" as const, multiple_match_action: "HIGHEST_PROBABILITY" as const }
                  : {}),
              },
        trainer_specification: {
          kind: "sklearn_logistic_regression",
          parameters: {
            regularization: "l2",
            c_values: candidates,
            maximum_iterations: iterations,
          },
        },
      };
      const configuration = await coreApi<ClassificationModelConfigurationRead>(
        `/v1/matters/${matterId}/classification-models/${model.id}/configurations`,
        { method: "POST", body: JSON.stringify(configurationPayload) },
      );

      setTrainingStage("LAUNCHING");
      const trainingJob = await coreApi<ClassificationTrainingJobCreateResponse>(
        `/v1/matters/${matterId}/classification-models/${model.id}/training-jobs`,
        {
          method: "POST",
          body: JSON.stringify({
            configuration_id: configuration.id,
            training_scope: {
              scope_type: scopeType,
              scope_id: scopeType === "MATTER" ? matterId : scopeBatchId,
            },
            embedding_job_id: activeEmbeddingJob.id,
            random_seed: seed,
            validation_policy: {
              fold_count: folds,
              minimum_labeled_count: minimumLabeledCount,
              minimum_labeled_count_per_class: minimumLabeledCountPerClass,
            },
            idempotency_key: crypto.randomUUID(),
          }),
        },
      );
      return { model, trainingJob };
    },
    onSuccess: async ({ model, trainingJob }) => {
      setTrainingStage(null);
      await queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] });
      toast.success(trainingJob.created ? "Durable model training was queued." : "The matching training job already exists.");
      router.push(`/app/clients/${clientId}/matters/${matterId}/classification-models/${model.id}`);
    },
    onError: async (error) => {
      setTrainingStage(null);
      await queryClient.invalidateQueries({ queryKey: ["classification-models", matterId] });
      toast.error(error instanceof Error ? error.message : "Model training could not be started.");
    },
  });

  if (models.isPending) return <TableLoading />;
  if (models.error) return <QueryError message={models.error.message} />;

  if (!selectedField) {
    return (
      <div className="space-y-6">
        <div>
          <h2 className="text-lg font-semibold">Classification</h2>
          <p className="text-sm text-muted-foreground">Choose the coding field that a model should predict.</p>
        </div>
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {codingFields.map((field) => {
            const modelCount = models.data.filter((model) => model.target_metadata_definition_id === field.id).length;
            return (
              <button
                key={field.id}
                type="button"
                className="rounded-xl border bg-card p-4 text-left transition-colors hover:border-primary/50 hover:bg-muted/30"
                onClick={() => {
                  setSelectedFieldId(field.id);
                  setStrategy(defaultStrategy(field));
                  setHierarchyLevel("");
                  setName(`${field.display_name} model`);
                }}
              >
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="font-semibold">{field.display_name}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{field.type} · {field.cardinality.toLowerCase()}</p>
                  </div>
                  <ChevronRight className="size-4 text-muted-foreground" />
                </div>
                <p className="mt-4 text-sm text-muted-foreground">{modelCount} model{modelCount === 1 ? "" : "s"}</p>
              </button>
            );
          })}
        </div>
        {!codingFields.length ? <p className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">No active coding fields are available. Create an asserted Boolean, enum, text, or long-text field first.</p> : null}
      </div>
    );
  }

  const selectedStrategy = selectedField.type === "BOOLEAN" ? "BINARY" : strategy;
  const selectedValidation = validationOptions.find((option) => option.value === validationPercent) ?? validationOptions[1];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <Button variant="ghost" size="sm" className="-ml-3 mb-2" onClick={() => setSelectedFieldId(null)}><ArrowLeft />All coding fields</Button>
          <h2 className="text-lg font-semibold">{selectedField.display_name}</h2>
          <p className="text-sm text-muted-foreground">Models trained to predict this coding field.</p>
        </div>
        <Badge variant="outline">{selectedField.type} · {selectedField.cardinality.toLowerCase()}</Badge>
      </div>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-base"><BrainCircuit className="size-4" />Models</CardTitle></CardHeader>
        <CardContent>
          {fieldModels.length ? <div className="divide-y rounded-lg border">{fieldModels.map((model) => (
            <Link key={model.id} href={`/app/clients/${clientId}/matters/${matterId}/classification-models/${model.id}`} className="flex items-center justify-between gap-3 p-3 hover:bg-muted/40">
              <div>
                <p className="font-medium">{model.name}</p>
                <p className="text-xs text-muted-foreground">{model.current_configuration?.classification_strategy?.toLowerCase().replaceAll("_", " ") ?? "not configured"}{selectedField.hierarchy_separator && model.current_configuration ? ` · ${model.current_configuration.hierarchy_level ? `level ${model.current_configuration.hierarchy_level}` : "full path"}` : ""} · created {formatDate(model.created_at)}</p>
              </div>
              <div className="flex items-center gap-2">
                <Badge variant={model.status === "ACTIVE" && model.active_model_version_id ? "accent" : "outline"}>
                  {model.status === "SUSPENDED" ? "Suspended" : model.status === "ARCHIVED" ? "Archived" : model.active_model_version_id ? "Activated" : "Not activated"}
                </Badge>
                <ChevronRight className="size-4 text-muted-foreground" />
              </div>
            </Link>
          ))}</div> : <p className="text-sm text-muted-foreground">No models have been created for this field.</p>}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Plus className="size-4" />Train a new model</CardTitle></CardHeader>
        <CardContent className="space-y-5">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="classification-name">Model name</Label><Input id="classification-name" value={name} onChange={(event) => setName(event.target.value)} /></div>
            <div className="space-y-2"><Label>Classification strategy</Label><Select value={selectedStrategy} onValueChange={(value) => setStrategy(value as Strategy)} disabled={selectedField.type === "BOOLEAN"}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="BINARY">Binary — exactly two observed classes</SelectItem><SelectItem value="MULTICLASS" disabled={selectedField.cardinality === "MULTIPLE"}>Multiclass — one class per document</SelectItem><SelectItem value="ONE_VS_REST">One vs rest — independent class decisions</SelectItem></SelectContent></Select></div>
            {selectedField.hierarchy_separator ? <div className="space-y-2"><Label htmlFor="classification-hierarchy-level">Training hierarchy level</Label><Input id="classification-hierarchy-level" type="number" min={1} max={32} step={1} placeholder="Full path" value={hierarchyLevel} onChange={(event) => setHierarchyLevel(event.target.value)} /><p className="text-xs text-muted-foreground">Level 1 is the root. Leave blank to train on each complete <code>{selectedField.hierarchy_separator}</code>-delimited path.</p></div> : null}
            <div className="space-y-2"><Label>Training scope</Label><Select value={scopeType} onValueChange={(value) => setScopeType(value as ScopeType)}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="MATTER">Entire matter</SelectItem><SelectItem value="REVIEW_BATCH">Review batch</SelectItem></SelectContent></Select></div>
            {scopeType === "REVIEW_BATCH" ? <div className="space-y-2"><Label>Review batch</Label><Select value={scopeBatchId} onValueChange={setScopeBatchId}><SelectTrigger><SelectValue placeholder="Choose a ready batch" /></SelectTrigger><SelectContent>{readyBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} documents</SelectItem>)}</SelectContent></Select></div> : null}
            <div className="space-y-2"><Label>Train / validation split</Label><Select value={validationPercent} onValueChange={setValidationPercent}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{validationOptions.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select><p className="text-xs text-muted-foreground">Implemented as {selectedValidation.folds}-fold group-aware cross-validation; every document is held out once.</p></div>
            <div className="space-y-2"><Label htmlFor="classification-seed">Random seed</Label><Input id="classification-seed" inputMode="numeric" value={randomSeed} onChange={(event) => setRandomSeed(event.target.value)} /></div>
          </div>

          <div className="rounded-lg border bg-muted/20 p-4">
            <p className="font-medium">Training parameters</p>
            <div className="mt-3 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <div className="space-y-2"><Label htmlFor="classification-c-values">Regularization C candidates</Label><Input id="classification-c-values" value={cValues} onChange={(event) => setCValues(event.target.value)} /></div>
              <div className="space-y-2"><Label htmlFor="classification-iterations">Maximum iterations</Label><Input id="classification-iterations" inputMode="numeric" value={maximumIterations} onChange={(event) => setMaximumIterations(event.target.value)} /></div>
              <div className="space-y-2"><Label htmlFor="classification-minimum-labels">Minimum labeled documents</Label><Input id="classification-minimum-labels" inputMode="numeric" value={minimumLabels} onChange={(event) => setMinimumLabels(event.target.value)} /></div>
              <div className="space-y-2"><Label htmlFor="classification-minimum-class">Minimum per class, per fold</Label><Input id="classification-minimum-class" inputMode="numeric" value={minimumPerClass} onChange={(event) => setMinimumPerClass(event.target.value)} /></div>
            </div>
          </div>

          <div className="rounded-lg border p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><p className="flex items-center gap-2 font-medium"><Database className="size-4" />Document features</p><p className="mt-1 text-xs text-muted-foreground">Training uses the matter’s completed document embeddings. Current matter coding supplies the labels.</p></div>
              {!usableEmbeddingJobs.length && !runningEmbeddingJob ? <Button variant="outline" onClick={() => prepareEmbeddings.mutate()} disabled={prepareEmbeddings.isPending}>{prepareEmbeddings.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Prepare embeddings</Button> : null}
            </div>
            {usableEmbeddingJobs.length ? <div className="mt-3 space-y-2"><Label>Embedding set</Label><div className="flex flex-wrap items-center gap-2"><div className="min-w-72 flex-1"><Select value={activeEmbeddingJobId} onValueChange={setEmbeddingJobId}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{usableEmbeddingJobs.map((job) => <SelectItem key={job.id} value={job.id}>{job.status === "CANCELED" ? "Recovered completed output · " : ""}{job.embedding_model} · {job.embedding_dimensions} dimensions · {job.chunk_count.toLocaleString()} chunks</SelectItem>)}</SelectContent></Select></div>{activeEmbeddingJob ? <JobOutcomesDialog matterId={matterId} jobType="MATTER_EMBEDDING_JOB" jobId={activeEmbeddingJob.id} jobName={`${activeEmbeddingJob.embedding_model} embedding job`} /> : null}</div>{activeEmbeddingJob?.status === "CANCELED" ? <p className="text-xs text-muted-foreground">This run was canceled after all {activeEmbeddingJob.processed_count.toLocaleString()} documents were processed. Its completed chunk output can still be used for training.</p> : null}</div> : runningEmbeddingJob ? <p className="mt-3 text-sm text-muted-foreground">Embeddings are {runningEmbeddingJob.status.toLowerCase()}. This page will refresh their status automatically.</p> : <p className="mt-3 text-sm text-muted-foreground">Prepare document embeddings before training can begin.</p>}
          </div>

          <div className="rounded-lg border p-4">
            <div className="mb-3">
              <p className="flex items-center gap-2 font-medium"><ChartColumn className="size-4" />Class distribution</p>
              <p className="mt-1 text-xs text-muted-foreground">Current matter coding for the selected training scope. Each row shows the document count for one class.</p>
            </div>
            <ClassificationDistributionChart
              distribution={distribution.data}
              loading={distribution.isPending && !waitingForDistributionScope}
              error={Boolean(distribution.error)}
              waitingForScope={waitingForDistributionScope}
            />
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">Classes will be discovered from current coding and frozen with this training run.</p>
            <Button onClick={() => trainModel.mutate()} disabled={!name.trim() || !activeEmbeddingJob || (scopeType === "REVIEW_BATCH" && !scopeBatchId) || trainModel.isPending}>{trainModel.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}{stageLabel(trainingStage)}</Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
