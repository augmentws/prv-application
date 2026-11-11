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
import type { ClassificationModelRead, ClassificationScoringRunRead, JobExecutionRead, MatterAnalysisTaskRead, MatterAnalysisTaskRunRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { classificationScoringRunHref, matterHref, matterJobsHref } from "@/lib/matter-routes";

type ClassificationAnalysisJobRow = MatterAnalysisTaskRunRead & { job_kind: "ANALYSIS_TASK"; task_name: string };
type ClassificationScoringJobRow = ClassificationScoringRunRead & { job_kind: "SCORING_RUN"; task_name: string };
type ClassificationJobRow = ClassificationAnalysisJobRow | ClassificationScoringJobRow;

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
        return taskRuns.map((run) => ({ ...run, job_kind: "ANALYSIS_TASK" as const, task_name: task.name }));
      }));
      return runs.flat().sort((left, right) => right.created_at.localeCompare(left.created_at));
    },
    enabled: classificationTasks.isSuccess,
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL_JOB_STATUSES.includes(run.status)) ? 1500 : false,
  });
  const classificationScoringJobs = useQuery({
    queryKey: ["matter-classification-scoring-jobs", matterId],
    queryFn: async () => {
      const [runs, models] = await Promise.all([
        coreApi<ClassificationScoringRunRead[]>(`/v1/matters/${matterId}/classification-models/scoring-runs`),
        coreApi<ClassificationModelRead[]>(`/v1/matters/${matterId}/classification-models`),
      ]);
      const modelNames = new Map(models.map((model) => [model.id, model.name]));
      return runs.map((run) => ({ ...run, job_kind: "SCORING_RUN" as const, task_name: `${modelNames.get(run.classification_model_id) ?? "Classifier"} scoring` }));
    },
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL_JOB_STATUSES.includes(run.status)) ? 1500 : false,
  });
  const deleteAnalysisJob = useDeleteJob(["matter-classification-analysis-jobs", matterId]);
  const deleteScoringJob = useDeleteJob(["matter-classification-scoring-jobs", matterId]);
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
  const cancelClassificationScoringMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_SCORING_RUN/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-classification-scoring-jobs", matterId] });
      toast.success("The classifier scoring run was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The classifier scoring run could not be canceled."),
  });
  const retryClassificationScoringMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_SCORING_RUN/${runId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-classification-scoring-jobs", matterId] });
      toast.success("The classifier scoring run was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The classifier scoring run could not be retried."),
  });
  // The selected run lives in the URL, so Back closes it.
  const openJob = useCallback((jobId: string) => router.push(matterJobsHref(clientId, matterId, "CLASSIFICATION_ANALYSIS", { job: jobId })), [clientId, matterId, router]);
  const closeJob = useCallback(() => router.push(matterJobsHref(clientId, matterId, "CLASSIFICATION_ANALYSIS")), [clientId, matterId, router]);
  const jobs = useMemo<ClassificationJobRow[]>(() => [
    ...(classificationAnalysisJobs.data ?? []),
    ...(classificationScoringJobs.data ?? []),
  ].sort((left, right) => right.created_at.localeCompare(left.created_at)), [classificationAnalysisJobs.data, classificationScoringJobs.data]);
  const columns = useMemo<ColumnDef<ClassificationJobRow>[]>(() => [
    { accessorKey: "task_name", header: "Analysis task", cell: ({ row }) => <div><p className="font-semibold">{row.original.task_name}</p><p className="text-xs text-muted-foreground">{row.original.job_kind === "SCORING_RUN" ? `${row.original.execution?.notification_parent_type === "REVIEW_PROGRAM_RUN" ? "Review Program" : "Direct scoring"} · ${row.original.dataset_scope_type === "MATTER" ? "Whole matter" : "Review batch"}` : row.original.scope_type === "MATTER" ? "Whole matter" : "Review batch"}</p></div> },
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
      return <div className="min-w-44"><ProgressBar percent={percent} /><p className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"} stages</p>{row.original.population_document_count ? <p className="text-xs tabular-nums text-muted-foreground">{row.original.job_kind === "SCORING_RUN" ? `${row.original.scored_document_count.toLocaleString()} of ${row.original.population_document_count.toLocaleString()} documents scored` : `${row.original.population_document_count.toLocaleString()} documents frozen`}</p> : null}</div>;
    } },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const actions = row.original.execution?.available_actions ?? [];
      const terminal = TERMINAL_JOB_STATUSES.includes(row.original.status);
      const canRetry = actions.includes("RETRY") || row.original.status === "CANCELED" || (row.original.status === "FAILED" && row.original.failure_retriable);
      const scoring = row.original.job_kind === "SCORING_RUN";
      const canceling = scoring ? cancelClassificationScoringMutation.isPending : cancelClassificationAnalysisMutation.isPending;
      const retrying = scoring ? retryClassificationScoringMutation.isPending : retryClassificationAnalysisMutation.isPending;
      const cancel = scoring ? cancelClassificationScoringMutation.mutate : cancelClassificationAnalysisMutation.mutate;
      const retry = scoring ? retryClassificationScoringMutation.mutate : retryClassificationAnalysisMutation.mutate;
      const primary = scoring
        ? <Link href={classificationScoringRunHref(clientId, matterId, row.original.classification_model_id, row.original.id)}><FileSearch />View job</Link>
        : <button type="button" onClick={() => openJob(row.original.id)}><FileSearch />View job</button>;
      return <div className="flex justify-end gap-2">{!terminal && actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={canceling} onClick={() => cancel(row.original.id)}><Ban />Cancel</Button> : null}{canRetry ? <Button size="sm" variant="outline" disabled={retrying} onClick={() => retry(row.original.id)}><RotateCcw />Retry</Button> : null}<DeletableJobActions canDelete={canDeleteJobs && terminal} jobType={scoring ? "CLASSIFICATION_SCORING_RUN" : "CLASSIFICATION_ANALYSIS_TASK"} jobId={row.original.id} jobName={row.original.task_name} onDelete={scoring ? deleteScoringJob : deleteAnalysisJob} primary={primary} /></div>;
    } },
  ], [canDeleteJobs, cancelClassificationAnalysisMutation, cancelClassificationScoringMutation, clientId, deleteAnalysisJob, deleteScoringJob, matterId, openJob, retryClassificationAnalysisMutation, retryClassificationScoringMutation]);

  if (classificationTasks.isPending || classificationAnalysisJobs.isPending || classificationScoringJobs.isPending) return <TableLoading />;
  if (classificationTasks.error || classificationAnalysisJobs.error || classificationScoringJobs.error) return <QueryError message={classificationTasks.error?.message ?? classificationAnalysisJobs.error?.message ?? classificationScoringJobs.error?.message} />;
  const selectedRun = selectedJobId ? classificationAnalysisJobs.data.find((run) => run.id === selectedJobId) : undefined;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><BrainCircuit className="size-5 text-primary" />Classification analysis</h2><p className="text-sm text-muted-foreground">Classifier runs across a review batch or the whole matter. Results remain analysis output until explicitly published.</p></div><Button asChild variant="outline"><Link href={matterHref(clientId, matterId, "analysis")}><FileSearch />Analysis tasks</Link></Button></div>
    {selectedRun ? <ClassificationAnalysisJobDetail clientId={clientId} matterId={matterId} run={selectedRun} onClose={closeJob} /> : null}
    <DataTable columns={columns} data={jobs} emptyMessage="No classification analysis or scoring jobs have been started for this matter." />
  </section>;
}
