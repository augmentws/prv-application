"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, BarChart3, FileSearch, RotateCcw, Trash2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { AssignBatchCodingGroupsDialog } from "@/components/forms/assign-batch-coding-groups-dialog";
import { CreateReviewBatchDialog } from "@/components/forms/create-review-batch-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { QueryError, TableLoading } from "@/components/query-state";
import { SoftDeleteDialog } from "@/components/soft-delete-dialog";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SplitActionButton } from "@/components/ui/split-action-button";
import type { JobExecutionRead, MatterDefinitionAssessmentRead, MatterSavedSearchRead, MetadataGroupRead, ReviewBatchCreate, ReviewBatchRead, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export function BatchesSection() {
  const { matterId, permissionKeys } = useMatter();
  const queryClient = useQueryClient();
  const canDeleteBatches = permissionKeys.has("batch.delete");
  const groups = useQuery({ queryKey: ["metadata-groups", matterId], queryFn: () => coreApi<MetadataGroupRead[]>(`/v1/matters/${matterId}/metadata-groups`) });
  const reviewBatches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
    refetchInterval: (query) => query.state.data?.some((batch) => ["QUEUED", "BUILDING"].includes(batch.status)) ? 2000 : false,
  });
  const savedSearches = useQuery({ queryKey: ["matter-saved-searches", matterId], queryFn: () => coreApi<MatterSavedSearchRead[]>(`/v1/matters/${matterId}/saved-searches`) });
  const assessments = useQuery({ queryKey: ["definition-assessments", matterId], queryFn: () => coreApi<MatterDefinitionAssessmentRead[]>(`/v1/matters/${matterId}/definition-assessments`) });
  const tenantUsers = useQuery({ queryKey: ["matter-access-users", matterId, "batch.review"], queryFn: () => coreApi<UserRead[]>(`/v1/matters/${matterId}/access-users?permission_key=batch.review`) });

  const createReviewBatchMutation = useMutation({
    mutationFn: (values: ReviewBatchCreate) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (created) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current ? [created, ...current] : [created]);
      toast.success(`${created.name} was created.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch could not be created."),
  });
  const deleteReviewBatchMutation = useMutation({
    mutationFn: (batchId: string) => coreApi<void>(`/v1/matters/${matterId}/review-batches/${batchId}`, { method: "DELETE" }),
    onSuccess: (_, batchId) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current?.filter((batch) => batch.id !== batchId));
      toast.success("The review batch was deleted. Its history has been retained.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch could not be deleted."),
  });
  const cancelReviewBatchMutation = useMutation({
    mutationFn: (batchId: string) => coreApi<JobExecutionRead>(`/v1/jobs/REVIEW_BATCH_MATERIALIZATION/${batchId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] });
      toast.success("The review batch build was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch build could not be canceled."),
  });
  const retryReviewBatchMutation = useMutation({
    mutationFn: (batchId: string) => coreApi<JobExecutionRead>(`/v1/jobs/REVIEW_BATCH_MATERIALIZATION/${batchId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] });
      toast.success("The review batch build was queued again.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch build could not be retried."),
  });
  const assignReviewBatchMutation = useMutation({
    mutationFn: ({ batchId, userId }: { batchId: string; userId: string | null }) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}/assignment`, { method: "PUT", body: JSON.stringify({ assigned_user_id: userId }) }),
    onSuccess: (updated) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current?.map((batch) => batch.id === updated.id ? updated : batch));
      toast.success(updated.assigned_user ? `${updated.name} was assigned to ${updated.assigned_user.display_name}.` : `${updated.name} is now unassigned.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The batch assignment could not be changed."),
  });
  const assignBatchCodingGroupsMutation = useMutation({
    mutationFn: ({ batchId, codingGroupIds }: { batchId: string; codingGroupIds: string[] }) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}/coding-groups`, { method: "PUT", body: JSON.stringify({ coding_group_ids: codingGroupIds }) }),
    onSuccess: (updated) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current?.map((batch) => batch.id === updated.id ? updated : batch));
      toast.success(`Coding groups for ${updated.name} were updated.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The coding groups could not be updated."),
  });
  const createReviewBatch = useCallback((values: ReviewBatchCreate) => createReviewBatchMutation.mutateAsync(values).then(() => undefined), [createReviewBatchMutation]);
  const assignBatchCodingGroups = useCallback((batchId: string, codingGroupIds: string[]) => assignBatchCodingGroupsMutation.mutateAsync({ batchId, codingGroupIds }).then(() => undefined), [assignBatchCodingGroupsMutation]);

  const reviewBatchColumns = useMemo<ColumnDef<ReviewBatchRead>[]>(() => [
    { accessorKey: "name", header: "Batch", cell: ({ row }) => <div><p className="font-semibold">{row.original.name}</p>{row.original.description ? <p className="max-w-md truncate text-xs text-muted-foreground" title={row.original.description}>{row.original.description}</p> : null}</div> },
    { accessorKey: "selection_type", header: "Created from", cell: ({ row }) => <span className="text-muted-foreground">{batchSelectionLabel(row.original)}</span> },
    { accessorKey: "document_count", header: "Documents", cell: ({ row }) => <BatchMaterializationProgress batch={row.original} /> },
    { id: "coding", header: "Coding", cell: ({ row }) => <div className="flex items-center gap-2"><span className="text-muted-foreground">{row.original.coding_groups.length ? `${row.original.coding_groups.length} group${row.original.coding_groups.length === 1 ? "" : "s"}` : "No groups"}</span><AssignBatchCodingGroupsDialog batch={row.original} groups={groups.data ?? []} onSave={assignBatchCodingGroups} /></div> },
    { id: "assignee", header: "Assigned to", cell: ({ row }) => <Select value={row.original.assigned_user_id ?? "unassigned"} onValueChange={(value) => assignReviewBatchMutation.mutate({ batchId: row.original.id, userId: value === "unassigned" ? null : value })} disabled={assignReviewBatchMutation.isPending}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unassigned">Unassigned</SelectItem>{(tenantUsers.data ?? []).filter((user) => user.status === "ACTIVE").map((user) => <SelectItem key={user.id} value={user.id}>{user.display_name}</SelectItem>)}</SelectContent></Select> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div><StatusBadge status={row.original.status} />{row.original.execution ? <p className="mt-1 whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}{row.original.error_message ? <p className="mt-1 max-w-64 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : row.original.search_error_message ? <p className="mt-1 max-w-64 truncate text-xs text-destructive" title={row.original.search_error_message}>Search projection: {row.original.search_error_message}</p> : null}</div> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const actions = row.original.execution?.available_actions ?? [];
      if (actions.includes("CANCEL")) return <Button size="sm" variant="outline" disabled={cancelReviewBatchMutation.isPending} onClick={() => cancelReviewBatchMutation.mutate(row.original.id)}><Ban />Cancel</Button>;
      if (actions.includes("RETRY")) return <Button size="sm" variant="outline" disabled={retryReviewBatchMutation.isPending} onClick={() => retryReviewBatchMutation.mutate(row.original.id)}><RotateCcw />Retry</Button>;
      return row.original.status === "READY" ? <ReviewBatchActions matterId={matterId} batch={row.original} canDelete={canDeleteBatches} onDelete={(batchId) => deleteReviewBatchMutation.mutateAsync(batchId).then(() => undefined)} /> : row.original.status === "FAILED" || row.original.status === "CANCELED" ? <BatchDeleteAction batch={row.original} canDelete={canDeleteBatches} onDelete={(batchId) => deleteReviewBatchMutation.mutateAsync(batchId).then(() => undefined)} /> : null;
    } },
  ], [assignBatchCodingGroups, assignReviewBatchMutation, canDeleteBatches, cancelReviewBatchMutation, deleteReviewBatchMutation, groups.data, matterId, retryReviewBatchMutation, tenantUsers.data]);

  if (reviewBatches.isPending || groups.isPending || tenantUsers.isPending || savedSearches.isPending || assessments.isPending) return <TableLoading />;
  if (reviewBatches.error || groups.error || tenantUsers.error || savedSearches.error || assessments.error) return <QueryError message={reviewBatches.error?.message ?? groups.error?.message ?? tenantUsers.error?.message ?? savedSearches.error?.message ?? assessments.error?.message} />;
  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Review batches</h2><p className="text-sm text-muted-foreground">Freeze document sets for assignment, repeatable agent runs, and coding comparisons.</p></div><CreateReviewBatchDialog groups={groups.data} batches={reviewBatches.data} savedSearches={savedSearches.data} assessments={assessments.data} onCreate={createReviewBatch} /></div>
    <DataTable columns={reviewBatchColumns} data={reviewBatches.data} emptyMessage="No review batches have been created for this matter." />
  </div>;
}

function ReviewBatchActions({ matterId, batch, canDelete, onDelete }: { matterId: string; batch: ReviewBatchRead; canDelete: boolean; onDelete: (batchId: string) => Promise<void> }) {
  const [deleteOpen, setDeleteOpen] = useState(false);

  return <>
    <SplitActionButton
      label={batch.name}
      primary={<Link href={`/review/matters/${matterId}?batch=${batch.id}`}><FileSearch />Open review</Link>}
      actions={[
        {
          id: "statistics",
          label: "Statistics",
          icon: <BarChart3 aria-hidden="true" className="size-4" />,
          href: `/review/matters/${matterId}/batches/${batch.id}/statistics`,
          target: "_blank" as const,
        },
        ...(canDelete ? [{
          id: "delete",
          label: "Delete batch",
          icon: <Trash2 aria-hidden="true" className="size-4" />,
          destructive: true,
          onSelect: () => setDeleteOpen(true),
        }] : []),
      ]}
    />
    <SoftDeleteDialog open={deleteOpen} onOpenChange={setDeleteOpen} resourceType="batch" resourceName={batch.name} onDelete={() => onDelete(batch.id)} />
  </>;
}

function BatchDeleteAction({ batch, canDelete, onDelete }: { batch: ReviewBatchRead; canDelete: boolean; onDelete: (batchId: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  if (!canDelete) return null;
  return <>
    <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}><Trash2 />Delete batch</Button>
    <SoftDeleteDialog open={open} onOpenChange={setOpen} resourceType="batch" resourceName={batch.name} onDelete={() => onDelete(batch.id)} />
  </>;
}

function BatchMaterializationProgress({ batch }: { batch: ReviewBatchRead }) {
  const completed = batch.execution?.progress_completed ?? batch.document_count;
  const total = batch.execution?.progress_total;
  const active = batch.execution && ["QUEUED", "RUNNING", "WAITING"].includes(batch.execution.status);
  if (!active || total == null) {
    return <span className="tabular-nums">{batch.document_count.toLocaleString()}</span>;
  }
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total.toLocaleString()}</span></div>;
}

function batchSelectionLabel(batch: ReviewBatchRead) {
  if (batch.selection_type === "RANDOM_SAVED_SEARCH") {
    const name = batch.selection_definition.saved_search_name;
    return typeof name === "string" ? `${name} sample` : "Random saved-search sample";
  }
  if (batch.selection_type === "RANDOM_ASSESSMENT") {
    const name = batch.selection_definition.assessment_name;
    return typeof name === "string" ? `${name} candidate sample` : "Random assessment-candidate sample";
  }
  if (batch.selection_type === "DEFINITION_ASSESSMENT") {
    const name = batch.selection_definition.guidance_name;
    const revision = batch.selection_definition.guidance_revision;
    return typeof name === "string"
      ? `${name}${typeof revision === "number" ? ` · r${revision}` : ""}`
      : "Review Guidance assessment";
  }
  if (batch.selection_type === "CLASSIFICATION_PREDICTIONS") {
    const minimum = batch.selection_definition.minimum_confidence;
    const maximum = batch.selection_definition.maximum_confidence;
    const range = typeof minimum === "number"
      ? `${Math.round(minimum * 100)}%${typeof maximum === "number" ? `–${Math.round(maximum * 100)}%` : "+"}`
      : "selected";
    return `Classifier predictions · ${range}`;
  }
  return { ALL_MATTER: "All matter documents", SEARCH_QUERY: "Keyword search", RANDOM_MATTER: "Random matter sample", RANDOM_BATCH: "Random batch sample", RANDOM_ASSESSMENT: "Random assessment-candidate sample", DEFINITION_ASSESSMENT: "Matter Definition assessment" }[batch.selection_type];
}
