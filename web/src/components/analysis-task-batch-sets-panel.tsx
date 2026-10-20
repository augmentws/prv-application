"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, Layers3, LoaderCircle, Play, Plus, RefreshCw } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  AnalysisTaskBatchSetCreate,
  AnalysisTaskBatchSetPreviewCreate,
  AnalysisTaskBatchSetPreviewRead,
  AnalysisTaskBatchSetRead,
  AnalysisTaskBatchSetRunCreate,
  AnalysisTaskBatchSetRunRead,
  FactDevelopmentRunEstimateRead,
  FactDevelopmentRunRead,
  MatterAnalysisTaskRead,
  MatterAnalysisTaskRunRead,
  MatterAnalysisTaskVersionRead,
  MatterEmbeddingJobRead,
  MatterSavedSearchRead,
  ReviewBatchRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

type PopulationSource = AnalysisTaskBatchSetCreate["population_source_type"];

const SOURCE_LABELS: Record<PopulationSource, string> = {
  ALL_MATTER: "Whole matter",
  SOURCE_BATCH: "Existing review batch",
  SEARCH_QUERY: "Search query",
  SAVED_SEARCH: "Saved search",
};

const ACTIVE_STATUSES = new Set(["QUEUED", "BUILDING"]);
const ACTIVE_RUN_STATUSES = new Set(["QUEUED", "RUNNING"]);

