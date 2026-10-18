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
import { Button } from "@/components/ui/button";
import { SplitActionButton } from "@/components/ui/split-action-button";
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
    { accessorKey: "embedding_model", header: "Model", cell: ({ row }) => <div><p className="max-w-64 truncate font-semibold">{row.original.embedding_model}</p><p className="text-xs text-muted-foreground">{row.original.embedding_dimensions.toLocaleString()} dimensions</p>{row.original.error_message ? <p className="mt-1 max-w-80 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div className="space-y-1"><StatusBadge status={row.original.status} />{row.original.execution ? <p className="whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <EmbeddingProgress job={row.original} /> },
    { accessorKey: "embedded_count", header: "Generated", cell: ({ row }) => <EmbeddingOutcomeCount matterId={matterId} job={row.original} status="SUCCEEDED" count={row.original.embedded_count} /> },
    { accessorKey: "reused_count", header: "Reused", cell: ({ row }) => <EmbeddingOutcomeCount matterId={matterId} job={row.original} status="REUSED" count={row.original.reused_count} muted /> },
    { accessorKey: "skipped_count", header: "Skipped", cell: ({ row }) => <EmbeddingOutcomeCount matterId={matterId} job={row.original} status="SKIPPED" count={row.original.skipped_count} muted /> },
    { accessorKey: "failed_count", header: "Failed", cell: ({ row }) => <EmbeddingOutcomeCount matterId={matterId} job={row.original} status="FAILED" count={row.original.failed_count} destructive={row.original.failed_count > 0} /> },
    { accessorKey: "chunk_count", header: "Chunks", cell: ({ row }) => <span className="tabular-nums">{row.original.chunk_count.toLocaleString()}</span> },
    { id: "provider_usage", header: "Token usage", cell: ({ row }) => <EmbeddingTokenUsage job={row.original} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => (row.original.execution?.available_actions.includes("CANCEL") ?? ACTIVE_EMBEDDING_STATUSES.includes(row.original.status))
      ? <Button size="sm" variant="outline" onClick={() => cancelEmbeddingJobMutation.mutate(row.original.id)}><Ban />Cancel</Button>
      : <EmbeddingJobActions
          matterId={matterId}
          job={row.original}
          canDelete={canDeleteJobs}
          onDelete={deleteJob}
          onRetryIndex={() => retryEmbeddingIndexMutation.mutate(row.original.id)}
          retryingIndex={retryEmbeddingIndexMutation.isPending}
        /> },
  ], [canDeleteJobs, cancelEmbeddingJobMutation, deleteJob, matterId, retryEmbeddingIndexMutation]);

  if (embeddingJobs.isPending) return <TableLoading />;
  if (embeddingJobs.error) return <QueryError message={embeddingJobs.error.message} />;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Document embeddings</h2><p className="text-sm text-muted-foreground">Create sentence-aware chunks and vectors for every document in this matter.</p></div><Button onClick={() => createEmbeddingJobMutation.mutate()} disabled={createEmbeddingJobMutation.isPending || embeddingJobs.data.some((job) => ACTIVE_EMBEDDING_STATUSES.includes(job.status))}><Play />Generate embeddings</Button></div>
    <DataTable columns={columns} data={embeddingJobs.data} emptyMessage="No embedding jobs have been started for this matter." />
  </section>;
}

function EmbeddingOutcomeCount({ matterId, job, status, count, muted = false, destructive = false }: {
  matterId: string;
  job: MatterEmbeddingJobRead;
  status: "SUCCEEDED" | "REUSED" | "SKIPPED" | "FAILED";
  count: number;
  muted?: boolean;
  destructive?: boolean;
}) {
  const terminal = TERMINAL_JOB_STATUSES.includes(job.status);
  const label = { SUCCEEDED: "generated", REUSED: "reused", SKIPPED: "skipped", FAILED: "failed" }[status];
  const classes = `tabular-nums ${destructive ? "text-destructive" : muted ? "text-muted-foreground" : ""}`;
  if (!terminal) return <span className={classes}>{count.toLocaleString()}</span>;
  return <JobOutcomesDialog
    matterId={matterId}
    jobType="MATTER_EMBEDDING_JOB"
    jobId={job.id}
    jobName={`${job.embedding_model} embedding job`}
    initialStatus={status}
    trigger={<button type="button" className={`${classes} underline-offset-4 hover:text-primary hover:underline`} aria-label={`View ${label} outcomes`}>{count.toLocaleString()}</button>}
  />;
}

function EmbeddingJobActions({ matterId, job, canDelete, onDelete, onRetryIndex, retryingIndex }: {
  matterId: string;
  job: MatterEmbeddingJobRead;
  canDelete: boolean;
  onDelete: (jobType: string, jobId: string) => Promise<void>;
  onRetryIndex: () => void;
  retryingIndex: boolean;
}) {
  const [outcomesOpen, setOutcomesOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const jobName = `${job.embedding_model} embedding job`;
  const actions = [
    ...(job.status === "FAILED" ? [{
      id: "retry-index",
      label: "Retry indexing",
      icon: <RotateCcw aria-hidden="true" className="size-4" />,
      disabled: retryingIndex,
      onSelect: onRetryIndex,
    }] : []),
    ...(canDelete ? [{
      id: "delete",
      label: "Delete job",
      icon: <Trash2 aria-hidden="true" className="size-4" />,
      destructive: true,
      onSelect: () => setDeleteOpen(true),
    }] : []),
  ];
  const primary = <button type="button" onClick={() => setOutcomesOpen(true)}><FileSearch />View outcomes</button>;
  return <>
    {actions.length ? <SplitActionButton label={jobName} primary={primary} variant="outline" actions={actions} /> : <Button type="button" size="sm" variant="outline" onClick={() => setOutcomesOpen(true)}><FileSearch />View outcomes</Button>}
    <JobOutcomesDialog matterId={matterId} jobType="MATTER_EMBEDDING_JOB" jobId={job.id} jobName={jobName} open={outcomesOpen} onOpenChange={setOutcomesOpen} trigger={null} />
    <SoftDeleteDialog open={deleteOpen} onOpenChange={setDeleteOpen} resourceType="job" resourceName={jobName} onDelete={() => onDelete("MATTER_EMBEDDING_JOB", job.id)} />
  </>;
}

function EmbeddingProgress({ job }: { job: MatterEmbeddingJobRead }) {
  const completed = job.execution?.progress_completed ?? job.processed_count;
  const total = job.execution?.progress_total ?? job.total_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) ? 100 : 0;
  return <div className="min-w-36"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
}

function EmbeddingTokenUsage({ job }: { job: MatterEmbeddingJobRead }) {
  if (!job.provider_request_count && !job.total_tokens) {
    return <span className="text-sm text-muted-foreground">No external usage</span>;
  }
  return <div className="min-w-40"><p className="font-semibold tabular-nums">{job.total_tokens.toLocaleString()} tokens</p><p className="text-xs tabular-nums text-muted-foreground">{job.input_tokens.toLocaleString()} in · {job.output_tokens.toLocaleString()} out</p><p className="text-xs tabular-nums text-muted-foreground">{job.provider_request_count.toLocaleString()} {job.provider_request_count === 1 ? "request" : "requests"}</p></div>;
}
