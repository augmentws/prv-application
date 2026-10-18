"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, Ban, FileSearch, Sparkles, Trash2 } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { JobFailedDocumentsDialog } from "@/components/job-failed-documents-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { ProgressBar, TERMINAL_JOB_STATUSES, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { SoftDeleteDialog } from "@/components/soft-delete-dialog";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { SplitActionButton } from "@/components/ui/split-action-button";
import type { JobExecutionRead, MatterQuestionAnsweringJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { matterHref } from "@/lib/matter-routes";

export function QuestionAnsweringJobs() {
  const { clientId, matterId, permissionKeys } = useMatter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const questionAnsweringJobs = useQuery({
    queryKey: ["matter-question-answering-jobs", matterId],
    queryFn: () => coreApi<MatterQuestionAnsweringJobRead[]>(`/v1/matters/${matterId}/analysis-tasks/question-answering-jobs`),
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL_JOB_STATUSES.includes(run.status)) ? 1500 : false,
  });
  const deleteJob = useDeleteJob(["matter-question-answering-jobs", matterId]);
  const cancelQuestionAnsweringMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/ANALYSIS_TASK_BATCH_RUN/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-question-answering-jobs", matterId] });
      toast.success("The question-answering run was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The question-answering run could not be canceled."),
  });
  const columns = useMemo<ColumnDef<MatterQuestionAnsweringJobRead>[]>(() => [
    { accessorKey: "task_name", header: "Analysis task", cell: ({ row }) => <div><p className="font-semibold">{row.original.task_name}</p>{row.original.task_version != null ? <p className="text-xs text-muted-foreground">Published v{row.original.task_version}</p> : null}</div> },
    { accessorKey: "review_batch_name", header: "Review batch", cell: ({ row }) => <Link className="font-medium text-primary hover:underline" href={`/review/matters/${matterId}?batch=${row.original.review_batch_id}`}>{row.original.review_batch_name}</Link> },
    { accessorKey: "decision_provider", header: "Provider", cell: ({ row }) => <div><p>{row.original.decision_provider === "OPENAI_DECISIONS" ? "OpenAI Decisions" : "Jev"}</p><p className="max-w-48 truncate text-xs text-muted-foreground" title={row.original.model_key}>{row.original.model_key}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => {
      const opensFailures = row.original.status === "COMPLETED_WITH_ERRORS" && (row.original.failed_document_count ?? 0) > 0;
      if (opensFailures) return <JobFailedDocumentsDialog matterId={matterId} job={row.original} trigger={<button type="button" aria-label={`Review errors for ${row.original.task_name}`} className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"><StatusBadge status={row.original.status} className="cursor-pointer" /></button>} />;
      return <div><StatusBadge status={row.original.status} />{row.original.execution ? <p className="mt-1 whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}{row.original.error_message ? <p className="mt-1 max-w-72 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div>;
    } },
    { id: "progress", header: "Progress", cell: ({ row }) => {
      const successful = row.original.successful_document_count;
      const total = row.original.document_count;
      const percent = total ? Math.min(100, Math.round((successful / total) * 100)) : 0;
      return <div className="min-w-44"><ProgressBar percent={percent} /><p className="text-xs tabular-nums text-muted-foreground">{successful.toLocaleString()} / {total.toLocaleString()} successful</p></div>;
    } },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => <div className="flex justify-end gap-2">{row.original.execution?.available_actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelQuestionAnsweringMutation.isPending} onClick={() => cancelQuestionAnsweringMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null}<QuestionAnsweringJobActions matterId={matterId} job={row.original} canDelete={canDeleteJobs && TERMINAL_JOB_STATUSES.includes(row.original.status)} onDelete={deleteJob} /></div> },
  ], [canDeleteJobs, cancelQuestionAnsweringMutation, deleteJob, matterId]);

  if (questionAnsweringJobs.isPending) return <TableLoading />;
  if (questionAnsweringJobs.error) return <QueryError message={questionAnsweringJobs.error.message} />;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><Sparkles className="size-5 text-primary" />Question answering</h2><p className="text-sm text-muted-foreground">Question-answering Analysis Task runs across review batches. Results remain isolated until explicitly published.</p></div><Button asChild variant="outline"><Link href={matterHref(clientId, matterId, "analysis")}><FileSearch />Analysis tasks</Link></Button></div>
    <DataTable columns={columns} data={questionAnsweringJobs.data} emptyMessage="No question-answering jobs have been started for this matter." />
  </section>;
}

function QuestionAnsweringJobActions({ matterId, job, canDelete, onDelete }: { matterId: string; job: MatterQuestionAnsweringJobRead; canDelete: boolean; onDelete: (jobType: string, jobId: string) => Promise<void> }) {
  const [failuresOpen, setFailuresOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const primary = <Link href={`/review/matters/${matterId}?batch=${job.review_batch_id}`}><FileSearch />Open batch</Link>;
  const actions = [
    ...((job.failed_document_count ?? 0) ? [{
      id: "view-errors",
      label: "View errors",
      icon: <AlertTriangle aria-hidden="true" className="size-4" />,
      onSelect: () => setFailuresOpen(true),
    }] : []),
    ...(canDelete ? [{
      id: "delete",
      label: "Delete job",
      icon: <Trash2 aria-hidden="true" className="size-4" />,
      destructive: true,
      onSelect: () => setDeleteOpen(true),
    }] : []),
  ];

  if (!actions.length) {
    return <Button asChild size="sm">{primary}</Button>;
  }

  return <>
    <SplitActionButton
      label={`${job.task_name} on ${job.review_batch_name}`}
      primary={primary}
      actions={actions}
    />
    <JobFailedDocumentsDialog
      matterId={matterId}
      job={job}
      open={failuresOpen}
      onOpenChange={setFailuresOpen}
      trigger={null}
    />
    <SoftDeleteDialog open={deleteOpen} onOpenChange={setDeleteOpen} resourceType="job" resourceName={job.task_name} onDelete={() => onDelete("ANALYSIS_TASK_BATCH_RUN", job.id)} />
  </>;
}
