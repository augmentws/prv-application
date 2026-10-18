"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, CheckCircle2, FileSearch } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateTopicJobDialog } from "@/components/forms/create-topic-job-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { DeletableJobActions, ProgressBar, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { useEmbeddingJobs } from "@/components/matter/sections/jobs/embedding-jobs";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { MatterSavedSearchRead, MatterTopicJobCreate, MatterTopicJobRead, MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const ACTIVE_TOPIC_STATUSES = ["QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"];

export function TopicJobs() {
  const { clientId, matterId, permissionKeys } = useMatter();
  const queryClient = useQueryClient();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const savedSearches = useQuery({ queryKey: ["matter-saved-searches", matterId], queryFn: () => coreApi<MatterSavedSearchRead[]>(`/v1/matters/${matterId}/saved-searches`) });
  const embeddingJobs = useEmbeddingJobs(matterId);
  const topicJobs = useQuery({
    queryKey: ["matter-topic-jobs", matterId],
    queryFn: () => coreApi<MatterTopicJobRead[]>(`/v1/matters/${matterId}/topic-jobs`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SAMPLING", "CLUSTERING", "PUBLISHING"].includes(job.status)) ? 2000 : false,
  });
  const deleteJob = useDeleteJob(["matter-topic-jobs", matterId]);
  const createTopicJobMutation = useMutation({
    mutationFn: (values: MatterTopicJobCreate) => coreApi<MatterTopicJobRead>(`/v1/matters/${matterId}/topic-jobs`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterTopicJobRead[]>(["matter-topic-jobs", matterId], (current) => current ? [created, ...current] : [created]);
      toast.success("The topic clustering job was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The topic clustering job could not be started."),
  });
  const cancelTopicJobMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<MatterTopicJobRead>(`/v1/matters/${matterId}/topic-jobs/${jobId}/cancel`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterTopicJobRead[]>(["matter-topic-jobs", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      toast.success("The topic clustering job was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The topic clustering job could not be canceled."),
  });
  const columns = useMemo<ColumnDef<MatterTopicJobRead>[]>(() => [
    { accessorKey: "operating_mode", header: "Mode", cell: ({ row }) => {
      const scope = row.original.scope_mode === "INCLUDE_SAVED_SEARCH"
        ? `only ${row.original.saved_search_name ?? "saved search"}`
        : row.original.scope_mode === "EXCLUDE_SAVED_SEARCH"
          ? `excluding ${row.original.saved_search_name ?? "saved search"}`
          : "entire matter";
      return <div><p className="font-semibold">{row.original.operating_mode === "AUTO" ? "Automatic" : `${row.original.requested_topic_count} topics`}</p><p className="text-xs text-muted-foreground">{row.original.sample_size.toLocaleString()} chunk sample · {row.original.assignment_mode.toLowerCase()} · {scope}</p></div>;
    } },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "progress", header: "Progress", cell: ({ row }) => <TopicProgress job={row.original} /> },
    { accessorKey: "topic_count", header: "Topics", cell: ({ row }) => <div><span className="tabular-nums">{row.original.topic_count.toLocaleString()}</span>{row.original.clusters?.length ? <p className="max-w-64 truncate text-xs text-muted-foreground" title={row.original.clusters.map((cluster) => cluster.name).join(", ")}>{row.original.clusters.slice(0, 3).map((cluster) => cluster.name).join(", ")}</p> : null}</div> },
    { accessorKey: "assigned_document_count", header: "Assigned", cell: ({ row }) => <span className="tabular-nums">{row.original.assigned_document_count.toLocaleString()}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const active = ACTIVE_TOPIC_STATUSES.includes(row.original.status);
      if (active) return <div className="flex justify-end gap-2">{row.original.status === "AWAITING_REVIEW" ? <Button asChild size="sm"><Link href={`/app/clients/${clientId}/matters/${matterId}/topic-jobs/${row.original.id}`}><CheckCircle2 />Review topics</Link></Button> : null}<Button size="sm" variant="outline" onClick={() => cancelTopicJobMutation.mutate(row.original.id)}><Ban />Cancel</Button></div>;
      return <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_TOPIC_JOB" jobId={row.original.id} jobName="Topic clustering job" onDelete={deleteJob} primary={row.original.clusters?.length ? <Link href={`/app/clients/${clientId}/matters/${matterId}/topic-jobs/${row.original.id}`}><FileSearch />View topics</Link> : undefined} />;
    } },
  ], [canDeleteJobs, cancelTopicJobMutation, clientId, deleteJob, matterId]);

  if (topicJobs.isPending || embeddingJobs.isPending || savedSearches.isPending) return <TableLoading />;
  if (topicJobs.error || embeddingJobs.error || savedSearches.error) return <QueryError message={topicJobs.error?.message ?? embeddingJobs.error?.message ?? savedSearches.error?.message} />;
  const embeddingsReady = embeddingJobs.data.some((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0);
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Topic clustering</h2><p className="text-sm text-muted-foreground">Discover topic proposals, review them, and explicitly approve the set before document metadata is changed.</p>{!embeddingsReady ? <p className="mt-1 text-xs text-muted-foreground">Complete an embedding job before clustering topics.</p> : null}</div><CreateTopicJobDialog savedSearches={savedSearches.data.filter((saved) => (saved.search.search_mode ?? "KEYWORD") === "KEYWORD")} definitions={definitions.data ?? []} onCreate={(values) => createTopicJobMutation.mutateAsync(values).then(() => undefined)} disabled={createTopicJobMutation.isPending || !embeddingsReady || topicJobs.data.some((job) => ACTIVE_TOPIC_STATUSES.includes(job.status))} /></div>
    <DataTable columns={columns} data={topicJobs.data} emptyMessage="No topic clustering jobs have been started for this matter." />
  </section>;
}

function TopicProgress({ job }: { job: MatterTopicJobRead }) {
  const percent = job.document_count ? Math.min(100, Math.round((job.processed_document_count / job.document_count) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  const planning = ["QUEUED", "SAMPLING", "CLUSTERING"].includes(job.status);
  const awaitingReview = job.status === "AWAITING_REVIEW";
  return <div className="min-w-36"><ProgressBar percent={planning ? 8 : awaitingReview ? 0 : percent} /><span className="text-xs tabular-nums text-muted-foreground">{planning ? job.status.toLowerCase() : awaitingReview ? "Review required" : `${job.processed_document_count.toLocaleString()} / ${job.document_count.toLocaleString()}`}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
}
