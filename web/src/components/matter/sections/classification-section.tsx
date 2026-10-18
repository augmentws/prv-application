"use client";

import { useQuery } from "@tanstack/react-query";

import { ClassificationPanel } from "@/components/classification-panel";
import { useMatter } from "@/components/matter/matter-context";
import { QueryError, TableLoading } from "@/components/query-state";
import type { MatterEmbeddingJobRead, MetadataDefinitionRead, ReviewBatchRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

export function ClassificationSection() {
  const { clientId, matterId } = useMatter();
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const reviewBatches = useQuery({
    queryKey: ["review-batches", matterId],
    queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`),
    refetchInterval: (query) => query.state.data?.some((batch) => ["QUEUED", "BUILDING"].includes(batch.status)) ? 2000 : false,
  });
  const embeddingJobs = useQuery({
    queryKey: ["matter-embedding-jobs", matterId],
    queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "PLANNING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });

  if (reviewBatches.isPending || embeddingJobs.isPending || definitions.isPending) return <TableLoading />;
  if (reviewBatches.error || embeddingJobs.error || definitions.error) return <QueryError message={reviewBatches.error?.message ?? embeddingJobs.error?.message ?? definitions.error?.message} />;
  return <ClassificationPanel clientId={clientId} matterId={matterId} definitions={definitions.data} reviewBatches={reviewBatches.data} embeddingJobs={embeddingJobs.data} />;
}