export function AnalysisTaskBatchSetsPanel({ matterId, task }: { matterId: string; task: MatterAnalysisTaskRead }) {
  const queryClient = useQueryClient();
  const queryKey = ["analysis-task-batch-sets", matterId, task.id] as const;
  const sets = useQuery({
    queryKey,
    queryFn: () => coreApi<AnalysisTaskBatchSetRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets`),
    refetchInterval: (query) => query.state.data?.some((set) => ACTIVE_STATUSES.has(set.status)) ? 1500 : false,
  });
  const cancel = useMutation({
    mutationFn: (setId: string) => coreApi<AnalysisTaskBatchSetRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets/${setId}/cancel`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<AnalysisTaskBatchSetRead[]>(queryKey, (current) => current?.map((set) => set.id === updated.id ? updated : set) ?? [updated]);
      toast.success("Partitioned batch set canceled.");
    },
    onError: (error) => toast.error(error.message),
  });
  const retry = useMutation({
    mutationFn: (setId: string) => coreApi<AnalysisTaskBatchSetRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets/${setId}/retry`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<AnalysisTaskBatchSetRead[]>(queryKey, (current) => current?.map((set) => set.id === updated.id ? updated : set) ?? [updated]);
      toast.success("Partitioned batch set retry queued.");
    },
    onError: (error) => toast.error(error.message),
  });

  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2"><Layers3 className="size-4 text-primary" />Partitioned batch sets</CardTitle>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">Freeze a population against the published task version, then divide it into deterministic, mutually exclusive review batches.</p>
          </div>
          <CreateBatchSetDialog matterId={matterId} task={task} onCreated={(created) => {
            queryClient.setQueryData<AnalysisTaskBatchSetRead[]>(queryKey, (current) => [created, ...(current ?? [])]);
          }} />
        </div>
      </CardHeader>
      <CardContent className="space-y-3 p-4">
        {sets.isPending ? <TableLoading /> : sets.error ? <QueryError message={sets.error.message} /> : !sets.data.length ? (
          <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">No partitioned batch sets have been created for this task.</p>
        ) : sets.data.map((set) => (
          <div key={set.id} className="rounded-lg border p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2"><h4 className="font-semibold">{set.name}</h4><StatusBadge status={set.status} /><Badge variant="outline">{SOURCE_LABELS[set.population_source_type]}</Badge></div>
                <p className="mt-1 text-sm text-muted-foreground">{set.description || `${set.population_count.toLocaleString()} documents across ${set.batch_count.toLocaleString()} batches.`}</p>
                <p className="mt-2 font-mono text-xs text-muted-foreground">Task version {set.matter_analysis_task_version_id} · population {shortHash(set.population_hash)}</p>
                {set.search_index_generation_id ? <p className="mt-1 font-mono text-xs text-muted-foreground">Search generation {set.search_index_generation_id}</p> : null}
              </div>
              <div className="flex gap-2">
                {set.status === "READY" ? <BatchSetRunAllDialog matterId={matterId} task={task} batchSet={set} /> : null}
                {ACTIVE_STATUSES.has(set.status) ? <Button variant="outline" size="sm" onClick={() => cancel.mutate(set.id)} disabled={cancel.isPending}><Ban />Cancel</Button> : null}
                {["FAILED", "CANCELED"].includes(set.status) ? <Button variant="outline" size="sm" onClick={() => retry.mutate(set.id)} disabled={retry.isPending}><RefreshCw />Retry</Button> : null}
              </div>
            </div>
            {set.error_message ? <p role="alert" className="mt-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{set.error_message}</p> : null}
            {set.warning_details.length ? <div className="mt-3 space-y-1">{set.warning_details.map((warning, index) => <p key={`${String(warning.code)}-${index}`} className="flex items-start gap-2 text-sm text-amber-700"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{warningText(warning)}</p>)}</div> : null}
            {set.children?.length ? (
              <div className="mt-4 overflow-hidden rounded-md border">
                {set.children.map((child) => <div key={child.id} className="flex flex-wrap items-center justify-between gap-3 border-b px-3 py-2 last:border-b-0"><div><p className="text-sm font-medium">{child.ordinal}. {child.name}</p><p className="text-xs text-muted-foreground">{child.document_count.toLocaleString()} documents</p></div><div className="flex flex-wrap items-center gap-2"><StatusBadge status={child.status} /><Badge variant="outline">Search: {friendly(child.search_status)}</Badge>{child.search_error_message ? <span className="max-w-xs text-xs text-destructive">{child.search_error_message}</span> : null}{set.status === "READY" && child.status === "READY" ? <BatchSetChildRunDialog matterId={matterId} task={task} batchSet={set} child={child} /> : null}</div></div>)}
              </div>
            ) : null}
            <BatchSetRuns matterId={matterId} task={task} batchSet={set} />
            <p className="mt-3 text-xs text-muted-foreground">Created {formatDate(set.created_at)}. Exclusivity applies within this set only; the same document may still belong to unrelated batches or another set.</p>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function BatchSetRunAllDialog({ matterId, task, batchSet }: {
  matterId: string;
  task: MatterAnalysisTaskRead;
  batchSet: AnalysisTaskBatchSetRead;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [maxConcurrency, setMaxConcurrency] = useState("2");
  const [embeddingJobId, setEmbeddingJobId] = useState("");
  const [useBatchApi, setUseBatchApi] = useState(false);
  const versions = useQuery({
    queryKey: ["analysis-task-versions", matterId, task.id],
    queryFn: () => coreApi<MatterAnalysisTaskVersionRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`),
    enabled: open && task.task_type === "CLASSIFICATION",
  });
  const embeddings = useQuery({
    queryKey: ["matter-embedding-jobs", matterId],
    queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`),
    enabled: open && task.task_type === "CLASSIFICATION",
  });
  const pinnedVersion = versions.data?.find((version) => version.id === batchSet.matter_analysis_task_version_id);
  const usableEmbeddings = (embeddings.data ?? []).filter((job) => (
    ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status)
    && (!pinnedVersion?.classification_embedding_job_id || job.id === pinnedVersion.classification_embedding_job_id)
  ));
  const effectiveEmbeddingJobId = usableEmbeddings.some((job) => job.id === embeddingJobId)
    ? embeddingJobId
    : usableEmbeddings[0]?.id ?? "";
  const concurrency = Number(maxConcurrency);
  const valid = Number.isInteger(concurrency) && concurrency >= 1 && concurrency <= 32
    && (task.task_type !== "CLASSIFICATION" || Boolean(effectiveEmbeddingJobId));
  const create = useMutation({
    mutationFn: () => coreApi<AnalysisTaskBatchSetRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets/${batchSet.id}/runs`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: crypto.randomUUID(),
        max_concurrency: concurrency,
        embedding_job_id: task.task_type === "CLASSIFICATION" ? effectiveEmbeddingJobId : null,
        coding_source: "MATTER_METADATA",
        use_batch_api: task.task_type === "FACT_DEVELOPMENT" && useBatchApi,
      } satisfies AnalysisTaskBatchSetRunCreate),
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-batch-set-runs", matterId, task.id, batchSet.id] });
      toast.success("Run-all execution queued.");
      setOpen(false);
    },
    onError: (error) => toast.error(error.message),
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button size="sm"><Play />Run all</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Run task across this set</DialogTitle><DialogDescription>Queue one tracked parent execution for all {batchSet.batch_count.toLocaleString()} mutually exclusive child batches. Results remain on their native child runs.</DialogDescription></DialogHeader>
        <Field label="Simultaneous child batches" htmlFor={`batch-set-run-window-${batchSet.id}`}><Input id={`batch-set-run-window-${batchSet.id}`} type="number" min={1} max={32} value={maxConcurrency} onChange={(event) => setMaxConcurrency(event.target.value)} /></Field>
        {task.task_type === "CLASSIFICATION" ? <Field label="Compatible document embeddings" htmlFor={`batch-set-run-embedding-${batchSet.id}`}><Select value={effectiveEmbeddingJobId} onValueChange={setEmbeddingJobId}><SelectTrigger id={`batch-set-run-embedding-${batchSet.id}`}><SelectValue placeholder={embeddings.isPending ? "Loading embeddings…" : "Select embeddings"} /></SelectTrigger><SelectContent>{usableEmbeddings.map((job) => <SelectItem key={job.id} value={job.id}>{job.embedding_model} · {job.processed_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field> : null}
        {task.task_type === "FACT_DEVELOPMENT" ? <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={useBatchApi} onChange={(event) => setUseBatchApi(event.target.checked)} />Use provider Batch API for each child</label> : null}
        {create.error ? <p role="alert" className="text-sm text-destructive">{create.error.message}</p> : null}
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)} disabled={create.isPending}>Cancel</Button><Button onClick={() => create.mutate()} disabled={!valid || create.isPending}>{create.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Queue run all</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BatchSetRuns({ matterId, task, batchSet }: {
  matterId: string;
  task: MatterAnalysisTaskRead;
  batchSet: AnalysisTaskBatchSetRead;
}) {
  const queryClient = useQueryClient();
  const queryKey = ["analysis-task-batch-set-runs", matterId, task.id, batchSet.id] as const;
  const path = `/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets/${batchSet.id}/runs`;
  const runs = useQuery({
    queryKey,
    queryFn: () => coreApi<AnalysisTaskBatchSetRunRead[]>(path),
    refetchInterval: (query) => query.state.data?.some((run) => ACTIVE_RUN_STATUSES.has(run.status)) ? 1500 : false,
  });
  const action = useMutation({
    mutationFn: ({ runId, verb }: { runId: string; verb: "cancel" | "retry" }) => coreApi<AnalysisTaskBatchSetRunRead>(`${path}/${runId}/${verb}`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<AnalysisTaskBatchSetRunRead[]>(queryKey, (current) => current?.map((run) => run.id === updated.id ? updated : run) ?? [updated]);
      toast.success(updated.status === "CANCELED" ? "Run-all execution canceled." : "Failed child batches requeued.");
    },
    onError: (error) => toast.error(error.message),
  });
  if (runs.isPending || runs.error || !runs.data.length) return runs.error ? <QueryError message={runs.error.message} /> : null;
  return <div className="mt-4 space-y-2"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Run-all history</p>{runs.data.map((run) => <div key={run.id} className="rounded-md border bg-muted/10 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-2"><StatusBadge status={run.status} /><span className="text-sm">{run.completed_child_count.toLocaleString()} completed · {run.failed_child_count.toLocaleString()} failed · {run.child_count.toLocaleString()} total</span><Badge variant="outline">Window {String(run.configuration_snapshot.max_concurrency ?? "—")}</Badge></div><div className="flex gap-2">{ACTIVE_RUN_STATUSES.has(run.status) ? <Button size="sm" variant="outline" onClick={() => action.mutate({ runId: run.id, verb: "cancel" })}><Ban />Cancel</Button> : null}{["FAILED", "CANCELED", "COMPLETED_WITH_ERRORS"].includes(run.status) ? <Button size="sm" variant="outline" onClick={() => action.mutate({ runId: run.id, verb: "retry" })}><RefreshCw />Retry failed</Button> : null}</div></div><div className="mt-2 flex flex-wrap gap-1">{run.children?.map((child) => <Badge key={child.id} variant="outline">{child.ordinal}: {friendly(child.status)}</Badge>)}</div><p className="mt-2 text-xs text-muted-foreground">Started {formatDate(run.created_at)} · execution {shortId(run.id)}</p></div>)}</div>;
}

function BatchSetChildRunDialog({ matterId, task, batchSet, child }: {
  matterId: string;
  task: MatterAnalysisTaskRead;
  batchSet: AnalysisTaskBatchSetRead;
  child: NonNullable<AnalysisTaskBatchSetRead["children"]>[number];
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [embeddingJobId, setEmbeddingJobId] = useState("");
  const versions = useQuery({
    queryKey: ["analysis-task-versions", matterId, task.id],
    queryFn: () => coreApi<MatterAnalysisTaskVersionRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`),
    enabled: open && task.task_type === "CLASSIFICATION",
  });
  const embeddings = useQuery({
    queryKey: ["matter-embedding-jobs", matterId],
    queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`),
    enabled: open && task.task_type === "CLASSIFICATION",
  });
  const pinnedVersion = versions.data?.find((version) => version.id === batchSet.matter_analysis_task_version_id);
  const usableEmbeddings = (embeddings.data ?? []).filter((job) => (
    ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status)
    && (!pinnedVersion?.classification_embedding_job_id || job.id === pinnedVersion.classification_embedding_job_id)
  ));
  const effectiveEmbeddingJobId = usableEmbeddings.some((job) => job.id === embeddingJobId)
    ? embeddingJobId
    : usableEmbeddings[0]?.id ?? "";
  const questionRun = useMutation({
    mutationFn: () => coreApi<ReviewBatchRunRead>(`/v1/matters/${matterId}/review-batches/${child.id}/analysis-runs`, {
      method: "POST",
      body: JSON.stringify({ matter_analysis_task_id: task.id, matter_analysis_task_version_id: batchSet.matter_analysis_task_version_id }),
    }),
    onSuccess: () => finish("Question-answering run queued."),
    onError: (error) => toast.error(error.message),
  });
  const classificationRun = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/runs`, {
      method: "POST",
      body: JSON.stringify({
        scope_type: "REVIEW_BATCH",
        scope_id: child.id,
        embedding_job_id: effectiveEmbeddingJobId,
        matter_analysis_task_version_id: batchSet.matter_analysis_task_version_id,
        idempotency_key: crypto.randomUUID(),
      }),
    }),
    onSuccess: () => finish("Classification run queued."),
    onError: (error) => toast.error(error.message),
  });
  const factEstimate = useMutation({
    mutationFn: () => coreApi<FactDevelopmentRunEstimateRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs/estimate`, {
      method: "POST",
      body: JSON.stringify({
        review_batch_id: child.id,
        coding_source: "MATTER_METADATA",
        reviewer_coding_id: null,
        matter_analysis_task_version_id: batchSet.matter_analysis_task_version_id,
      }),
    }),
    onError: (error) => toast.error(error.message),
  });
  const factRun = useMutation({
    mutationFn: () => coreApi<FactDevelopmentRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs`, {
      method: "POST",
      body: JSON.stringify({
        review_batch_id: child.id,
        coding_source: "MATTER_METADATA",
        reviewer_coding_id: null,
        matter_analysis_task_version_id: batchSet.matter_analysis_task_version_id,
        idempotency_key: crypto.randomUUID(),
        use_batch_api: false,
      }),
    }),
    onSuccess: () => finish("Fact-development run queued."),
    onError: (error) => toast.error(error.message),
  });

  function finish(message: string) {
    void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId, task.id] });
    void queryClient.invalidateQueries({ queryKey: ["fact-development-runs", matterId, task.id] });
    toast.success(message);
    setOpen(false);
  }

  const busy = questionRun.isPending || classificationRun.isPending || factEstimate.isPending || factRun.isPending;
  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) factEstimate.reset(); }}>
      <DialogTrigger asChild><Button size="sm" variant="outline"><Play />Run task</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Run {task.name}</DialogTitle><DialogDescription>{child.name} · {child.document_count.toLocaleString()} documents · pinned task version {shortId(batchSet.matter_analysis_task_version_id)}</DialogDescription></DialogHeader>
        {task.task_type === "QUESTION_ANSWERING" ? <p className="text-sm text-muted-foreground">Queue the published decision workflow for every document in this managed child. Results remain isolated for review.</p> : null}
        {task.task_type === "CLASSIFICATION" ? <div className="space-y-3"><Field label="Compatible document embeddings" htmlFor={`batch-set-embedding-${child.id}`}><Select value={effectiveEmbeddingJobId} onValueChange={setEmbeddingJobId}><SelectTrigger id={`batch-set-embedding-${child.id}`}><SelectValue placeholder={embeddings.isPending ? "Loading embeddings…" : "Select embeddings"} /></SelectTrigger><SelectContent>{usableEmbeddings.map((job) => <SelectItem key={job.id} value={job.id}>{job.embedding_model} · {job.processed_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field>{!embeddings.isPending && !usableEmbeddings.length ? <p className="text-sm text-amber-700">No completed embeddings compatible with this pinned task version are available.</p> : null}</div> : null}
        {task.task_type === "FACT_DEVELOPMENT" ? <div className="space-y-3"><p className="text-sm text-muted-foreground">This quick launch uses current matter coding. Use the full run panel when a specific reviewer&apos;s coding or the provider Batch API is required.</p>{factEstimate.data ? <div className="rounded-lg border bg-muted/20 p-3 text-sm"><p>{factEstimate.data.documents_to_extract.toLocaleString()} documents to read from {factEstimate.data.matched_document_count.toLocaleString()} issue-matched documents.</p><p className="mt-1 text-xs text-muted-foreground">Approximately {factEstimate.data.estimated_input_tokens.toLocaleString()} input and {factEstimate.data.estimated_output_tokens.toLocaleString()} output tokens.</p></div> : null}</div> : null}
        {(versions.error || embeddings.error) ? <QueryError message={(versions.error ?? embeddings.error)!.message} /> : null}
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
          {task.task_type === "QUESTION_ANSWERING" ? <Button onClick={() => questionRun.mutate()} disabled={busy}><Play />Queue run</Button> : null}
          {task.task_type === "CLASSIFICATION" ? <Button onClick={() => classificationRun.mutate()} disabled={busy || !effectiveEmbeddingJobId}>{classificationRun.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Queue classification</Button> : null}
          {task.task_type === "FACT_DEVELOPMENT" && !factEstimate.data ? <Button variant="outline" onClick={() => factEstimate.mutate()} disabled={busy}>{factEstimate.isPending ? <LoaderCircle className="animate-spin" /> : null}Estimate</Button> : null}
          {task.task_type === "FACT_DEVELOPMENT" && factEstimate.data ? <Button onClick={() => factRun.mutate()} disabled={busy || factEstimate.data.documents_to_extract === 0}>{factRun.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Queue fact development</Button> : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateBatchSetDialog({ matterId, task, onCreated }: { matterId: string; task: MatterAnalysisTaskRead; onCreated: (set: AnalysisTaskBatchSetRead) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [source, setSource] = useState<PopulationSource>("ALL_MATTER");
  const [sourceBatchId, setSourceBatchId] = useState("");
  const [savedSearchId, setSavedSearchId] = useState("");
  const [query, setQuery] = useState("");
  const [targetSize, setTargetSize] = useState("500");
  const [keepFamilies, setKeepFamilies] = useState(true);
  const [preview, setPreview] = useState<AnalysisTaskBatchSetPreviewRead>();
  const batches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
    enabled: open && source === "SOURCE_BATCH",
  });
  const savedSearches = useQuery({
    queryKey: ["saved-searches", matterId],
    queryFn: () => coreApi<MatterSavedSearchRead[]>(`/v1/matters/${matterId}/saved-searches`),
    enabled: open && source === "SAVED_SEARCH",
  });
  const usableSavedSearches = (savedSearches.data ?? []).filter((saved) => (saved.search.search_mode ?? "KEYWORD") === "KEYWORD");
  const targetBatchSize = Number(targetSize);
  const sourceReady = source === "ALL_MATTER"
    || (source === "SOURCE_BATCH" && Boolean(sourceBatchId))
    || (source === "SEARCH_QUERY" && Boolean(query.trim()))
    || (source === "SAVED_SEARCH" && Boolean(savedSearchId));
  const valid = sourceReady && Number.isInteger(targetBatchSize) && targetBatchSize > 0;

  function previewPayload(): AnalysisTaskBatchSetPreviewCreate {
    return {
      population_source_type: source,
      source_review_batch_id: source === "SOURCE_BATCH" ? sourceBatchId : null,
      search: source === "SEARCH_QUERY" ? { query: query.trim(), search_mode: "KEYWORD" } : null,
      saved_search_id: source === "SAVED_SEARCH" ? savedSearchId : null,
      target_batch_size: targetBatchSize,
      keep_together: keepFamilies ? ["FAMILY"] : [],
    };
  }

  const previewMutation = useMutation({
    mutationFn: () => coreApi<AnalysisTaskBatchSetPreviewRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets/preview`, { method: "POST", body: JSON.stringify(previewPayload()) }),
    onSuccess: setPreview,
  });
  const createMutation = useMutation({
    mutationFn: () => coreApi<AnalysisTaskBatchSetRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/batch-sets`, {
      method: "POST",
      body: JSON.stringify({ ...previewPayload(), name: name.trim(), description: description.trim() || null } satisfies AnalysisTaskBatchSetCreate),
    }),
    onSuccess: (created) => {
      onCreated(created);
      toast.success("Partitioned batch set queued.");
      setOpen(false);
      reset();
    },
  });

  function invalidatePreview() {
    setPreview(undefined);
    previewMutation.reset();
  }

  function reset() {
    setName("");
    setDescription("");
    setSource("ALL_MATTER");
    setSourceBatchId("");
    setSavedSearchId("");
    setQuery("");
    setTargetSize("500");
    setKeepFamilies(true);
    setPreview(undefined);
    previewMutation.reset();
    createMutation.reset();
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) reset(); }}>
      <DialogTrigger asChild><Button><Plus />New partitioned set</Button></DialogTrigger>
      <DialogContent className="max-w-3xl">
        <DialogHeader><DialogTitle>Create a partitioned batch set</DialogTitle><DialogDescription>Preview the exact population and allocation before creating immutable child review batches.</DialogDescription></DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" htmlFor="batch-set-name"><Input id="batch-set-name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Privilege review wave 1" /></Field>
          <Field label="Population source" htmlFor="batch-set-source"><Select value={source} onValueChange={(value) => { setSource(value as PopulationSource); invalidatePreview(); }}><SelectTrigger id="batch-set-source"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(SOURCE_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></Field>
          <Field label="Target documents per batch" htmlFor="batch-set-size"><Input id="batch-set-size" type="number" min={1} value={targetSize} onChange={(event) => { setTargetSize(event.target.value); invalidatePreview(); }} /></Field>
          <div className="flex items-end pb-2"><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={keepFamilies} onChange={(event) => { setKeepFamilies(event.target.checked); invalidatePreview(); }} />Keep document families together</label></div>
        </div>
        {source === "SOURCE_BATCH" ? <Field label="Ready review batch" htmlFor="batch-set-source-batch"><Select value={sourceBatchId} onValueChange={(value) => { setSourceBatchId(value); invalidatePreview(); }}><SelectTrigger id="batch-set-source-batch"><SelectValue placeholder="Select a batch" /></SelectTrigger><SelectContent>{(batches.data ?? []).filter((batch) => batch.status === "READY").map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field> : null}
        {source === "SEARCH_QUERY" ? <Field label="Keyword search" htmlFor="batch-set-query"><Input id="batch-set-query" value={query} onChange={(event) => { setQuery(event.target.value); invalidatePreview(); }} placeholder="privilege AND counsel" /></Field> : null}
        {source === "SAVED_SEARCH" ? <Field label="Saved keyword search" htmlFor="batch-set-saved-search"><Select value={savedSearchId} onValueChange={(value) => { setSavedSearchId(value); invalidatePreview(); }}><SelectTrigger id="batch-set-saved-search"><SelectValue placeholder="Select a saved search" /></SelectTrigger><SelectContent>{usableSavedSearches.map((saved) => <SelectItem key={saved.id} value={saved.id}>{saved.name}</SelectItem>)}</SelectContent></Select>{!savedSearches.isPending && !usableSavedSearches.length ? <p className="text-xs text-muted-foreground">No accessible keyword saved searches are available.</p> : null}</Field> : null}
        <Field label="Description" htmlFor="batch-set-description"><Textarea id="batch-set-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Optional purpose or reviewer instructions" /></Field>
        {batches.error || savedSearches.error ? <QueryError message={(batches.error ?? savedSearches.error)!.message} /> : null}
        {previewMutation.error ? <p role="alert" className="text-sm text-destructive">{previewMutation.error.message}</p> : null}
        {preview ? <div className="space-y-2 rounded-lg border bg-muted/20 p-4"><p className="font-semibold">Preview</p><p className="text-sm">{preview.population_count.toLocaleString()} documents → {preview.estimated_batch_count.toLocaleString()} batches</p><p className="text-sm text-muted-foreground">Estimated sizes: {preview.estimated_batch_sizes.map((size) => size.toLocaleString()).join(", ") || "none"}</p>{(preview.warnings ?? []).map((warning, index) => <p key={index} className="flex gap-2 text-sm text-amber-700"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{warningText(warning)}</p>)}<p className="text-xs text-muted-foreground">Each document appears in exactly one child batch in this set. It may still exist in unrelated batches or another set.</p></div> : null}
        {createMutation.error ? <p role="alert" className="text-sm text-destructive">{createMutation.error.message}</p> : null}
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button variant="outline" onClick={() => previewMutation.mutate()} disabled={!valid || previewMutation.isPending}>{previewMutation.isPending ? <LoaderCircle className="animate-spin" /> : null}{previewMutation.isPending ? "Previewing…" : "Preview allocation"}</Button><Button onClick={() => createMutation.mutate()} disabled={!preview || !name.trim() || createMutation.isPending}>{createMutation.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}{createMutation.isPending ? "Creating…" : "Create set"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label htmlFor={htmlFor}>{label}</Label>{children}</div>;
}

function shortHash(value: string | null) {
  return value ? `${value.slice(0, 12)}…` : "pending";
}

function shortId(value: string) {
  return `${value.slice(0, 8)}…`;
}

function friendly(value: string) {
  return value.toLocaleLowerCase().replaceAll("_", " ");
}

function warningText(warning: Record<string, unknown>) {
  const code = typeof warning.code === "string" ? friendly(warning.code) : "warning";
  const message = typeof warning.message === "string" ? warning.message : null;
  return message ? `${code}: ${message}` : code;
}
