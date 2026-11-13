"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, FileSearch, Play, RotateCcw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { JobOutcomesDialog } from "@/components/job-outcomes-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { ProgressBar, TERMINAL_JOB_STATUSES, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { SoftDeleteDialog } from "@/components/soft-delete-dialog";
import { StatusBadge } from "@/components/status-badge";
import { ActionMenuButton } from "@/components/ui/action-menu-button";
import { Button } from "@/components/ui/button";
import type { JobExecutionRead, MatterEmbeddingJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export const ACTIVE_EMBEDDING_STATUSES = ["QUEUED", "PLANNING", "RUNNING"];

export function useEmbeddingJobs(matterId: string) {
  return useQuery({
    queryKey: ["matter-embedding-jobs", matterId],
    queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`),
    refetchInterval: (query) => query.state.data?.some((job) => ACTIVE_EMBEDDING_STATUSES.includes(job.status)) ? 2000 : false,
  });
}

export function EmbeddingJobs() {
  const { matterId, permissionKeys } = useMatter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const embeddingJobs = useEmbeddingJobs(matterId);
  const deleteJob = useDeleteJob(["matter-embedding-jobs", matterId]);
  const createEmbeddingJobMutation = useMutation({
    mutationFn: () => coreApi<MatterEmbeddingJobRead>(`/v1/matters/${matterId}/embedding-jobs`, { method: "POST" }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterEmbeddingJobRead[]>(["matter-embedding-jobs", matterId], (current) => current ? [created, ...current] : [created]);
      toast.success("The embedding job was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The embedding job could not be started."),
  });
  const cancelEmbeddingJobMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<JobExecutionRead>(`/v1/jobs/MATTER_EMBEDDING_JOB/${jobId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-embedding-jobs", matterId] });
      toast.success("The embedding job was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The embedding job could not be canceled."),
  });
  const retryEmbeddingIndexMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<MatterEmbeddingJobRead>(`/v1/matters/${matterId}/embedding-jobs/${jobId}/retry-index`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterEmbeddingJobRead[]>(["matter-embedding-jobs", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      toast.success("Search indexing was queued again; completed embeddings will be reused.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Search indexing could not be retried."),
  });
  const columns = useMemo<ColumnDef<MatterEmbeddingJobRead>[]>(() => [
    { accessorKey: "embedding_model", header: "Model", size: 200, minSize: 140, maxSize: 280, cell: ({ row }) => <div className="min-w-0"><p className="truncate font-semibold">{row.original.embedding_model}</p><p className="truncate text-xs text-muted-foreground">{row.original.embedding_dimensions.toLocaleString()} dimensions</p>{row.original.error_message ? <p className="mt-1 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
    { accessorKey: "status", header: "Status", size: 130, minSize: 105, maxSize: 180, cell: ({ row }) => <div className="space-y-1"><StatusBadge status={row.original.status} />{row.original.execution ? <p className="truncate text-xs text-muted-foreground" title={`${row.original.execution.stage_label} · attempt ${row.original.execution.attempt_number}`}>{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}</div> },
    { id: "progress", header: "Progress", size: 155, minSize: 125, maxSize: 190, cell: ({ row }) => <EmbeddingProgress job={row.original} /> },
    { id: "outcomes", header: "Outcomes", size: 205, minSize: 170, maxSize: 250, cell: ({ row }) => <EmbeddingOutcomes matterId={matterId} job={row.original} /> },
    { id: "usage", header: "Usage", size: 150, minSize: 115, maxSize: 190, cell: ({ row }) => <EmbeddingUsage job={row.original} /> },
    { accessorKey: "created_at", header: "Created", size: 130, minSize: 105, maxSize: 170, cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", size: 52, minSize: 48, maxSize: 64, enableResizing: false, cell: ({ row }) => <div className="flex justify-end"><EmbeddingJobActions
          matterId={matterId}
          job={row.original}
          canCancel={row.original.execution?.available_actions.includes("CANCEL") ?? ACTIVE_EMBEDDING_STATUSES.includes(row.original.status)}
          canceling={cancelEmbeddingJobMutation.isPending}
          canDelete={canDeleteJobs && TERMINAL_JOB_STATUSES.includes(row.original.status)}
          onCancel={() => cancelEmbeddingJobMutation.mutate(row.original.id)}
          onDelete={deleteJob}
          onRetryIndex={() => retryEmbeddingIndexMutation.mutate(row.original.id)}
          retryingIndex={retryEmbeddingIndexMutation.isPending}
        /></div> },
  ], [canDeleteJobs, cancelEmbeddingJobMutation, deleteJob, matterId, retryEmbeddingIndexMutation]);

  if (embeddingJobs.isPending) return <TableLoading />;
  if (embeddingJobs.error) return <QueryError message={embeddingJobs.error.message} />;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Document embeddings</h2><p className="text-sm text-muted-foreground">Create sentence-aware chunks and vectors for every document in this matter.</p></div><Button onClick={() => createEmbeddingJobMutation.mutate()} disabled={createEmbeddingJobMutation.isPending || embeddingJobs.data.some((job) => ACTIVE_EMBEDDING_STATUSES.includes(job.status))}><Play />Generate embeddings</Button></div>
    <DataTable columns={columns} data={embeddingJobs.data} emptyMessage="No embedding jobs have been started for this matter." fitToWidth />
  </section>;
}

function EmbeddingOutcomes({ matterId, job }: { matterId: string; job: MatterEmbeddingJobRead }) {
  const searchSkippedCount = job.search_projection_skipped_count ?? 0;
  const searchSkippedDocumentIds = job.search_projection_skipped_document_ids ?? [];
  return <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
    <EmbeddingOutcomeCount matterId={matterId} job={job} status="SUCCEEDED" count={job.embedded_count} label="Generated" />
    <EmbeddingOutcomeCount matterId={matterId} job={job} status="REUSED" count={job.reused_count} label="Reused" muted />
    <EmbeddingOutcomeCount matterId={matterId} job={job} status="SKIPPED" count={job.skipped_count} label="Skipped" muted />
    <EmbeddingOutcomeCount matterId={matterId} job={job} status="FAILED" count={job.failed_count} label="Failed" destructive={job.failed_count > 0} />
    {searchSkippedCount > 0
      ? <span className="col-span-2 flex min-w-0 items-baseline justify-between gap-1 text-amber-700 dark:text-amber-400" title={searchSkippedDocumentIds.join(", ")}><span className="truncate">Index skipped</span><span className="tabular-nums">{searchSkippedCount.toLocaleString()}</span></span>
      : null}
  </div>;
}

function EmbeddingOutcomeCount({ matterId, job, status, count, label, muted = false, destructive = false }: {
  matterId: string;
  job: MatterEmbeddingJobRead;
  status: "SUCCEEDED" | "REUSED" | "SKIPPED" | "FAILED";
  count: number;
  label: string;
  muted?: boolean;
  destructive?: boolean;
}) {
  const terminal = TERMINAL_JOB_STATUSES.includes(job.status);
  const classes = `tabular-nums ${destructive ? "text-destructive" : muted ? "text-muted-foreground" : ""}`;
  const value = !terminal ? <span className={classes}>{count.toLocaleString()}</span> : <JobOutcomesDialog
    matterId={matterId}
    jobType="MATTER_EMBEDDING_JOB"
    jobId={job.id}
    jobName={`${job.embedding_model} embedding job`}
    initialStatus={status}
    trigger={<button type="button" className={`${classes} underline-offset-4 hover:text-primary hover:underline`} aria-label={`View ${label.toLowerCase()} outcomes`}>{count.toLocaleString()}</button>}
  />;
  return <span className="flex min-w-0 items-baseline justify-between gap-1"><span className="truncate text-muted-foreground">{label}</span>{value}</span>;
}

function EmbeddingJobActions({ matterId, job, canCancel, canceling, canDelete, onCancel, onDelete, onRetryIndex, retryingIndex }: {
  matterId: string;
  job: MatterEmbeddingJobRead;
  canCancel: boolean;
  canceling: boolean;
  canDelete: boolean;
  onCancel: () => void;
  onDelete: (jobType: string, jobId: string) => Promise<void>;
  onRetryIndex: () => void;
  retryingIndex: boolean;
}) {
  const [outcomesOpen, setOutcomesOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const jobName = `${job.embedding_model} embedding job`;
  const actions = [
    ...(job.search_projection_retry_available ? [{
      id: "retry-index",
      label: "Retry indexing",
      icon: <RotateCcw aria-hidden="true" className="size-4" />,
      disabled: retryingIndex,
      onSelect: onRetryIndex,
    }] : []),
    {
      id: "outcomes",
      label: "View outcomes",
      icon: <FileSearch aria-hidden="true" className="size-4" />,
      onSelect: () => setOutcomesOpen(true),
    },
    ...(canCancel ? [{
      id: "cancel",
      label: "Cancel job",
      icon: <Ban aria-hidden="true" className="size-4" />,
      disabled: canceling,
      destructive: true,
      onSelect: onCancel,
    }] : []),
    ...(canDelete ? [{
      id: "delete",
      label: "Delete job",
      icon: <Trash2 aria-hidden="true" className="size-4" />,
      destructive: true,
      onSelect: () => setDeleteOpen(true),
    }] : []),
  ];
  return <>
    <ActionMenuButton label={jobName} actions={actions} />
    <JobOutcomesDialog matterId={matterId} jobType="MATTER_EMBEDDING_JOB" jobId={job.id} jobName={jobName} open={outcomesOpen} onOpenChange={setOutcomesOpen} trigger={null} />
    <SoftDeleteDialog open={deleteOpen} onOpenChange={setDeleteOpen} resourceType="job" resourceName={jobName} onDelete={() => onDelete("MATTER_EMBEDDING_JOB", job.id)} />
  </>;
}

function EmbeddingProgress({ job }: { job: MatterEmbeddingJobRead }) {
  const completed = job.execution?.progress_completed ?? job.processed_count;
  const total = job.execution?.progress_total ?? job.total_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) ? 100 : 0;
  return <div className="min-w-0"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
}

function EmbeddingUsage({ job }: { job: MatterEmbeddingJobRead }) {
  const tokenTitle = `${job.input_tokens.toLocaleString()} input · ${job.output_tokens.toLocaleString()} output · ${job.provider_request_count.toLocaleString()} ${job.provider_request_count === 1 ? "request" : "requests"}`;
  return <div className="min-w-0">
    <p className="truncate font-semibold tabular-nums">{job.chunk_count.toLocaleString()} chunks</p>
    {job.provider_request_count || job.total_tokens
      ? <p className="truncate text-xs tabular-nums text-muted-foreground" title={tokenTitle}>{job.total_tokens.toLocaleString()} tokens</p>
      : <p className="truncate text-xs text-muted-foreground">No token usage</p>}
  </div>;
}
