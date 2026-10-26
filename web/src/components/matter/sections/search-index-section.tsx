"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { useMatter } from "@/components/matter/matter-context";
import { QueryError, TableLoading } from "@/components/query-state";
import { SearchIndexPanel } from "@/components/search-index-panel";
import type { MatterOverviewCounts, SearchIndexGenerationRead, SearchProjectionOperationRead, SearchProjectionRetryResponse } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

export function SearchIndexSection() {
  const { matterId } = useMatter();
  const queryClient = useQueryClient();
  const overviewCounts = useQuery({
    queryKey: ["matter-overview-counts", matterId],
    queryFn: () => coreApi<MatterOverviewCounts>(`/v1/matters/${matterId}/overview-counts`),
  });
  const searchIndexes = useQuery({
    queryKey: ["search-indexes", matterId],
    queryFn: () => coreApi<SearchIndexGenerationRead[]>(`/v1/matters/${matterId}/search-indexes`),
    refetchInterval: 3000,
  });
  const searchOperations = useQuery({
    queryKey: ["search-operations", matterId],
    queryFn: () => coreApi<SearchProjectionOperationRead[]>(`/v1/matters/${matterId}/search-operations?limit=500`),
    refetchInterval: (query) => query.state.data?.some((operation) => operation.status === "QUEUED" || operation.status === "RUNNING") ? 2000 : false,
  });
  const retryableSearchOperations = useQuery({
    queryKey: ["retryable-search-operations", matterId],
    queryFn: () => coreApi<SearchProjectionRetryResponse>(`/v1/matters/${matterId}/search-operations/retryable`),
    refetchInterval: 3000,
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
  const promoteGenerationMutation = useMutation({
    mutationFn: (generationId: string) => coreApi<SearchIndexGenerationRead>(`/v1/matters/${matterId}/search-indexes/${generationId}/promote`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["search-indexes", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["search-operations", matterId] });
      toast.success("The search generation was promoted and is now active.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The search generation could not be promoted."),
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

  if (searchIndexes.isPending || searchOperations.isPending || retryableSearchOperations.isPending || overviewCounts.isPending) return <TableLoading />;
  if (searchIndexes.error || searchOperations.error || retryableSearchOperations.error || overviewCounts.error) return <QueryError message={searchIndexes.error?.message ?? searchOperations.error?.message ?? retryableSearchOperations.error?.message ?? overviewCounts.error?.message} />;
  return <SearchIndexPanel
    coreDocumentCount={overviewCounts.data.document_count}
    indexes={searchIndexes.data}
    operations={searchOperations.data}
    onRebuild={() => rebuildSearchMutation.mutateAsync().then(() => undefined)}
    rebuilding={rebuildSearchMutation.isPending}
    onConfirmReindex={(operationId) => confirmReindexMutation.mutateAsync(operationId).then(() => undefined)}
    confirmingReindex={confirmReindexMutation.isPending}
    onPromoteGeneration={(generationId) => promoteGenerationMutation.mutateAsync(generationId).then(() => undefined)}
    promotingGeneration={promoteGenerationMutation.isPending}
    onRetryFailed={() => retryFailedSearchMutation.mutateAsync().then(() => undefined)}
    retryingFailed={retryFailedSearchMutation.isPending}
    retryableOperationCount={retryableSearchOperations.data.requeued_operation_count}
    retryableDocumentCount={retryableSearchOperations.data.requeued_document_count}
  />;
}
