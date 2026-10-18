"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, BrainCircuit, FileSearch, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { ClassificationAnalysisJobDetail } from "@/components/classification-analysis-job-detail";
import { DataTable } from "@/components/data-table";
import { useMatter } from "@/components/matter/matter-context";
import { DeletableJobActions, ProgressBar, TERMINAL_JOB_STATUSES, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { JobExecutionRead, MatterAnalysisTaskRead, MatterAnalysisTaskRunRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { matterHref, matterJobsHref } from "@/lib/matter-routes";

type ClassificationAnalysisJobRow = MatterAnalysisTaskRunRead & { task_name: string };

export function ClassificationAnalysisJobs({ selectedJobId }: { selectedJobId?: string }) {
  const { clientId, matterId, permissionKeys } = useMatter();
  const router = useRouter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const classificationTasks = useQuery({
    queryKey: ["matter-classification-analysis-tasks", matterId],
    queryFn: () => coreApi<MatterAnalysisTaskRead[]>(`/v1/matters/${matterId}/analysis-tasks`),
  });
  const classificationTaskIds = (classificationTasks.data ?? [])
    .filter((task) => task.task_type === "CLASSIFICATION")
    .map((task) => task.id)
    .sort();
  const classificationAnalysisJobs = useQuery({
    queryKey: ["matter-classification-analysis-jobs", matterId, classificationTaskIds.join(",")],
    queryFn: async () => {
      const tasks = (classificationTasks.data ?? []).filter((task) => task.task_type === "CLASSIFICATION");
      const runs = await Promise.all(tasks.map(async (task) => {
        const taskRuns = await coreApi<MatterAnalysisTaskRunRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/runs`);
        return taskRuns.map((run) => ({ ...run, task_name: task.name }));
      }));
      return runs.flat().sort((left, right) => right.created_at.localeCompare(left.created_at));
    },
    enabled: classificationTasks.isSuccess,
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL_JOB_STATUSES.includes(run.status)) ? 1500 : false,
  });
  const deleteJob = useDeleteJob(["matter-classification-analysis-jobs", matterId]);
  const cancelClassificationAnalysisMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-classification-analysis-jobs", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId] });
      toast.success("The classification run was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The classification run could not be canceled."),
  });
  const retryClassificationAnalysisMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/${runId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-classification-analysis-jobs", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId] });
      toast.success("The classification run was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The classification run could not be retried."),
  });
  // The selected run lives in the URL, so Back closes it.
  const openJob = useCallback((jobId: string) => router.push(matterJobsHref(clientId, matterId, "CLASSIFICATION_ANALYSIS", { job: jobId })), [clientId, matterId, router]);
  const closeJob = useCallback(() => router.push(matterJobsHref(clientId, matterId, "CLASSIFICATION_ANALYSIS")), [clientId, matterId, router]);
  const columns = useMemo<ColumnDef<ClassificationAnalysisJobRow>[]>(() => [
    { accessorKey: "task_name", header: "Analysis task", cell: ({ row }) => <div><p className="font-semibold">{row.original.task_name}</p><p className="text-xs text-muted-foreground">{row.original.scope_type === "MATTER" ? "Whole matter" : "Review batch"}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => {
      const execution = row.original.execution;
      const terminal = TERMINAL_JOB_STATUSES.includes(row.original.status);
      const domainStage = row.original.current_stage.replaceAll("_", " ").toLowerCase();
      const stage = terminal ? `${domainStage.charAt(0).toUpperCase()}${domainStage.slice(1)}` : execution?.stage_label ?? row.original.current_stage;
      const failure = terminal ? row.original.failure_summary ?? execution?.failure_summary : execution?.failure_summary ?? row.original.failure_summary;
      return <div className="space-y-1"><StatusBadge status={row.original.status} /><p className="whitespace-nowrap text-xs text-muted-foreground">{stage} · attempt {execution?.attempt_number ?? 1}</p>{failure ? <p className="max-w-80 truncate text-xs text-destructive" title={failure}>{failure}</p> : null}</div>;
    } },
    { id: "progress", header: "Progress", cell: ({ row }) => {
      const completed = row.original.execution?.progress_completed ?? row.original.progress_completed;
      const total = row.original.execution?.progress_total ?? row.original.progress_total;
      const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
      return <div className="min-w-44"><ProgressBar percent={percent} /><p className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"} stages</p>{row.original.population_document_count ? <p className="text-xs tabular-nums text-muted-foreground">{row.original.population_document_count.toLocaleString()} documents frozen</p> : null}</div>;
    } },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const actions = row.original.execution?.available_actions ?? [];
      const terminal = TERMINAL_JOB_STATUSES.includes(row.original.status);
      const canRetry = actions.includes("RETRY") || row.original.status === "CANCELED" || (row.original.status === "FAILED" && row.original.failure_retriable);
      return <div className="flex justify-end gap-2">{!terminal && actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelClassificationAnalysisMutation.isPending} onClick={() => cancelClassificationAnalysisMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null}{canRetry ? <Button size="sm" variant="outline" disabled={retryClassificationAnalysisMutation.isPending} onClick={() => retryClassificationAnalysisMutation.mutate(row.original.id)}><RotateCcw />Retry</Button> : null}<DeletableJobActions canDelete={canDeleteJobs && terminal} jobType="CLASSIFICATION_ANALYSIS_TASK" jobId={row.original.id} jobName={row.original.task_name} onDelete={deleteJob} primary={<button type="button" onClick={() => openJob(row.original.id)}><FileSearch />View job</button>} /></div>;
    } },
  ], [canDeleteJobs, cancelClassificationAnalysisMutation, deleteJob, openJob, retryClassificationAnalysisMutation]);

  if (classificationTasks.isPending || classificationAnalysisJobs.isPending) return <TableLoading />;
  if (classificationTasks.error || classificationAnalysisJobs.error) return <QueryError message={classificationTasks.error?.message ?? classificationAnalysisJobs.error?.message} />;
  const selectedRun = selectedJobId ? classificationAnalysisJobs.data.find((run) => run.id === selectedJobId) : undefined;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><BrainCircuit className="size-5 text-primary" />Classification analysis</h2><p className="text-sm text-muted-foreground">Classifier runs across a review batch or the whole matter. Results remain analysis output until explicitly published.</p></div><Button asChild variant="outline"><Link href={matterHref(clientId, matterId, "analysis")}><FileSearch />Analysis tasks</Link></Button></div>
    {selectedRun ? <ClassificationAnalysisJobDetail clientId={clientId} matterId={matterId} run={selectedRun} onClose={closeJob} /> : null}
    <DataTable columns={columns} data={classificationAnalysisJobs.data} emptyMessage="No classification analysis jobs have been started for this matter." />
  </section>;
}
