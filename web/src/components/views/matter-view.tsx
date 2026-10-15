"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, Ban, BarChart3, BrainCircuit, BriefcaseBusiness, CheckCircle2, ChevronRight, Database, Download, FileDown, FileSearch, FileText, ListChecks, Play, RotateCcw, Search, Sparkles, Tags, Trash2, UsersRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ReactElement, useCallback, useMemo, useState, useSyncExternalStore } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { AnalysisTasksPanel } from "@/components/analysis-tasks-panel";
import { ClassificationAnalysisJobDetail } from "@/components/classification-analysis-job-detail";
import { ClassificationPanel } from "@/components/classification-panel";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { MetadataDefinitionEditor } from "@/components/metadata-definition-editor";
import { CloneMatterDialog, type CloneMatterValues } from "@/components/forms/clone-matter-dialog";
import { AssignBatchCodingGroupsDialog } from "@/components/forms/assign-batch-coding-groups-dialog";
import { CreateMetadataDialog } from "@/components/forms/create-metadata-dialog";
import { type CreateMetadataGroupValues } from "@/components/forms/create-metadata-group-dialog";
import { CreateReviewBatchDialog } from "@/components/forms/create-review-batch-dialog";
import { SaveMatterTemplateDialog, type SaveMatterTemplateValues } from "@/components/forms/save-matter-template-dialog";
import { CreateTopicJobDialog } from "@/components/forms/create-topic-job-dialog";
import { HelpLink } from "@/components/help-link";
import { JobFailedDocumentsDialog } from "@/components/job-failed-documents-dialog";
import { MatterDefinitionPanel } from "@/components/matter-definition-panel";
import { MatterTokenUsagePanel } from "@/components/matter-token-usage-panel";
import { MetadataGroupsPanel } from "@/components/metadata-groups-panel";
import { QueryError, TableLoading } from "@/components/query-state";
import { SearchIndexPanel } from "@/components/search-index-panel";
import { SoftDeleteDialog } from "@/components/soft-delete-dialog";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SplitActionButton } from "@/components/ui/split-action-button";
import type { ClientRead, EffectivePermissionsRead, JobExecutionRead, MatterAnalysisTaskRead, MatterAnalysisTaskRunRead, MatterBulkTagJobRead, MatterDefinitionAssessmentRead, MatterDocumentImportRead, MatterEmbeddingJobRead, MatterOverviewCounts, MatterProviderUsageReportRead, MatterQuestionAnsweringJobRead, MatterRead, MatterSavedSearchRead, MatterSearchExportRead, MatterTemplateRead, MatterTopicJobCreate, MatterTopicJobRead, MetadataDefinitionCreate, MetadataDefinitionRead, MetadataGroupRead, ReviewBatchCreate, ReviewBatchRead, SearchIndexGenerationRead, SearchProjectionOperationRead, SearchProjectionRetryResponse, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { bulkTagDisplayStatus, bulkTagIsWaiting } from "@/lib/bulk-tag-jobs";
import { formatDate } from "@/lib/format";

type MatterTab = "overview" | "metadata" | "definition" | "analysis" | "batches" | "classification" | "jobs" | "usage" | "search";
type MetadataTab = "definitions" | "groups";
type MatterJobType = "BULK_TAGGING" | "DOCUMENT_EMBEDDINGS" | "TOPIC_CLUSTERING" | "DOCUMENT_IMPORTS" | "QUESTION_ANSWERING" | "CLASSIFICATION_ANALYSIS" | "SEARCH_EXPORTS";
type ClassificationAnalysisJobRow = MatterAnalysisTaskRunRead & { task_name: string };

const MATTER_JOB_TYPE_STORAGE_KEY = "priv-view:matter-jobs:selected-type";
const MATTER_JOB_TYPE_CHANGE_EVENT = "priv-view:matter-jobs:selected-type-change";
const MATTER_JOB_TYPES: MatterJobType[] = ["BULK_TAGGING", "DOCUMENT_EMBEDDINGS", "TOPIC_CLUSTERING", "DOCUMENT_IMPORTS", "QUESTION_ANSWERING", "CLASSIFICATION_ANALYSIS", "SEARCH_EXPORTS"];

function readStoredMatterJobType(): MatterJobType {
  const stored = window.localStorage.getItem(MATTER_JOB_TYPE_STORAGE_KEY);
  return stored && MATTER_JOB_TYPES.includes(stored as MatterJobType) ? stored as MatterJobType : "BULK_TAGGING";
}

function subscribeToMatterJobType(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(MATTER_JOB_TYPE_CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(MATTER_JOB_TYPE_CHANGE_EVENT, callback);
  };
}

export function MatterView({ clientId, matterId, requestedTab, requestedMetadataTab, selectedJobId, requestedJobType }: {
  clientId: string;
  matterId: string;
  requestedTab?: string;
  requestedMetadataTab?: string;
  selectedJobId?: string;
  requestedJobType?: string;
}) {
  const tab: MatterTab = requestedTab === "overview" || requestedTab === "definition" || requestedTab === "analysis" || requestedTab === "batches" || requestedTab === "classification" || requestedTab === "jobs" || requestedTab === "usage" || requestedTab === "search" ? requestedTab : "metadata";
  const metadataTab: MetadataTab = requestedTab === "groups" || requestedMetadataTab === "groups" ? "groups" : "definitions";
  const router = useRouter();
  const queryClient = useQueryClient();
  const storedJobType = useSyncExternalStore(subscribeToMatterJobType, readStoredMatterJobType, () => "BULK_TAGGING");
  const explicitJobType = requestedJobType && MATTER_JOB_TYPES.includes(requestedJobType as MatterJobType)
    ? requestedJobType as MatterJobType
    : undefined;
  const jobType = explicitJobType ?? (selectedJobId ? "DOCUMENT_IMPORTS" : storedJobType);
  const [usageOffset, setUsageOffset] = useState(0);
  const [selectedClassificationJobId, setSelectedClassificationJobId] = useState(
    explicitJobType === "CLASSIFICATION_ANALYSIS" ? selectedJobId : undefined,
  );

  const changeJobType = useCallback((value: string) => {
    const next = value as MatterJobType;
    window.localStorage.setItem(MATTER_JOB_TYPE_STORAGE_KEY, next);
    window.dispatchEvent(new Event(MATTER_JOB_TYPE_CHANGE_EVENT));
    const params = new URLSearchParams({ tab: "jobs", job_type: next });
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);
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
    enabled: tab === "jobs" || tab === "classification",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const topicJobs = useQuery({
    queryKey: ["matter-topic-jobs", matterId],
    queryFn: () => coreApi<MatterTopicJobRead[]>(`/v1/matters/${matterId}/topic-jobs`),
    enabled: tab === "jobs",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SAMPLING", "CLUSTERING", "PUBLISHING"].includes(job.status)) ? 2000 : false,
  });
  const savedSearches = useQuery({
    queryKey: ["matter-saved-searches", matterId],
    queryFn: () => coreApi<MatterSavedSearchRead[]>(`/v1/matters/${matterId}/saved-searches`),
    enabled: tab === "jobs" || tab === "batches",
  });
  const assessments = useQuery({
    queryKey: ["definition-assessments", matterId],
    queryFn: () => coreApi<MatterDefinitionAssessmentRead[]>(`/v1/matters/${matterId}/definition-assessments`),
    enabled: tab === "batches",
  });
  const bulkTagJobs = useQuery({
    queryKey: ["matter-bulk-tag-jobs", matterId],
    queryFn: () => coreApi<MatterBulkTagJobRead[]>(`/v1/matters/${matterId}/bulk-tag-jobs?limit=100`),
    enabled: tab === "jobs",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const classificationTasks = useQuery({
    queryKey: ["matter-classification-analysis-tasks", matterId],
    queryFn: () => coreApi<MatterAnalysisTaskRead[]>(`/v1/matters/${matterId}/analysis-tasks`),
    enabled: tab === "jobs" && jobType === "CLASSIFICATION_ANALYSIS",
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
    enabled: tab === "jobs" && jobType === "CLASSIFICATION_ANALYSIS" && classificationTasks.isSuccess,
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 1500 : false,
  });
  const questionAnsweringJobs = useQuery({
    queryKey: ["matter-question-answering-jobs", matterId],
    queryFn: () => coreApi<MatterQuestionAnsweringJobRead[]>(`/v1/matters/${matterId}/analysis-tasks/question-answering-jobs`),
    enabled: tab === "jobs" && jobType === "QUESTION_ANSWERING",
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 1500 : false,
  });
  const searchExportJobs = useQuery({
    queryKey: ["matter-search-export-jobs", matterId],
    queryFn: () => coreApi<MatterSearchExportRead[]>(`/v1/matters/${matterId}/search-exports?limit=500`),
    enabled: tab === "jobs" && jobType === "SEARCH_EXPORTS",
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "RUNNING"].includes(job.status)) ? 1500 : false,
  });
  const reviewBatches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
    enabled: tab === "batches" || tab === "classification",
    refetchInterval: (query) => query.state.data?.some((batch) => ["QUEUED", "BUILDING"].includes(batch.status)) ? 2000 : false,
  });
  const providerUsage = useQuery({
    queryKey: ["matter-provider-usage", matterId, usageOffset],
    queryFn: () => coreApi<MatterProviderUsageReportRead>(`/v1/matters/${matterId}/provider-usage?offset=${usageOffset}&limit=100`),
    enabled: tab === "usage",
    placeholderData: (previous) => previous,
  });
  const tenantUsers = useQuery({
    queryKey: ["matter-access-users", matterId, "batch.review"],
    queryFn: () => coreApi<UserRead[]>(`/v1/matters/${matterId}/access-users?permission_key=batch.review`),
    enabled: tab === "batches",
  });
  const effectivePermissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
    enabled: tab === "batches" || tab === "jobs",
  });
  const permissionKeys = new Set(effectivePermissions.data?.permission_keys ?? []);
  const canDeleteBatches = permissionKeys.has("batch.delete");
  const canDeleteJobs = permissionKeys.has("job.delete");
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
  const retryableSearchOperations = useQuery({
    queryKey: ["retryable-search-operations", matterId],
    queryFn: () => coreApi<SearchProjectionRetryResponse>(`/v1/matters/${matterId}/search-operations/retryable`),
    enabled: tab === "search",
    refetchInterval: 3000,
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
  const cancelQuestionAnsweringMutation = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/ANALYSIS_TASK_BATCH_RUN/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["matter-question-answering-jobs", matterId] });
      toast.success("The question-answering run was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The question-answering run could not be canceled."),
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
      void queryClient.invalidateQueries({ queryKey: ["retryable-search-operations", matterId] });
      toast.success(result.requeued_operation_count
        ? `${result.requeued_operation_count.toLocaleString()} failed or interrupted search ${result.requeued_operation_count === 1 ? "job was" : "jobs were"} requeued for ${result.requeued_document_count.toLocaleString()} documents.`
        : "There are no failed or interrupted document-update jobs to requeue.");
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
  const deleteReviewBatchMutation = useMutation({
    mutationFn: (batchId: string) => coreApi<void>(`/v1/matters/${matterId}/review-batches/${batchId}`, { method: "DELETE" }),
    onSuccess: (_, batchId) => {
      queryClient.setQueryData<ReviewBatchRead[]>(["review-batches", matterId], (current) => current?.filter((batch) => batch.id !== batchId));
      toast.success("The review batch was deleted. Its history has been retained.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The review batch could not be deleted."),
  });
  const deleteJobMutation = useMutation({
    mutationFn: ({ jobType, jobId }: { jobType: string; jobId: string }) => coreApi<void>(`/v1/jobs/${jobType}/${jobId}`, { method: "DELETE" }),
    onSuccess: (_, { jobType, jobId }) => {
      const remove = <T extends { id: string }>(current: T[] | undefined) => current?.filter((job) => job.id !== jobId);
      if (jobType === "MATTER_DOCUMENT_IMPORT") queryClient.setQueryData<MatterDocumentImportRead[]>(["matter-document-imports", matterId], remove);
      if (jobType === "MATTER_EMBEDDING_JOB") queryClient.setQueryData<MatterEmbeddingJobRead[]>(["matter-embedding-jobs", matterId], remove);
      if (jobType === "MATTER_TOPIC_JOB") queryClient.setQueryData<MatterTopicJobRead[]>(["matter-topic-jobs", matterId], remove);
      if (jobType === "MATTER_BULK_TAG_JOB") queryClient.setQueryData<MatterBulkTagJobRead[]>(["matter-bulk-tag-jobs", matterId], remove);
      if (jobType === "ANALYSIS_TASK_BATCH_RUN") queryClient.setQueryData<MatterQuestionAnsweringJobRead[]>(["matter-question-answering-jobs", matterId], remove);
      if (jobType === "CLASSIFICATION_ANALYSIS_TASK") {
        queryClient.setQueriesData<ClassificationAnalysisJobRow[]>({ queryKey: ["matter-classification-analysis-jobs", matterId] }, remove);
      }
      if (jobType === "MATTER_SEARCH_EXPORT") queryClient.setQueryData<MatterSearchExportRead[]>(["matter-search-export-jobs", matterId], remove);
      toast.success("The job was deleted. Its execution history and results have been retained.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The job could not be deleted."),
  });
  const deleteJob = useCallback(
    (jobType: string, jobId: string) => deleteJobMutation.mutateAsync({ jobType, jobId }).then(() => undefined),
    [deleteJobMutation],
  );
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
  const createGroup = useCallback((values: CreateMetadataGroupValues) => groupMutation.mutateAsync(values).then(() => undefined), [groupMutation]);
  const changeVisibility = useCallback((group: MetadataGroupRead, surface: "TABLE" | "DOCUMENT", visible: boolean) => visibilityMutation.mutateAsync({ group, surface, visible }).then(() => undefined), [visibilityMutation]);
  const saveTemplate = useCallback((values: SaveMatterTemplateValues) => templateMutation.mutateAsync(values).then(() => undefined), [templateMutation]);
  const cloneMatter = useCallback((values: CloneMatterValues) => cloneMutation.mutateAsync(values).then(() => undefined), [cloneMutation]);
  const createReviewBatch = useCallback((values: ReviewBatchCreate) => createReviewBatchMutation.mutateAsync(values).then(() => undefined), [createReviewBatchMutation]);
  const assignBatchCodingGroups = useCallback((batchId: string, codingGroupIds: string[]) => assignBatchCodingGroupsMutation.mutateAsync({ batchId, codingGroupIds }).then(() => undefined), [assignBatchCodingGroupsMutation]);
  const updateDefinitionCache = useCallback((updated: MetadataDefinitionRead) => {
    queryClient.setQueryData<MetadataDefinitionRead[]>(["metadata-definitions", matterId], (current) => current?.map((definition) => definition.id === updated.id ? updated : definition));
  }, [matterId, queryClient]);
  const columns = useMemo<ColumnDef<MetadataDefinitionRead>[]>(() => [
    { accessorKey: "display_name", header: "Field", cell: ({ row }) => <div><p className="font-semibold">{row.original.display_name}</p><code className="text-xs text-muted-foreground">{row.original.key}</code></div> },
    { accessorKey: "type", header: "Type", cell: ({ row }) => <Badge>{row.original.type.toLowerCase().replace("_", " ")}</Badge> },
    { id: "allowed_values", header: "Enum values", size: 220, cell: ({ row }) => row.original.type === "ENUM" ? <span className="text-muted-foreground">{(row.original.allowed_values ?? []).filter((value) => value.active !== false).length} active · {(row.original.allowed_values ?? []).filter((value) => value.active === false).length} inactive</span> : <span className="text-muted-foreground">—</span> },
    { accessorKey: "value_source", header: "Source", cell: ({ row }) => <Badge variant="outline">{row.original.value_source.toLowerCase()}</Badge> },
    { accessorKey: "cardinality", header: "Values", cell: ({ row }) => <span className="text-muted-foreground">{row.original.cardinality.toLowerCase()}</span> },
    { id: "behavior", header: "Behavior", cell: ({ row }) => <div className="flex flex-wrap gap-1">{row.original.searchable ? <Badge variant="outline">search</Badge> : null}{row.original.facetable ? <Badge variant="outline">filter</Badge> : null}{row.original.normalize_to_lowercase ? <Badge variant="outline">lowercase</Badge> : null}{row.original.hierarchy_separator ? <Badge variant="outline">hierarchy <code>{row.original.hierarchy_separator}</code></Badge> : null}{row.original.ai_assignable ? <Badge variant="accent">agent</Badge> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "actions", header: "", size: 100, cell: ({ row }) => row.original.value_source === "ASSERTED" ? <div className="flex justify-end"><MetadataDefinitionEditor matterId={matterId} definition={row.original} onChange={updateDefinitionCache} /></div> : null },
  ], [matterId, updateDefinitionCache]);

  const openTab = useCallback((nextTab: MatterTab, jobId?: string) => {
    const params = new URLSearchParams({ tab: nextTab });
    if (jobId) params.set("job", jobId);
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);

  const openMetadataTab = useCallback((nextTab: MetadataTab) => {
    const params = new URLSearchParams({ tab: "metadata", metadata: nextTab });
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);

  const openClassificationJob = useCallback((jobId: string) => {
    setSelectedClassificationJobId(jobId);
    window.localStorage.setItem(MATTER_JOB_TYPE_STORAGE_KEY, "CLASSIFICATION_ANALYSIS");
    const params = new URLSearchParams({ tab: "jobs", job_type: "CLASSIFICATION_ANALYSIS", job: jobId });
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);

  const closeClassificationJob = useCallback(() => {
    setSelectedClassificationJobId(undefined);
    const params = new URLSearchParams({ tab: "jobs", job_type: "CLASSIFICATION_ANALYSIS" });
    router.replace(`/app/clients/${clientId}/matters/${matterId}?${params}`);
  }, [clientId, matterId, router]);

  const jobColumns = useMemo<ColumnDef<MatterDocumentImportRead>[]>(() => [
    { accessorKey: "selection_summary", header: "Selection", cell: ({ row }) => <button type="button" className="max-w-md text-left font-semibold hover:text-primary" onClick={() => openTab("jobs", row.original.id)}>{row.original.selection_summary}</button> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div className="space-y-1"><StatusBadge status={row.original.status} />{row.original.execution ? <p className="whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <JobProgress job={row.original} /> },
    { accessorKey: "added_count", header: "Added", cell: ({ row }) => <span className="tabular-nums">{row.original.added_count.toLocaleString()}</span> },
    { accessorKey: "duplicate_count", header: "Already present", cell: ({ row }) => <span className="tabular-nums text-muted-foreground">{row.original.duplicate_count.toLocaleString()}</span> },
    { accessorKey: "imported_label_event_count", header: "Imported labels", cell: ({ row }) => <span className="tabular-nums">{(row.original.imported_label_event_count ?? 0).toLocaleString()}</span> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(row.original.status) ? <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_DOCUMENT_IMPORT" jobId={row.original.id} jobName={row.original.selection_summary} onDelete={deleteJob} /> : null },
  ], [canDeleteJobs, deleteJob, openTab]);
  const embeddingJobColumns = useMemo<ColumnDef<MatterEmbeddingJobRead>[]>(() => [
    { accessorKey: "embedding_model", header: "Model", cell: ({ row }) => <div><p className="max-w-64 truncate font-semibold">{row.original.embedding_model}</p><p className="text-xs text-muted-foreground">{row.original.embedding_dimensions.toLocaleString()} dimensions</p>{row.original.error_message ? <p className="mt-1 max-w-80 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div className="space-y-1"><StatusBadge status={row.original.status} />{row.original.execution ? <p className="whitespace-nowrap text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <EmbeddingProgress job={row.original} /> },
    { accessorKey: "embedded_count", header: "Generated", cell: ({ row }) => <span className="tabular-nums">{row.original.embedded_count.toLocaleString()}</span> },
    { accessorKey: "skipped_count", header: "Skipped", cell: ({ row }) => <span className="tabular-nums text-muted-foreground">{row.original.skipped_count.toLocaleString()}</span> },
    { accessorKey: "chunk_count", header: "Chunks", cell: ({ row }) => <span className="tabular-nums">{row.original.chunk_count.toLocaleString()}</span> },
    { id: "provider_usage", header: "Token usage", cell: ({ row }) => <EmbeddingTokenUsage job={row.original} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => (row.original.execution?.available_actions.includes("CANCEL") ?? ["QUEUED", "PLANNING", "RUNNING"].includes(row.original.status))
      ? <Button size="sm" variant="outline" onClick={() => cancelEmbeddingJobMutation.mutate(row.original.id)}><Ban />Cancel</Button>
      : <DeletableJobActions
          canDelete={canDeleteJobs}
          jobType="MATTER_EMBEDDING_JOB"
          jobId={row.original.id}
          jobName={`${row.original.embedding_model} embedding job`}
          onDelete={deleteJob}
          primary={row.original.status === "FAILED" ? <button type="button" disabled={retryEmbeddingIndexMutation.isPending} onClick={() => retryEmbeddingIndexMutation.mutate(row.original.id)}><RotateCcw />Retry indexing</button> : undefined}
        /> },
  ], [canDeleteJobs, cancelEmbeddingJobMutation, deleteJob, retryEmbeddingIndexMutation]);
  const topicJobColumns = useMemo<ColumnDef<MatterTopicJobRead>[]>(() => [
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
      const active = ["QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"].includes(row.original.status);
      if (active) return <div className="flex justify-end gap-2">{row.original.status === "AWAITING_REVIEW" ? <Button asChild size="sm"><Link href={`/app/clients/${clientId}/matters/${matterId}/topic-jobs/${row.original.id}`}><CheckCircle2 />Review topics</Link></Button> : null}<Button size="sm" variant="outline" onClick={() => cancelTopicJobMutation.mutate(row.original.id)}><Ban />Cancel</Button></div>;
      return <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_TOPIC_JOB" jobId={row.original.id} jobName="Topic clustering job" onDelete={deleteJob} primary={row.original.clusters?.length ? <Link href={`/app/clients/${clientId}/matters/${matterId}/topic-jobs/${row.original.id}`}><FileSearch />View topics</Link> : undefined} />;
    } },
  ], [canDeleteJobs, cancelTopicJobMutation, clientId, deleteJob, matterId]);
  const bulkTagJobColumns = useMemo<ColumnDef<MatterBulkTagJobRead>[]>(() => {
    const definitionById = new Map((definitions.data ?? []).map((definition) => [definition.id, definition]));
    return [
      { id: "field", header: "Tag", cell: ({ row }) => {
        const assignments = row.original.assignments.length ? row.original.assignments : [{ metadata_definition_id: row.original.metadata_definition_id, value: row.original.value }];
        const label = assignments.map((assignment) => {
          const definition = definitionById.get(assignment.metadata_definition_id);
          return `${definition?.display_name ?? "Unknown field"}: ${formatBulkTagValue(assignment.value, definition)}`;
        }).join("; ");
        return <p className="max-w-80 truncate text-sm" title={label}>{label}</p>;
      } },
      { id: "selection", header: "Search", cell: ({ row }) => <span className="block max-w-72 truncate text-muted-foreground" title={bulkTagSearchLabel(row.original.search_definition)}>{bulkTagSearchLabel(row.original.search_definition)}</span> },
      { accessorKey: "status", header: "Status", cell: ({ row }) => <div><StatusBadge status={bulkTagDisplayStatus(row.original)} />{row.original.error_message ? <p className="mt-1 max-w-64 truncate text-xs text-destructive" title={row.original.error_message}>{row.original.error_message}</p> : null}</div> },
      { id: "progress", header: "Progress", cell: ({ row }) => <BulkTagProgress job={row.original} /> },
      { accessorKey: "tagged_count", header: "Tagged", cell: ({ row }) => <span className="tabular-nums">{row.original.tagged_count.toLocaleString()}</span> },
      { accessorKey: "failed_count", header: "Failed", cell: ({ row }) => <span className={row.original.failed_count ? "tabular-nums text-destructive" : "tabular-nums text-muted-foreground"}>{row.original.failed_count.toLocaleString()}</span> },
      { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
      { id: "actions", header: "", cell: ({ row }) => ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(row.original.status) ? <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_BULK_TAG_JOB" jobId={row.original.id} jobName="Bulk tagging job" onDelete={deleteJob} /> : null },
    ];
  }, [canDeleteJobs, definitions.data, deleteJob]);
  const classificationAnalysisJobColumns = useMemo<ColumnDef<ClassificationAnalysisJobRow>[]>(() => [
    { accessorKey: "task_name", header: "Analysis task", cell: ({ row }) => <div><p className="font-semibold">{row.original.task_name}</p><p className="text-xs text-muted-foreground">{row.original.scope_type === "MATTER" ? "Whole matter" : "Review batch"}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => {
      const execution = row.original.execution;
      const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(row.original.status);
      const domainStage = row.original.current_stage.replaceAll("_", " ").toLowerCase();
      const stage = terminal ? `${domainStage.charAt(0).toUpperCase()}${domainStage.slice(1)}` : execution?.stage_label ?? row.original.current_stage;
      const failure = terminal ? row.original.failure_summary ?? execution?.failure_summary : execution?.failure_summary ?? row.original.failure_summary;
      return <div className="space-y-1"><StatusBadge status={row.original.status} /><p className="whitespace-nowrap text-xs text-muted-foreground">{stage} · attempt {execution?.attempt_number ?? 1}</p>{failure ? <p className="max-w-80 truncate text-xs text-destructive" title={failure}>{failure}</p> : null}</div>;
    } },
    { id: "progress", header: "Progress", cell: ({ row }) => {
      const completed = row.original.execution?.progress_completed ?? row.original.progress_completed;
      const total = row.original.execution?.progress_total ?? row.original.progress_total;
      const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : 0;
      return <div className="min-w-44"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><p className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"} stages</p>{row.original.population_document_count ? <p className="text-xs tabular-nums text-muted-foreground">{row.original.population_document_count.toLocaleString()} documents frozen</p> : null}</div>;
    } },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => {
      const actions = row.original.execution?.available_actions ?? [];
      const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(row.original.status);
      const canRetry = actions.includes("RETRY") || row.original.status === "CANCELED" || (row.original.status === "FAILED" && row.original.failure_retriable);
      return <div className="flex justify-end gap-2">{!terminal && actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelClassificationAnalysisMutation.isPending} onClick={() => cancelClassificationAnalysisMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null}{canRetry ? <Button size="sm" variant="outline" disabled={retryClassificationAnalysisMutation.isPending} onClick={() => retryClassificationAnalysisMutation.mutate(row.original.id)}><RotateCcw />Retry</Button> : null}<DeletableJobActions canDelete={canDeleteJobs && terminal} jobType="CLASSIFICATION_ANALYSIS_TASK" jobId={row.original.id} jobName={row.original.task_name} onDelete={deleteJob} primary={<button type="button" onClick={() => openClassificationJob(row.original.id)}><FileSearch />View job</button>} /></div>;
    } },
  ], [canDeleteJobs, cancelClassificationAnalysisMutation, deleteJob, openClassificationJob, retryClassificationAnalysisMutation]);
  const questionAnsweringJobColumns = useMemo<ColumnDef<MatterQuestionAnsweringJobRead>[]>(() => [
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
      return <div className="min-w-44"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><p className="text-xs tabular-nums text-muted-foreground">{successful.toLocaleString()} / {total.toLocaleString()} successful</p></div>;
    } },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: ({ row }) => <div className="flex justify-end gap-2">{row.original.execution?.available_actions.includes("CANCEL") ? <Button size="sm" variant="outline" disabled={cancelQuestionAnsweringMutation.isPending} onClick={() => cancelQuestionAnsweringMutation.mutate(row.original.id)}><Ban />Cancel</Button> : null}<QuestionAnsweringJobActions matterId={matterId} job={row.original} canDelete={canDeleteJobs && ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(row.original.status)} onDelete={deleteJob} /></div> },
  ], [canDeleteJobs, cancelQuestionAnsweringMutation, deleteJob, matterId]);
  const searchExportJobColumns = useMemo<ColumnDef<MatterSearchExportRead>[]>(() => [
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

  const classificationJobsPending = jobType === "CLASSIFICATION_ANALYSIS" && (classificationTasks.isPending || classificationAnalysisJobs.isPending);
  const classificationJobsError = jobType === "CLASSIFICATION_ANALYSIS" ? classificationTasks.error ?? classificationAnalysisJobs.error : null;
  const questionAnsweringJobsPending = jobType === "QUESTION_ANSWERING" && questionAnsweringJobs.isPending;
  const questionAnsweringJobsError = jobType === "QUESTION_ANSWERING" ? questionAnsweringJobs.error : null;
  const searchExportJobsPending = jobType === "SEARCH_EXPORTS" && searchExportJobs.isPending;
  const searchExportJobsError = jobType === "SEARCH_EXPORTS" ? searchExportJobs.error : null;

  if (client.isPending || matter.isPending) return <TableLoading />;
  if (client.error || matter.error) return <QueryError message={client.error?.message ?? matter.error?.message} />;

  return (
    <div className={tab === "definition" ? "matter-definition-page-frame" : undefined}>
      <div className={tab === "definition" ? "matter-definition-page-header" : undefined}>
        <ResourcePageHeader
          breadcrumbs={<><Link href="/app/clients" className="hover:text-foreground">Clients</Link><ChevronRight className="size-4" /><Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{matter.data.name}</span></>}
          title={matter.data.name}
        />

        <div className="mb-6 flex min-w-0 flex-wrap items-end justify-between gap-x-4 border-b" aria-label="Matter navigation and actions">
          <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto" role="tablist" aria-label="Matter sections">
            <button role="tab" aria-selected={tab === "overview"} onClick={() => openTab("overview")} className={tabClass(tab === "overview")}>Overview</button>
            <button role="tab" aria-selected={tab === "metadata"} onClick={() => openTab("metadata")} className={tabClass(tab === "metadata")}>Metadata</button>
            <button role="tab" aria-selected={tab === "definition"} onClick={() => openTab("definition")} className={tabClass(tab === "definition")}>Matter definition</button>
            <button role="tab" aria-selected={tab === "analysis"} onClick={() => openTab("analysis")} className={tabClass(tab === "analysis")}>Analysis tasks</button>
            <button role="tab" aria-selected={tab === "batches"} onClick={() => openTab("batches")} className={tabClass(tab === "batches")}>Batches</button>
            <button role="tab" aria-selected={tab === "classification"} onClick={() => openTab("classification")} className={tabClass(tab === "classification")}>Classification</button>
            <button role="tab" aria-selected={tab === "jobs"} onClick={() => openTab("jobs")} className={tabClass(tab === "jobs")}>Jobs</button>
            <button role="tab" aria-selected={tab === "usage"} onClick={() => openTab("usage")} className={tabClass(tab === "usage")}>Token usage</button>
            <button role="tab" aria-selected={tab === "search"} onClick={() => openTab("search")} className={tabClass(tab === "search")}>Search index</button>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2 pb-2">
            <Button asChild><Link href={`/review/matters/${matterId}`}><FileSearch />Search & Review</Link></Button>
            <HelpLink topic={tab === "classification" ? "classification" : tab === "jobs" ? "matterJobs" : tab === "usage" ? "tokenUsage" : tab === "search" ? "matterSearch" : tab === "definition" ? "matterDefinition" : "metadata"} />
            {tab === "overview" ? <><CloneMatterDialog sourceName={matter.data.name} onClone={cloneMatter} /><SaveMatterTemplateDialog onCreate={saveTemplate} /></> : null}
          </div>
        </div>
      </div>
      <div className={tab === "definition" ? "matter-definition-page-content" : undefined}>
        {tab === "metadata" ? <div className="mb-5 flex items-end justify-between gap-3 border-b">
          <div className="flex" role="tablist" aria-label="Metadata sections">
            <button type="button" role="tab" aria-selected={metadataTab === "definitions"} onClick={() => openMetadataTab("definitions")} className={subtabClass(metadataTab === "definitions")}>Definitions</button>
            <button type="button" role="tab" aria-selected={metadataTab === "groups"} onClick={() => openMetadataTab("groups")} className={subtabClass(metadataTab === "groups")}>Groups</button>
          </div>
          {metadataTab === "definitions" ? <div className="pb-2"><CreateMetadataDialog onCreate={createDefinition} /></div> : null}
        </div> : null}
        {tab === "overview" ? overviewCounts.error ? <QueryError message={overviewCounts.error.message} /> : <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5"><Summary icon={FileText} label="Documents" value={overviewCounts.data?.document_count ?? "—"} /><Summary icon={UsersRound} label="Custodians" value={overviewCounts.data?.custodian_count ?? "—"} /><Summary icon={Database} label="Metadata fields" value={definitions.data?.length ?? 0} /><Summary icon={Search} label="Searchable fields" value={definitions.data?.filter((item) => item.searchable).length ?? 0} /><Summary icon={Sparkles} label="Agent assignable" value={definitions.data?.filter((item) => item.ai_assignable).length ?? 0} /></div>
        : tab === "metadata" && metadataTab === "definitions" ? definitions.isPending ? <TableLoading /> : definitions.error ? <QueryError message={definitions.error.message} /> : <DataTable columns={columns} data={definitions.data} emptyMessage="No metadata fields yet. Add the first field definition for this matter." />
        : tab === "metadata" ? definitions.isPending || groups.isPending ? <TableLoading /> : definitions.error || groups.error ? <QueryError message={definitions.error?.message ?? groups.error?.message} /> : <MetadataGroupsPanel definitions={definitions.data} groups={groups.data} onCreate={createGroup} onVisibilityChange={changeVisibility} />
        : tab === "definition" ? <MatterDefinitionPanel matterId={matterId} />
        : tab === "analysis" ? <AnalysisTasksPanel clientId={clientId} matterId={matterId} />
        : tab === "batches" ? reviewBatches.isPending || groups.isPending || tenantUsers.isPending || savedSearches.isPending || assessments.isPending ? <TableLoading /> : reviewBatches.error || groups.error || tenantUsers.error || savedSearches.error || assessments.error ? <QueryError message={reviewBatches.error?.message ?? groups.error?.message ?? tenantUsers.error?.message ?? savedSearches.error?.message ?? assessments.error?.message} /> : <div className="space-y-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Review batches</h2><p className="text-sm text-muted-foreground">Freeze document sets for assignment, repeatable agent runs, and coding comparisons.</p></div><CreateReviewBatchDialog groups={groups.data} batches={reviewBatches.data} savedSearches={savedSearches.data} assessments={assessments.data} onCreate={createReviewBatch} /></div><DataTable columns={reviewBatchColumns} data={reviewBatches.data} emptyMessage="No review batches have been created for this matter." /></div>
        : tab === "classification" ? reviewBatches.isPending || embeddingJobs.isPending || definitions.isPending ? <TableLoading /> : reviewBatches.error || embeddingJobs.error || definitions.error ? <QueryError message={reviewBatches.error?.message ?? embeddingJobs.error?.message ?? definitions.error?.message} /> : <ClassificationPanel clientId={clientId} matterId={matterId} definitions={definitions.data} reviewBatches={reviewBatches.data} embeddingJobs={embeddingJobs.data} />
        : tab === "jobs" ? jobs.isPending || embeddingJobs.isPending || topicJobs.isPending || bulkTagJobs.isPending || savedSearches.isPending || classificationJobsPending || questionAnsweringJobsPending || searchExportJobsPending ? <TableLoading /> : jobs.error || embeddingJobs.error || topicJobs.error || bulkTagJobs.error || savedSearches.error || classificationJobsError || questionAnsweringJobsError || searchExportJobsError ? <QueryError message={jobs.error?.message ?? embeddingJobs.error?.message ?? topicJobs.error?.message ?? bulkTagJobs.error?.message ?? savedSearches.error?.message ?? classificationJobsError?.message ?? questionAnsweringJobsError?.message ?? searchExportJobsError?.message} /> : <div className="space-y-6">
          <div className="flex flex-wrap items-end justify-between gap-4 rounded-xl border bg-card p-4">
            <div><h2 className="text-lg font-semibold">Matter jobs</h2><p className="text-sm text-muted-foreground">Choose which type of background work to view.</p></div>
            <div className="w-full space-y-2 sm:w-64"><Label htmlFor="matter-job-type">Job type</Label><Select value={jobType} onValueChange={changeJobType}><SelectTrigger id="matter-job-type" aria-label="Job type"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="BULK_TAGGING">Bulk tagging</SelectItem><SelectItem value="DOCUMENT_EMBEDDINGS">Document embeddings</SelectItem><SelectItem value="TOPIC_CLUSTERING">Topic clustering</SelectItem><SelectItem value="DOCUMENT_IMPORTS">Document imports</SelectItem><SelectItem value="QUESTION_ANSWERING">Question answering</SelectItem><SelectItem value="CLASSIFICATION_ANALYSIS">Classification analysis</SelectItem><SelectItem value="SEARCH_EXPORTS">Search exports</SelectItem></SelectContent></Select></div>
          </div>
          {jobType === "BULK_TAGGING" ? <section className="space-y-4">
            <div><h2 className="flex items-center gap-2 text-lg font-semibold"><Tags className="size-5 text-primary" />Bulk tagging</h2><p className="text-sm text-muted-foreground">Coding changes applied to the frozen results of a matter search.</p></div>
            <DataTable columns={bulkTagJobColumns} data={bulkTagJobs.data} emptyMessage="No bulk tag jobs have been started for this matter." />
          </section> : null}
          {jobType === "DOCUMENT_EMBEDDINGS" ? <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Document embeddings</h2><p className="text-sm text-muted-foreground">Create sentence-aware chunks and vectors for every document in this matter.</p></div><Button onClick={() => createEmbeddingJobMutation.mutate()} disabled={createEmbeddingJobMutation.isPending || embeddingJobs.data.some((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status))}><Play />Generate embeddings</Button></div>
            <DataTable columns={embeddingJobColumns} data={embeddingJobs.data} emptyMessage="No embedding jobs have been started for this matter." />
          </section> : null}
          {jobType === "TOPIC_CLUSTERING" ? <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-lg font-semibold">Topic clustering</h2><p className="text-sm text-muted-foreground">Discover topic proposals, review them, and explicitly approve the set before document metadata is changed.</p>{!embeddingJobs.data.some((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0) ? <p className="mt-1 text-xs text-muted-foreground">Complete an embedding job before clustering topics.</p> : null}</div><CreateTopicJobDialog savedSearches={savedSearches.data.filter((saved) => (saved.search.search_mode ?? "KEYWORD") === "KEYWORD")} definitions={definitions.data ?? []} onCreate={(values) => createTopicJobMutation.mutateAsync(values).then(() => undefined)} disabled={createTopicJobMutation.isPending || !embeddingJobs.data.some((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && job.chunk_count > 0) || topicJobs.data.some((job) => ["QUEUED", "SAMPLING", "CLUSTERING", "AWAITING_REVIEW", "PUBLISHING"].includes(job.status))} /></div>
            <DataTable columns={topicJobColumns} data={topicJobs.data} emptyMessage="No topic clustering jobs have been started for this matter." />
          </section> : null}
          {jobType === "DOCUMENT_IMPORTS" ? <section className="space-y-4">
            <div><h2 className="text-lg font-semibold">Document imports</h2><p className="text-sm text-muted-foreground">Jobs that add collection documents to this matter.</p></div>
            {selectedJobId ? <JobDetail job={jobs.data.find((job) => job.id === selectedJobId)} onCancel={(jobId) => cancelJobMutation.mutate(jobId)} /> : null}
            <DataTable columns={jobColumns} data={jobs.data} emptyMessage="No document import jobs have been started for this matter." />
          </section> : null}
          {jobType === "QUESTION_ANSWERING" ? <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><Sparkles className="size-5 text-primary" />Question answering</h2><p className="text-sm text-muted-foreground">Question-answering Analysis Task runs across review batches. Results remain isolated until explicitly published.</p></div><Button asChild variant="outline"><Link href={`/app/clients/${clientId}/matters/${matterId}?tab=analysis`}><FileSearch />Analysis tasks</Link></Button></div>
            <DataTable columns={questionAnsweringJobColumns} data={questionAnsweringJobs.data ?? []} emptyMessage="No question-answering jobs have been started for this matter." />
          </section> : null}
          {jobType === "CLASSIFICATION_ANALYSIS" ? <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><BrainCircuit className="size-5 text-primary" />Classification analysis</h2><p className="text-sm text-muted-foreground">Classifier runs across a review batch or the whole matter. Results remain analysis output until explicitly published.</p></div><Button asChild variant="outline"><Link href={`/app/clients/${clientId}/matters/${matterId}?tab=analysis`}><FileSearch />Analysis tasks</Link></Button></div>
            {selectedClassificationJobId && classificationAnalysisJobs.data?.find((run) => run.id === selectedClassificationJobId) ? <ClassificationAnalysisJobDetail
              clientId={clientId}
              matterId={matterId}
              run={classificationAnalysisJobs.data.find((run) => run.id === selectedClassificationJobId)!}
              onClose={closeClassificationJob}
            /> : null}
            <DataTable columns={classificationAnalysisJobColumns} data={classificationAnalysisJobs.data ?? []} emptyMessage="No classification analysis jobs have been started for this matter." />
          </section> : null}
          {jobType === "SEARCH_EXPORTS" ? <section className="space-y-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><FileDown className="size-5 text-primary" />Search exports</h2><p className="text-sm text-muted-foreground">Durable CSV exports created from frozen matter-search definitions. Completed files are stored by the Artifact Service.</p></div><Button asChild variant="outline"><Link href={`/review/matters/${matterId}`}><Search />Search & Review</Link></Button></div>
            <DataTable columns={searchExportJobColumns} data={searchExportJobs.data ?? []} emptyMessage="No search exports have been created for this matter." />
          </section> : null}
        </div>
          : tab === "usage" ? providerUsage.isPending ? <TableLoading /> : providerUsage.error ? <QueryError message={providerUsage.error.message} /> : <MatterTokenUsagePanel report={providerUsage.data} loading={providerUsage.isFetching} onPageChange={setUsageOffset} />
          : searchIndexes.isPending || searchOperations.isPending || retryableSearchOperations.isPending || overviewCounts.isPending ? <TableLoading /> : searchIndexes.error || searchOperations.error || retryableSearchOperations.error || overviewCounts.error ? <QueryError message={searchIndexes.error?.message ?? searchOperations.error?.message ?? retryableSearchOperations.error?.message ?? overviewCounts.error?.message} /> : <SearchIndexPanel coreDocumentCount={overviewCounts.data.document_count} indexes={searchIndexes.data} operations={searchOperations.data} onRebuild={() => rebuildSearchMutation.mutateAsync().then(() => undefined)} rebuilding={rebuildSearchMutation.isPending} onConfirmReindex={(operationId) => confirmReindexMutation.mutateAsync(operationId).then(() => undefined)} confirmingReindex={confirmReindexMutation.isPending} onRetryFailed={() => retryFailedSearchMutation.mutateAsync().then(() => undefined)} retryingFailed={retryFailedSearchMutation.isPending} retryableOperationCount={retryableSearchOperations.data.requeued_operation_count} retryableDocumentCount={retryableSearchOperations.data.requeued_document_count} />}
      </div>
    </div>
  );
}

function tabClass(active: boolean) {
  return `relative px-4 py-3 text-sm font-semibold outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${active ? "text-primary after:absolute after:inset-x-1 after:bottom-0 after:h-0.5 after:bg-accent" : "text-muted-foreground hover:text-foreground"}`;
}

function subtabClass(active: boolean) {
  return `relative px-4 py-2.5 text-sm font-semibold outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${active ? "text-primary after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-primary" : "text-muted-foreground hover:text-foreground"}`;
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

function DeletableJobActions({ canDelete, jobType, jobId, jobName, onDelete, primary }: {
  canDelete: boolean;
  jobType: string;
  jobId: string;
  jobName: string;
  onDelete: (jobType: string, jobId: string) => Promise<void>;
  primary?: ReactElement;
}) {
  const [open, setOpen] = useState(false);
  if (!canDelete) return primary ? <Button asChild size="sm" variant="outline">{primary}</Button> : null;
  return <>
    {primary ? <SplitActionButton
      label={jobName}
      primary={primary}
      variant="outline"
      actions={[{
        id: "delete",
        label: "Delete job",
        icon: <Trash2 aria-hidden="true" className="size-4" />,
        destructive: true,
        onSelect: () => setOpen(true),
      }]}
    /> : <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}><Trash2 />Delete job</Button>}
    <SoftDeleteDialog open={open} onOpenChange={setOpen} resourceType="job" resourceName={jobName} onDelete={() => onDelete(jobType, jobId)} />
  </>;
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
  return { ALL_MATTER: "All matter documents", SEARCH_QUERY: "Keyword search", RANDOM_MATTER: "Random matter sample", RANDOM_BATCH: "Random batch sample", RANDOM_ASSESSMENT: "Random assessment-candidate sample", DEFINITION_ASSESSMENT: "Matter Definition assessment" }[batch.selection_type];
}

function Summary({ icon: Icon, label, value }: { icon: typeof ListChecks; label: string; value: number | string }) {
  return <Card><CardHeader><span className="mb-2 grid size-9 place-items-center rounded-lg bg-secondary text-primary"><Icon className="size-4" /></span><CardTitle className="text-sm text-muted-foreground">{label}</CardTitle></CardHeader><CardContent><p className="text-3xl font-semibold tabular-nums">{value}</p></CardContent></Card>;
}

function JobProgress({ job }: { job: MatterDocumentImportRead }) {
  const completed = job.execution?.progress_completed ?? job.processed_count;
  const total = job.execution?.progress_total ?? job.matched_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"}</span></div>;
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

function EmbeddingProgress({ job }: { job: MatterEmbeddingJobRead }) {
  const completed = job.execution?.progress_completed ?? job.processed_count;
  const total = job.execution?.progress_total ?? job.total_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) ? 100 : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total?.toLocaleString() ?? "—"}</span>{job.failed_count ? <span className="ml-2 text-xs text-destructive">{job.failed_count.toLocaleString()} failed</span> : null}</div>;
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

function BulkTagProgress({ job }: { job: MatterBulkTagJobRead }) {
  const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"].includes(job.status);
  const percent = job.matched_count ? Math.min(100, Math.round((job.processed_count / job.matched_count) * 100)) : terminal ? 100 : 0;
  return <div className="min-w-36"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{bulkTagIsWaiting(job) ? "Waiting for capacity" : `${job.processed_count.toLocaleString()} / ${job.matched_count.toLocaleString()} · ${percent}%`}</span></div>;
}

function SearchExportProgress({ job }: { job: MatterSearchExportRead }) {
  const total = job.matched_count;
  const completed = job.exported_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-40"><div className="mb-1 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary" style={{ width: `${percent}%` }} /></div><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total ? total.toLocaleString() : "—"} rows</span></div>;
}

function formatBytes(value: number | null) {
  if (value == null) return "—";
  if (value < 1024) return `${value.toLocaleString()} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}

function bulkTagSearchLabel(searchDefinition: Record<string, unknown>) {
  const query = typeof searchDefinition.query === "string" ? searchDefinition.query.trim() : "";
  const filters = Array.isArray(searchDefinition.filters) ? searchDefinition.filters.length : 0;
  if (query && filters) return `“${query}” + ${filters} ${filters === 1 ? "filter" : "filters"}`;
  if (query) return `“${query}”`;
  if (filters) return `${filters} ${filters === 1 ? "filter" : "filters"}`;
  return "Frozen search";
}

function formatBulkTagValue(value: unknown, definition?: MetadataDefinitionRead): string {
  if (Array.isArray(value)) return value.map((item) => formatBulkTagValue(item, definition)).join(", ");
  const serialized = typeof value === "string" ? value : value == null ? "—" : JSON.stringify(value);
  if (!definition || typeof value !== "string") return serialized;
  return definition.allowed_values?.find((option) => option.key === value)?.label ?? serialized;
}

function JobDetail({ job, onCancel }: { job?: MatterDocumentImportRead; onCancel: (jobId: string) => void }) {
  if (!job) return <Card className="p-5 text-sm text-muted-foreground">The selected job is not available in the recent job history.</Card>;
  const active = job.execution?.available_actions.includes("CANCEL") ?? ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status);
  return <Card className="p-5"><div className="flex flex-wrap items-start justify-between gap-4"><div><div className="mb-2 flex items-center gap-2"><BriefcaseBusiness className="size-4 text-primary" /><StatusBadge status={job.status} /></div><h2 className="text-lg font-semibold">{job.selection_summary}</h2><p className="mt-1 text-sm text-muted-foreground">Created {formatDate(job.created_at)} · {job.batch_count.toLocaleString()} {job.batch_count === 1 ? "batch" : "batches"}{job.execution ? ` · ${job.execution.stage_label} · attempt ${job.execution.attempt_number}` : ""}</p></div>{active ? <Button variant="outline" onClick={() => onCancel(job.id)}><Ban />Cancel job</Button> : null}</div><dl className="mt-5 grid gap-3 text-sm sm:grid-cols-4"><JobMetric label="Matched" value={job.matched_count} /><JobMetric label="Processed" value={job.processed_count} /><JobMetric label="Added" value={job.added_count} /><JobMetric label="Already present" value={job.duplicate_count} /><JobMetric label="Documents labeled" value={job.imported_label_document_count ?? 0} /><JobMetric label="Imported labels" value={job.imported_label_event_count ?? 0} /><JobMetric label="Label warnings" value={job.imported_label_warning_count ?? 0} /></dl>{job.error_message ? <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{job.error_message}</p> : null}</Card>;
}

function JobMetric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-lg bg-muted/50 p-3"><dt className="text-muted-foreground">{label}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{value.toLocaleString()}</dd></div>;
}
