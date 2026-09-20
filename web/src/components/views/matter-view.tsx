"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Ban, BriefcaseBusiness, ChevronRight, Database, FileSearch, FileText, ListChecks, Play, Search, Sparkles, UsersRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CloneMatterDialog, type CloneMatterValues } from "@/components/forms/clone-matter-dialog";
import { CreateMetadataDialog } from "@/components/forms/create-metadata-dialog";
import { type CreateMetadataGroupValues } from "@/components/forms/create-metadata-group-dialog";
import { CreateReviewBatchDialog } from "@/components/forms/create-review-batch-dialog";
import { SaveMatterTemplateDialog, type SaveMatterTemplateValues } from "@/components/forms/save-matter-template-dialog";
import { CreateTopicJobDialog } from "@/components/forms/create-topic-job-dialog";
import { ReviewTopicProposalsDialog } from "@/components/forms/review-topic-proposals-dialog";
import { HelpLink } from "@/components/help-link";
import { MatterDefinitionPanel } from "@/components/matter-definition-panel";
import { MetadataGroupsPanel } from "@/components/metadata-groups-panel";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { SearchIndexPanel } from "@/components/search-index-panel";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { ClientRead, MatterDocumentImportRead, MatterEmbeddingJobRead, MatterOverviewCounts, MatterRead, MatterTemplateRead, MatterTopicApplyRequest, MatterTopicJobCreate, MatterTopicJobRead, MetadataDefinitionCreate, MetadataDefinitionRead, MetadataGroupRead, ReviewBatchCreate, ReviewBatchRead, SearchIndexGenerationRead, SearchProjectionOperationRead, SearchProjectionRetryResponse, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

type MatterTab = "overview" | "metadata" | "groups" | "definition" | "batches" | "jobs" | "search";

export function MatterView({ clientId, matterId, requestedTab, selectedJobId }: {
  clientId: string;
  matterId: string;
  requestedTab?: string;
  selectedJobId?: string;
}) {
  const tab: MatterTab = requestedTab === "overview" || requestedTab === "groups" || requestedTab === "definition" || requestedTab === "batches" || requestedTab === "jobs" || requestedTab === "search" ? requestedTab : "metadata";
  const router = useRouter();
  const queryClient = useQueryClient();
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const groups = useQuery({ queryKey: ["metadata-groups", matterId], queryFn: () => coreApi<MetadataGroupRead[]>(`/v1/matters/${matterId}/metadata-groups`) });
  const overviewCounts = useQuery({
    queryKey: ["matter-overview-counts", matterId],
    queryFn: () => coreApi<MatterOverviewCounts>(`/v1/matters/${matterId}/overview-counts`),
    enabled: tab === "overview" || tab === "search",
  });
  const jobs = useQuery({
    queryKey: ["matter-document-imports", matterId],
    queryFn: () => coreApi<MatterDocumentImportRead[]>(`/v1/matters/${matterId}/document-imports`),
    enabled: tab === "jobs",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const embeddingJobs = useQuery({
    queryKey: ["matter-embedding-jobs", matterId],
    queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`),
    enabled: tab === "jobs",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const topicJobs = useQuery({
    queryKey: ["matter-topic-jobs", matterId],
    queryFn: () => coreApi<MatterTopicJobRead[]>(`/v1/matters/${matterId}/topic-jobs`),
    enabled: tab === "jobs",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SAMPLING", "CLUSTERING", "PUBLISHING"].includes(job.status)) ? 2000 : false,
  });
  const reviewBatches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
    enabled: tab === "batches",
    refetchInterval: (query) => query.state.data?.some((batch) => ["QUEUED", "BUILDING"].includes(batch.status)) ? 2000 : false,
  });
  const tenantUsers = useQuery({
    queryKey: ["tenant-users", client.data?.tenant_id],
    queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${client.data!.tenant_id}/users`),
    enabled: tab === "batches" && Boolean(client.data?.tenant_id),
  });
  const searchIndexes = useQuery({
    queryKey: ["search-indexes", matterId],
    queryFn: () => coreApi<SearchIndexGenerationRead[]>(`/v1/matters/${matterId}/search-indexes`),
    enabled: tab === "search",
    refetchInterval: 3000,
  });
  const searchOperations = useQuery({
    queryKey: ["search-operations", matterId],
    queryFn: () => coreApi<SearchProjectionOperationRead[]>(`/v1/matters/${matterId}/search-operations?limit=500`),
    enabled: tab === "search",
    refetchInterval: (query) => query.state.data?.some((operation) => operation.status === "QUEUED" || operation.status === "RUNNING") ? 2000 : false,
  });
  const mutation = useMutation({
    mutationFn: (values: MetadataDefinitionCreate) => coreApi<MetadataDefinitionRead>(`/v1/matters/${matterId}/metadata-definitions`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (definition) => { void queryClient.invalidateQueries({ queryKey: ["metadata-definitions", matterId] }); toast.success(`${definition.display_name} was created.`); },
  });
  const createDefinition = useCallback((values: MetadataDefinitionCreate) => mutation.mutateAsync(values).then(() => undefined), [mutation]);
  const groupMutation = useMutation({
    mutationFn: (values: CreateMetadataGroupValues) => coreApi<MetadataGroupRead>(`/v1/matters/${matterId}/metadata-groups`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (group) => { void queryClient.invalidateQueries({ queryKey: ["metadata-groups", matterId] }); toast.success(`${group.display_name} was created.`); },
  });
  const visibilityMutation = useMutation({
    mutationFn: ({ group, surface, visible }: { group: MetadataGroupRead; surface: "TABLE" | "DOCUMENT"; visible: boolean }) => coreApi<MetadataGroupRead>(`/v1/matters/${matterId}/metadata-groups/${group.id}/visibility`, { method: "PUT", body: JSON.stringify({ surface, visible }) }),
    onSuccess: (updated) => queryClient.setQueryData<MetadataGroupRead[]>(["metadata-groups", matterId], (current) => current?.map((group) => group.id === updated.id ? updated : group)),
    onError: (error) => toast.error(error instanceof Error ? error.message : "Visibility could not be changed."),
  });
  const templateMutation = useMutation({
    mutationFn: (values: SaveMatterTemplateValues) => coreApi<MatterTemplateRead>(`/v1/matters/${matterId}/templates`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (template) => { void queryClient.invalidateQueries({ queryKey: ["matter-templates", clientId] }); toast.success(`${template.name} is ready to use.`); },
  });
  const cloneMutation = useMutation({
    mutationFn: (values: CloneMatterValues) => coreApi<MatterRead>(`/v1/clients/${clientId}/matters`, { method: "POST", body: JSON.stringify({ name: values.name, clone_from_matter_id: matterId }) }),
    onSuccess: (created) => { void queryClient.invalidateQueries({ queryKey: ["matters", clientId] }); toast.success(`${created.name} was created.`); router.push(`/app/clients/${clientId}/matters/${created.id}`); },
  });
  const cancelJobMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<MatterDocumentImportRead>(`/v1/matters/${matterId}/document-imports/${jobId}/cancel`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterDocumentImportRead[]>(["matter-document-imports", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      toast.success("The import job was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The import job could not be canceled."),
  });
  const createEmbeddingJobMutation = useMutation({
    mutationFn: () => coreApi<MatterEmbeddingJobRead>(`/v1/matters/${matterId}/embedding-jobs`, { method: "POST" }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterEmbeddingJobRead[]>(["matter-embedding-jobs", matterId], (current) => current ? [created, ...current] : [created]);
      toast.success("The embedding job was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The embedding job could not be started."),
  });
  const cancelEmbeddingJobMutation = useMutation({
    mutationFn: (jobId: string) => coreApi<MatterEmbeddingJobRead>(`/v1/matters/${matterId}/embedding-jobs/${jobId}/cancel`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterEmbeddingJobRead[]>(["matter-embedding-jobs", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      toast.success("The embedding job was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The embedding job could not be canceled."),
  });
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
  const applyTopicJobMutation = useMutation({
    mutationFn: ({ jobId, payload }: { jobId: string; payload: MatterTopicApplyRequest }) => coreApi<MatterTopicJobRead>(`/v1/matters/${matterId}/topic-jobs/${jobId}/apply`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (updated) => {
      queryClient.setQueryData<MatterTopicJobRead[]>(["matter-topic-jobs", matterId], (current) => current?.map((job) => job.id === updated.id ? updated : job));
      void queryClient.invalidateQueries({ queryKey: ["metadata-definitions", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["metadata-groups", matterId] });
      toast.success("The approved topics are being applied to the matter.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The approved topics could not be applied."),
  });
  const rebuildSearchMutation = useMutation({
    mutationFn: () => coreApi<SearchProjectionOperationRead>(`/v1/matters/${matterId}/search-indexes/rebuild`, { method: "POST" }),
    onSuccess: (operation) => {
      queryClient.setQueryData<SearchProjectionOperationRead[]>(["search-operations", matterId], (current) => current ? [operation, ...current.filter((item) => item.id !== operation.id)] : [operation]);
      void queryClient.invalidateQueries({ queryKey: ["search-indexes", matterId] });
      toast.success("The search index rebuild was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The search index rebuild could not be requested."),
  });
  const confirmReindexMutation = useMutation({
    mutationFn: (operationId: string) => coreApi<SearchProjectionOperationRead>(`/v1/matters/${matterId}/search-operations/${operationId}/confirm-reindex`, { method: "POST" }),
    onSuccess: (operation) => {
      queryClient.setQueryData<SearchProjectionOperationRead[]>(["search-operations", matterId], (current) => current?.map((item) => item.id === operation.id ? operation : item));
      toast.success("The full search reindex was confirmed and queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The search reindex could not be confirmed."),
  });
  const retryFailedSearchMutation = useMutation({
    mutationFn: () => coreApi<SearchProjectionRetryResponse>(`/v1/matters/${matterId}/search-operations/retry-failed`, { method: "POST" }),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["search-operations", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["search-indexes", matterId] });
      toast.success(result.requeued_operation_count
        ? `${result.requeued_operation_count.toLocaleString()} failed search ${result.requeued_operation_count === 1 ? "job was" : "jobs were"} requeued for ${result.requeued_document_count.toLocaleString()} documents.`
        : "There are no failed document-update jobs to requeue.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The failed search jobs could not be requeued."),
  });
  const createReviewBatchMutation = useMutation({
    mutationFn: (values: ReviewBatchCreate) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (created) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current ? [created, ...current] : [created]);
      toast.success(`${created.name} was created.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch could not be created."),
  });
  const assignReviewBatchMutation = useMutation({
    mutationFn: ({ batchId, userId }: { batchId: string; userId: string | null }) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}/assignment`, { method: "PUT", body: JSON.stringify({ assigned_user_id: userId }) }),
    onSuccess: (updated) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current?.map((batch) => batch.id === updated.id ? updated : batch));
      toast.success(updated.assigned_user ? `${updated.name} was assigned to ${updated.assigned_user.display_name}.` : `${updated.name} is now unassigned.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The batch assignment could not be changed."),
  });
  const createGroup = useCallback((values: CreateMetadataGroupValues) => groupMutation.mutateAsync(values).then(() => undefined), [groupMutation]);
  const changeVisibility = useCallback((group: MetadataGroupRead, surface: "TABLE" | "DOCUMENT", visible: boolean) => visibilityMutation.mutateAsync({ group, surface, visible }).then(() => undefined), [visibilityMutation]);
  const saveTemplate = useCallback((values: SaveMatterTemplateValues) => templateMutation.mutateAsync(values).then(() => undefined), [templateMutation]);
  const cloneMatter = useCallback((values: CloneMatterValues) => cloneMutation.mutateAsync(values).then(() => undefined), [cloneMutation]);
  const createReviewBatch = useCallback((values: ReviewBatchCreate) => createReviewBatchMutation.mutateAsync(values).then(() => undefined), [createReviewBatchMutation]);
  const applyTopics = useCallback((jobId: string, payload: MatterTopicApplyRequest) => applyTopicJobMutation.mutateAsync({ jobId, payload }).then(() => undefined), [applyTopicJobMutation]);
  const columns = useMemo<ColumnDef<MetadataDefinitionRead>[]>(() => [
    { accessorKey: "display_name", header: "Field", cell: ({ row }) => <div><p className="font-semibold">{row.original.display_name}</p><code className="text-xs text-muted-foreground">{row.original.key}</code></div> },
    { accessorKey: "type", header: "Type", cell: ({ row }) => <Badge>{row.original.type.toLowerCase().replace("_", " ")}</Badge> },
    { accessorKey: "value_source", header: "Source", cell: ({ row }) => <Badge variant="outline">{row.original.value_source.toLowerCase()}</Badge> },
    { accessorKey: "cardinality", header: "Values", cell: ({ row }) => <span className="text-muted-foreground">{row.original.cardinality.toLowerCase()}</span> },
    { id: "behavior", header: "Behavior", cell: ({ row }) => <div className="flex flex-wrap gap-1">{row.original.searchable ? <Badge variant="outline">search</Badge> : null}{row.original.facetable ? <Badge variant="outline">filter</Badge> : null}{row.original.ai_assignable ? <Badge variant="accent">agent</Badge> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
  ], []);

  const openTab = useCallback((nextTab: MatterTab, jobId?: string) => {
    const params = new URLSearchParams({ tab: nextTab });
    if (jobId) params.set("job", jobId);
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);

  const jobColumns = useMemo<ColumnDef<MatterDocumentImportRead>[]>(() => [
    { accessorKey: "selection_summary", header: "Selection", cell: ({ row }) => <button type="button" className="max-w-md text-left font-semibold hover:text-primary" onClick={() => openTab("jobs", row.original.id)}>{row.original.selection_summary}</button> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "progress", header: "Progress", cell: ({ row }) => <JobProgress job={row.original} /> },
    { accessorKey: "added_count", header: "Added", cell: ({ row }) => <span className="tabular-nums">{row.original.added_count.toLocaleString()}</span> },
    { accessorKey: "duplicate_count", header: "Already present", cell: ({ row }) => <span className="tabular-nums text-muted-foreground">{row.original.duplicate_count.toLocaleString()}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
  ], [openTab]);
  const embeddingJobColumns = useMemo<ColumnDef<MatterEmbeddingJobRead>[]>(() => [
    { accessorKey: "embedding_model", header: "Model", cell: ({ row }) => <div><p className="max-w-64 truncate font-semibold">{row.original.embedding_model}</p><p className="text-xs text-muted-foreground">{row.original.embedding_dimensions.toLocaleString()} dimensions</p>{row.original.error_message ? <p className="mt-1 max-w-80 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "progress", header: "Progress", cell: ({ row }) => <EmbeddingProgress job={row.original} /> },
    { accessorKey: "embedded_count", header: "Generated", cell: ({ row }) => <span className="tabular-nums">{row.original.embedded_count.toLocaleString()}</span> },
    { accessorKey: "skipped_count", header: "Skipped", cell: ({ row }) => <span className="tabular-nums text-muted-foreground">{row.original.skipped_count.toLocaleString()}</span> },
    { accessorKey: "chunk_count", header: "Chunks", cell: ({ row }) => <span className="tabular-nums">{row.original.chunk_count.toLocaleString()}</span> },
    { id: "provider_usage", header: "Token usage", cell: ({ row }) => <EmbeddingTokenUsage job={row.original} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => ["QUEUED", "PLANNING", "RUNNING"].includes(row.original.status) ? <Button size="sm" variant="outline" onClick={() => cancelEmbeddingJobMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null },
  ], [cancelEmbeddingJobMutation]);
  const topicJobColumns = useMemo<ColumnDef<MatterTopicJobRead>[]>(() => [
    { accessorKey: "operating_mode", header: "Mode", cell: ({ row }) => <div><p className="font-semibold">{row.original.operating_mode === "AUTO" ? "Automatic" : `${row.original.requested_topic_count} topics`}</p><p className="text-xs text-muted-foreground">{row.original.sample_size.toLocaleString()} chunk sample · {row.original.assignment_mode.toLowerCase()}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "progress", header: "Progress", cell: ({ row }) => <TopicProgress job={row.original} /> },
    { accessorKey: "topic_count", header: "Topics", cell: ({ row }) => <div><span className="tabular-nums">{row.original.topic_count.toLocaleString()}</span>{row.original.clusters?.length ? <p className="max-w-64 truncate text-xs text-muted-foreground" title={row.original.clusters.map((cluster) => cluster.name).join(", ")}>{row.original.clusters.slice(0, 3).map((cluster) => cluster.name).join(", ")}</p> : null}</div> },
    { accessorKey: "assigned_document_count", header: "Assigned", cell: ({ row }) => <span className="tabular-nums">{row.original.assigned_document_count.toLocaleString()}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => <div className="flex justify-end gap-2">{row.original.status === "AWAITING_REVIEW" ? <ReviewTopicProposalsDialog job={row.original} onApply={applyTopics} /> : null}{["QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"].includes(row.original.status) ? <Button size="sm" variant="outline" onClick={() => cancelTopicJobMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null}</div> },
  ], [applyTopics, cancelTopicJobMutation]);
  const reviewBatchColumns = useMemo<ColumnDef<ReviewBatchRead>[]>(() => [
    { accessorKey: "name", header: "Batch", cell: ({ row }) => <div><p className="font-semibold">{row.original.name}</p>{row.original.description ? <p className="max-w-md truncate text-xs text-muted-foreground" title={row.original.description}>{row.original.description}</p> : null}</div> },
    { accessorKey: "selection_type", header: "Created from", cell: ({ row }) => <span className="text-muted-foreground">{batchSelectionLabel(row.original.selection_type)}</span> },
    { accessorKey: "document_count", header: "Documents", cell: ({ row }) => ["QUEUED", "BUILDING"].includes(row.original.status) ? <span className="text-muted-foreground">Building…</span> : <span className="tabular-nums">{row.original.document_count.toLocaleString()}</span> },
    { id: "coding", header: "Coding", cell: ({ row }) => <span className="text-muted-foreground">{row.original.coding_groups.length ? `${row.original.coding_groups.length} group${row.original.coding_groups.length === 1 ? "" : "s"}` : "No groups"}</span> },
    { id: "assignee", header: "Assigned to", cell: ({ row }) => <Select value={row.original.assigned_user_id ?? "unassigned"} onValueChange={(value) => assignReviewBatchMutation.mutate({ batchId: row.original.id, userId: value === "unassigned" ? null : value })} disabled={assignReviewBatchMutation.isPending}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unassigned">Unassigned</SelectItem>{(tenantUsers.data ?? []).filter((user) => user.status === "ACTIVE").map((user) => <SelectItem key={user.id} value={user.id}>{user.display_name}</SelectItem>)}</SelectContent></Select> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div><StatusBadge status={row.original.status} />{row.original.error_message ? <p className="mt-1 max-w-64 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => row.original.status === "READY" ? <Button asChild size="sm"><Link href={`/review/matters/${matterId}?batch=${row.original.id}`}><FileSearch />Open review</Link></Button> : null },
  ], [assignReviewBatchMutation, matterId, tenantUsers.data]);

  if (client.isPending || matter.isPending) return <TableLoading />;
  if (client.error || matter.error) return <QueryError message={client.error?.message ?? matter.error?.message} />;

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex flex-wrap items-center gap-1 text-sm text-muted-foreground"><Link href="/app/clients" className="hover:text-foreground">Clients</Link><ChevronRight className="size-4" /><Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{matter.data.name}</span></nav>
      <PageHeader eyebrow={client.data.name} title={matter.data.name} description="Configure the matter-level structure used by reviewers and processing agents." actions={<div className="flex flex-wrap items-center gap-2"><Button asChild><Link href={`/review/matters/${matterId}`}><FileSearch />Search & Review</Link></Button><HelpLink topic={tab === "jobs" ? "matterJobs" : tab === "search" ? "matterSearch" : tab === "definition" ? "matterDefinition" : "metadata"} /><CloneMatterDialog sourceName={matter.data.name} onClone={cloneMatter} /><SaveMatterTemplateDialog onCreate={saveTemplate} />{tab === "metadata" ? <CreateMetadataDialog onCreate={createDefinition} /> : null}</div>} />
      <div className="mb-6 flex gap-1 border-b" role="tablist" aria-label="Matter sections">
        <button role="tab" aria-selected={tab === "overview"} onClick={() => openTab("overview")} className={tabClass(tab === "overview")}>Overview</button>
        <button role="tab" aria-selected={tab === "metadata"} onClick={() => openTab("metadata")} className={tabClass(tab === "metadata")}>Metadata definitions</button>
        <button role="tab" aria-selected={tab === "groups"} onClick={() => openTab("groups")} className={tabClass(tab === "groups")}>Metadata groups</button>
        <button role="tab" aria-selected={tab === "definition"} onClick={() => openTab("definition")} className={tabClass(tab === "definition")}>Matter definition</button>
        <button role="tab" aria-selected={tab === "batches"} onClick={() => openTab("batches")} className={tabClass(tab === "batches")}>Batches</button>
        <button role="tab" aria-selected={tab === "jobs"} onClick={() => openTab("jobs")} className={tabClass(tab === "jobs")}>Jobs</button>
        <button role="tab" aria-selected={tab === "search"} onClick={() => openTab("search")} className={tabClass(tab === "search")}>Search index</button>
      </div>
      {tab === "overview" ? overviewCounts.error ? <QueryError message={overviewCounts.error.message} /> : <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5"><Summary icon={FileText} label="Documents" value={overviewCounts.data?.document_count ?? "—"} /><Summary icon={UsersRound} label="Custodians" value={overviewCounts.data?.custodian_count ?? "—"} /><Summary icon={Database} label="Metadata fields" value={definitions.data?.length ?? 0} /><Summary icon={Search} label="Searchable fields" value={definitions.data?.filter((item) => item.searchable).length ?? 0} /><Summary icon={Sparkles} label="Agent assignable" value={definitions.data?.filter((item) => item.ai_assignable).length ?? 0} /></div>
        : tab === "metadata" ? definitions.isPending ? <TableLoading /> : definitions.error ? <QueryError message={definitions.error.message} /> : <DataTable columns={columns} data={definitions.data} emptyMessage="No metadata fields yet. Add the first field definition for this matter." />
        : tab === "groups" ? definitions.isPending || groups.isPending ? <TableLoading /> : definitions.error || groups.error ? <QueryError message={definitions.error?.message ?? groups.error?.message} /> : <MetadataGroupsPanel definitions={definitions.data} groups={groups.data} onCreate={createGroup} onVisibilityChange={changeVisibility} />
        : tab === "definition" ? <MatterDefinitionPanel matterId={matterId} />
        : tab === "batches" ? reviewBatches.isPending || groups.isPending || tenantUsers.isPending ? <TableLoading /> : reviewBatches.error || groups.error || tenantUsers.error ? <QueryError message={reviewBatches.error?.message ?? groups.error?.message ?? tenantUsers.error?.message} /> : <div className="space-y-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Review batches</h2><p className="text-sm text-muted-foreground">Freeze document sets for assignment, repeatable agent runs, and coding comparisons.</p></div><CreateReviewBatchDialog groups={groups.data} batches={reviewBatches.data} onCreate={createReviewBatch} /></div><DataTable columns={reviewBatchColumns} data={reviewBatches.data} emptyMessage="No review batches have been created for this matter." /></div>
        : tab === "jobs" ? jobs.isPending || embeddingJobs.isPending || topicJobs.isPending ? <TableLoading /> : jobs.error || embeddingJobs.error || topicJobs.error ? <QueryError message={jobs.error?.message ?? embeddingJobs.error?.message ?? topicJobs.error?.message} /> : <div className="space-y-8">
          <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Document embeddings</h2><p className="text-sm text-muted-foreground">Create sentence-aware chunks and vectors for every document in this matter.</p></div><Button onClick={() => createEmbeddingJobMutation.mutate()} disabled={createEmbeddingJobMutation.isPending || embeddingJobs.data.some((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status))}><Play />Generate embeddings</Button></div>
            <DataTable columns={embeddingJobColumns} data={embeddingJobs.data} emptyMessage="No embedding jobs have been started for this matter." />
          </section>
          <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Topic clustering</h2><p className="text-sm text-muted-foreground">Discover topic proposals, review them, and explicitly approve the set before document metadata is changed.</p>{!embeddingJobs.data.some((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0) ? <p className="mt-1 text-xs text-muted-foreground">Complete an embedding job before clustering topics.</p> : null}</div><CreateTopicJobDialog onCreate={(values) => createTopicJobMutation.mutateAsync(values).then(() => undefined)} disabled={createTopicJobMutation.isPending || !embeddingJobs.data.some((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0) || topicJobs.data.some((job) => ["QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"].includes(job.status))} /></div>
            <DataTable columns={topicJobColumns} data={topicJobs.data} emptyMessage="No topic clustering jobs have been started for this matter." />
          </section>
          <section className="space-y-4">
            <div><h2 className="text-lg font-semibold">Document imports</h2><p className="text-sm text-muted-foreground">Jobs that add collection documents to this matter.</p></div>
            {selectedJobId ? <JobDetail job={jobs.data.find((job) => job.id === selectedJobId)} onCancel={(jobId) => cancelJobMutation.mutate(jobId)} /> : null}
            <DataTable columns={jobColumns} data={jobs.data} emptyMessage="No document import jobs have been started for this matter." />
          </section>
        </div>
        : searchIndexes.isPending || searchOperations.isPending || overviewCounts.isPending ? <TableLoading /> : searchIndexes.error || searchOperations.error || overviewCounts.error ? <QueryError message={searchIndexes.error?.message ?? searchOperations.error?.message ?? overviewCounts.error?.message} /> : <SearchIndexPanel coreDocumentCount={overviewCounts.data.document_count} indexes={searchIndexes.data} operations={searchOperations.data} onRebuild={() => rebuildSearchMutation.mutateAsync().then(() => undefined)} rebuilding={rebuildSearchMutation.isPending} onConfirmReindex={(operationId) => confirmReindexMutation.mutateAsync(operationId).then(() => undefined)} confirmingReindex={confirmReindexMutation.isPending} onRetryFailed={() => retryFailedSearchMutation.mutateAsync().then(() => undefined)} retryingFailed={retryFailedSearchMutation.isPending} />}
    </>
  );
}

function tabClass(active: boolean) {
  return `relative px-4 py-3 text-sm font-semibold outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${active ? "text-primary after:absolute after:inset-x-1 after:bottom-0 after:h-0.5 after:bg-accent" : "text-muted-foreground hover:text-foreground"}`;
}

function batchSelectionLabel(value: ReviewBatchRead["selection_type"]) {
  return { ALL_MATTER: "All matter documents", SEARCH_QUERY: "Keyword search", RANDOM_MATTER: "Random matter sample", RANDOM_BATCH: "Random batch sample", DEFINITION_ASSESSMENT: "Matter Definition assessment" }[value];
}

function Summary({ icon: Icon, label, value }: { icon: typeof ListChecks; label: string; value: number | string }) {
  return <Card><CardHeader><span className="mb-2 grid size-9 place-items-center rounded-lg bg-secondary text-primary"><Icon className="size-4" /></span><CardTitle className="text-sm text-muted-foreground">{label}</CardTitle></CardHeader><CardContent><p className="text-3xl font-semibold tabular-nums">{value}</p></CardContent></Card>;
}

function JobProgress({ job }: { job: MatterDocumentImportRead }) {
  const percent = job.matched_count ? Math.min(100, Math.round((job.processed_count / job.matched_count) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{job.processed_count.toLocaleString()} / {job.matched_count.toLocaleString()}</span></div>;
}

function EmbeddingProgress({ job }: { job: MatterEmbeddingJobRead }) {
  const percent = job.total_count ? Math.min(100, Math.round((job.processed_count / job.total_count) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{job.processed_count.toLocaleString()} / {job.total_count.toLocaleString()}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
}

function EmbeddingTokenUsage({ job }: { job: MatterEmbeddingJobRead }) {
  if (!job.provider_request_count && !job.total_tokens) {
    return <span className="text-sm text-muted-foreground">No external usage</span>;
  }
  return <div className="min-w-40"><p className="font-semibold tabular-nums">{job.total_tokens.toLocaleString()} tokens</p><p className="text-xs tabular-nums text-muted-foreground">{job.input_tokens.toLocaleString()} in · {job.output_tokens.toLocaleString()} out</p><p className="text-xs tabular-nums text-muted-foreground">{job.provider_request_count.toLocaleString()} {job.provider_request_count === 1 ? "request" : "requests"}</p></div>;
}

function TopicProgress({ job }: { job: MatterTopicJobRead }) {
  const percent = job.document_count ? Math.min(100, Math.round((job.processed_document_count / job.document_count) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  const planning = ["QUEUED", "SAMPLING", "CLUSTERING"].includes(job.status);
  const awaitingReview = job.status === "AWAITING_REVIEW";
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${planning ? 8 : awaitingReview ? 0 : percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{planning ? job.status.toLowerCase() : awaitingReview ? "Review required" : `${job.processed_document_count.toLocaleString()} / ${job.document_count.toLocaleString()}`}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
}

function JobDetail({ job, onCancel }: { job?: MatterDocumentImportRead; onCancel: (jobId: string) => void }) {
  if (!job) return <Card className="p-5 text-sm text-muted-foreground">The selected job is not available in the recent job history.</Card>;
  const active = ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status);
  return <Card className="p-5"><div className="flex flex-wrap items-start justify-between gap-4"><div><div className="mb-2 flex items-center gap-2"><BriefcaseBusiness className="size-4 text-primary" /><StatusBadge status={job.status} /></div><h2 className="text-lg font-semibold">{job.selection_summary}</h2><p className="mt-1 text-sm text-muted-foreground">Created {formatDate(job.created_at)} · {job.batch_count.toLocaleString()} {job.batch_count === 1 ? "batch" : "batches"}</p></div>{active ? <Button variant="outline" onClick={() => onCancel(job.id)}><Ban />Cancel job</Button> : null}</div><dl className="mt-5 grid gap-3 text-sm sm:grid-cols-4"><JobMetric label="Matched" value={job.matched_count} /><JobMetric label="Processed" value={job.processed_count} /><JobMetric label="Added" value={job.added_count} /><JobMetric label="Already present" value={job.duplicate_count} /></dl>{job.error_message ? <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{job.error_message}</p> : null}</Card>;
}

function JobMetric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-lg bg-muted/50 p-3"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{value.toLocaleString()}</dd></div>;
}
