"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, Layers3, LoaderCircle, Plus, RefreshCw } from "lucide-react";
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
  MatterAnalysisTaskRead,
  MatterSavedSearchRead,
  ReviewBatchRead,
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
                {ACTIVE_STATUSES.has(set.status) ? <Button variant="outline" size="sm" onClick={() => cancel.mutate(set.id)} disabled={cancel.isPending}><Ban />Cancel</Button> : null}
                {["FAILED", "CANCELED"].includes(set.status) ? <Button variant="outline" size="sm" onClick={() => retry.mutate(set.id)} disabled={retry.isPending}><RefreshCw />Retry</Button> : null}
              </div>
            </div>
            {set.error_message ? <p role="alert" className="mt-3 rounded-md bg-destructive/10 p-3 text-sm text-destructive">{set.error_message}</p> : null}
            {set.warning_details.length ? <div className="mt-3 space-y-1">{set.warning_details.map((warning, index) => <p key={`${String(warning.code)}-${index}`} className="flex items-start gap-2 text-sm text-amber-700"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{warningText(warning)}</p>)}</div> : null}
            {set.children?.length ? (
              <div className="mt-4 overflow-hidden rounded-md border">
                {set.children.map((child) => <div key={child.id} className="flex flex-wrap items-center justify-between gap-3 border-b px-3 py-2 last:border-b-0"><div><p className="text-sm font-medium">{child.ordinal}. {child.name}</p><p className="text-xs text-muted-foreground">{child.document_count.toLocaleString()} documents</p></div><div className="flex flex-wrap items-center gap-2"><StatusBadge status={child.status} /><Badge variant="outline">Search: {friendly(child.search_status)}</Badge>{child.search_error_message ? <span className="max-w-xs text-xs text-destructive">{child.search_error_message}</span> : null}</div></div>)}
              </div>
            ) : null}
            <p className="mt-3 text-xs text-muted-foreground">Created {formatDate(set.created_at)}. Exclusivity applies within this set only; the same document may still belong to unrelated batches or another set.</p>
          </div>
        ))}
      </CardContent>
    </Card>
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

function friendly(value: string) {
  return value.toLocaleLowerCase().replaceAll("_", " ");
}

function warningText(warning: Record<string, unknown>) {
  const code = typeof warning.code === "string" ? friendly(warning.code) : "warning";
  const message = typeof warning.message === "string" ? warning.message : null;
  return message ? `${code}: ${message}` : code;
}
