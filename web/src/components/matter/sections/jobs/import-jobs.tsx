"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, BriefcaseBusiness } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { useMatter } from "@/components/matter/matter-context";
import { DeletableJobActions, ProgressBar, TERMINAL_JOB_STATUSES, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { MatterDocumentImportRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { matterJobsHref } from "@/lib/matter-routes";

export function ImportJobs({ selectedJobId }: { selectedJobId?: string }) {
  const { clientId, matterId, permissionKeys } = useMatter();
  const router = useRouter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const jobs = useQuery({
    queryKey: ["matter-document-imports", matterId],
    queryFn: () => coreApi<MatterDocumentImportRead[]>(`/v1/matters/${matterId}/document-imports`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const deleteJob = useDeleteJob(["matter-document-imports", matterId]);
  const cancelJobMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<MatterDocumentImportRead>(`/v1/matters/${matterId}/document-imports/${jobId}/cancel`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterDocumentImportRead[]>(["matter-document-imports", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      toast.success("The import job was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The import job could not be canceled."),
  });
  const columns = useMemo<ColumnDef<MatterDocumentImportRead>[]>(() => [
    { accessorKey: "selection_summary", header: "Selection", cell: ({ row }) => <button type="button" className="max-w-md text-left font-semibold hover:text-primary" onClick={() => router.push(matterJobsHref(clientId, matterId, "DOCUMENT_IMPORTS", { job: row.original.id }))}>{row.original.selection_summary}</button> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div className="space-y-1"><StatusBadge status={row.original.status} />{row.original.execution ? <p className="whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <ImportProgress job={row.original} /> },
    { accessorKey: "added_count", header: "Added", cell: ({ row }) => <span className="tabular-nums">{row.original.added_count.toLocaleString()}</span> },
    { accessorKey: "duplicate_count", header: "Already present", cell: ({ row }) => <span className="tabular-nums text-muted-foreground">{row.original.duplicate_count.toLocaleString()}</span> },
    { accessorKey: "imported_label_event_count", header: "Imported labels", cell: ({ row }) => <span className="tabular-nums">{(row.original.imported_label_event_count ?? 0).toLocaleString()}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => TERMINAL_JOB_STATUSES.includes(row.original.status) ? <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_DOCUMENT_IMPORT" jobId={row.original.id} jobName={row.original.selection_summary} onDelete={deleteJob} /> : null },
  ], [canDeleteJobs, clientId, deleteJob, matterId, router]);

  if (jobs.isPending) return <TableLoading />;
  if (jobs.error) return <QueryError message={jobs.error.message} />;
  return <section className="space-y-4">
    <div><h2 className="text-lg font-semibold">Document imports</h2><p className="text-sm text-muted-foreground">Jobs that add collection documents to this matter.</p></div>
    {selectedJobId ? <ImportJobDetail job={jobs.data.find((job) => job.id === selectedJobId)} onCancel={(jobId) => cancelJobMutation.mutate(jobId)} /> : null}
    <DataTable columns={columns} data={jobs.data} emptyMessage="No document import jobs have been started for this matter." />
  </section>;
}

function ImportProgress({ job }: { job: MatterDocumentImportRead }) {
  const completed = job.execution?.progress_completed ?? job.processed_count;
  const total = job.execution?.progress_total ?? job.matched_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-36"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"}</span></div>;
}

function ImportJobDetail({ job, onCancel }: { job?: MatterDocumentImportRead; onCancel: (jobId: string) => void }) {
  if (!job) return <Card className="p-5 text-sm text-muted-foreground">The selected job is not available in the recent job history.</Card>;
  const active = job.execution?.available_actions.includes("CANCEL") ?? ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status);
  return <Card className="p-5"><div className="flex flex-wrap items-start justify-between gap-4"><div><div className="mb-2 flex items-center gap-2"><BriefcaseBusiness className="size-4 text-primary" /><StatusBadge status={job.status} /></div><h2 className="text-lg font-semibold">{job.selection_summary}</h2><p className="mt-1 text-sm text-muted-foreground">Created {formatDate(job.created_at)} · {job.batch_count.toLocaleString()} {job.batch_count === 1 ? "batch" : "batches"}{job.execution ? ` · ${job.execution.stage_label} · attempt ${job.execution.attempt_number}` : ""}</p></div>{active ? <Button variant="outline" onClick={() => onCancel(job.id)}><Ban />Cancel job</Button> : null}</div><dl className="mt-5 grid gap-3 text-sm sm:grid-cols-4"><JobMetric label="Matched" value={job.matched_count} /><JobMetric label="Processed" value={job.processed_count} /><JobMetric label="Added" value={job.added_count} /><JobMetric label="Already present" value={job.duplicate_count} /><JobMetric label="Documents labeled" value={job.imported_label_document_count ?? 0} /><JobMetric label="Imported labels" value={job.imported_label_event_count ?? 0} /><JobMetric label="Label warnings" value={job.imported_label_warning_count ?? 0} /></dl>{job.error_message ? <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{job.error_message}</p> : null}</Card>;
}

function JobMetric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-lg bg-muted/50 p-3"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{value.toLocaleString()}</dd></div>;
}
