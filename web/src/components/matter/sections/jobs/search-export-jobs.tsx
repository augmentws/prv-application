"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, Download, FileDown, RotateCcw, Search } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { useMatter } from "@/components/matter/matter-context";
import { DeletableJobActions, ProgressBar, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { JobExecutionRead, MatterSearchExportRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { searchAndReviewHref } from "@/lib/matter-routes";

export function SearchExportJobs() {
  const { matterId, permissionKeys } = useMatter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const searchExportJobs = useQuery({
    queryKey: ["matter-search-export-jobs", matterId],
    queryFn: () => coreApi<MatterSearchExportRead[]>(`/v1/matters/${matterId}/search-exports?limit=500`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "RUNNING"].includes(job.status)) ? 1500 : false,
  });
  const deleteJob = useDeleteJob(["matter-search-export-jobs", matterId]);
  const cancelSearchExportMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<JobExecutionRead>(`/v1/jobs/MATTER_SEARCH_EXPORT/${jobId}/cancel`, { method: "POST" }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ["matter-search-export-jobs", matterId] }); toast.success("The search export was canceled."); },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The search export could not be canceled."),
  });
  const retrySearchExportMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<JobExecutionRead>(`/v1/jobs/MATTER_SEARCH_EXPORT/${jobId}/retry`, { method: "POST" }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ["matter-search-export-jobs", matterId] }); toast.success("The search export was queued again."); },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The search export could not be retried."),
  });
  const columns = useMemo<ColumnDef<MatterSearchExportRead>[]>(() => [
    { accessorKey: "requested_filename", header: "Export", cell: ({ row }) => <div><p className="font-semibold">{row.original.requested_filename}</p><p className="text-xs text-muted-foreground">{row.original.selected_field_count.toLocaleString()} columns · requested by {row.original.created_by_display_name}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div><StatusBadge status={row.original.status} />{row.original.execution ? <p className="mt-1 whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}{row.original.failure_summary ? <p className="mt-1 max-w-72 truncate text-xs text-destructive" title={row.original.failure_summary}>{row.original.failure_summary}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <SearchExportProgress job={row.original} /> },
    { accessorKey: "output_byte_length", header: "Size", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatBytes(row.original.output_byte_length)}</span> },
    { accessorKey: "expires_at", header: "Available until", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{row.original.expires_at ? formatDate(row.original.expires_at) : "—"}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const actions = row.original.execution?.available_actions ?? [];
      const terminal = ["COMPLETED", "FAILED", "CANCELED"].includes(row.original.status);
      const expired = row.original.expires_at != null && Date.parse(row.original.expires_at) <= Date.now();
      if (actions.includes("CANCEL")) return <Button size="sm" variant="outline" disabled={cancelSearchExportMutation.isPending} onClick={() => cancelSearchExportMutation.mutate(row.original.id)}><Ban />Cancel</Button>;
      if (actions.includes("RETRY")) return <div className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled={retrySearchExportMutation.isPending} onClick={() => retrySearchExportMutation.mutate(row.original.id)}><RotateCcw />Retry</Button><DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_SEARCH_EXPORT" jobId={row.original.id} jobName={row.original.requested_filename} onDelete={deleteJob} /></div>;
      if (row.original.status === "COMPLETED" && expired) return <div className="flex justify-end gap-2"><Button size="sm" variant="outline" disabled><Download />Expired</Button><DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_SEARCH_EXPORT" jobId={row.original.id} jobName={row.original.requested_filename} onDelete={deleteJob} /></div>;
      return <DeletableJobActions canDelete={canDeleteJobs && terminal} jobType="MATTER_SEARCH_EXPORT" jobId={row.original.id} jobName={row.original.requested_filename} onDelete={deleteJob} primary={row.original.status === "COMPLETED" ? <a href={`/api/core/v1/matters/${matterId}/search-exports/${row.original.id}/download`}><Download />Download CSV</a> : undefined} />;
    } },
  ], [canDeleteJobs, cancelSearchExportMutation, deleteJob, matterId, retrySearchExportMutation]);

  if (searchExportJobs.isPending) return <TableLoading />;
  if (searchExportJobs.error) return <QueryError message={searchExportJobs.error.message} />;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><FileDown className="size-5 text-primary" />Search exports</h2><p className="text-sm text-muted-foreground">Durable CSV exports created from frozen matter-search definitions. Completed files are stored by the Artifact Service.</p></div><Button asChild variant="outline"><Link href={searchAndReviewHref(matterId)}><Search />Search & Review</Link></Button></div>
    <DataTable columns={columns} data={searchExportJobs.data} emptyMessage="No search exports have been created for this matter." />
  </section>;
}

function SearchExportProgress({ job }: { job: MatterSearchExportRead }) {
  const total = job.matched_count;
  const completed = job.exported_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-40"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total ? total.toLocaleString() : "—"} rows</span></div>;
}

function formatBytes(value: number | null) {
  if (value == null) return "—";
  if (value < 1024) return `${value.toLocaleString()} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}
