"use client";

import { useQuery } from "@tanstack/react-query";

import { StatusBadge } from "@/components/status-badge";
import type { CollectionDeletionJobRead, MatterDeletionJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

const DELETION_POLL_INTERVAL_MS = 30_000;
const TERMINAL_DELETION_STATUSES = new Set(["COMPLETED", "FAILED"]);

const deletionQueryOptions = {
  refetchOnReconnect: false,
  refetchOnWindowFocus: false,
  refetchInterval: (query: { state: { data?: { status: string } } }) => {
    const status = query.state.data?.status;
    return status && TERMINAL_DELETION_STATUSES.has(status) ? false : DELETION_POLL_INTERVAL_MS;
  },
} as const;

export function MatterDeletionStatus({ matterId }: { matterId: string }) {
  const job = useQuery({
    queryKey: ["matter-deletion", matterId],
    queryFn: () => coreApi<MatterDeletionJobRead>(`/v1/matters/${matterId}/deletion`),
    ...deletionQueryOptions,
  });

  if (job.isPending) return <DeletionStatusFallback detail="Loading deletion status…" />;
  if (job.error) return <DeletionStatusFallback detail="Deletion status unavailable" />;
  return <DeletionStatus status={job.data.status} detail={matterDeletionDetail(job.data)} />;
}

export function CollectionDeletionStatus({ collectionId }: { collectionId: string }) {
  const job = useQuery({
    queryKey: ["collection-deletion", collectionId],
    queryFn: () => coreApi<CollectionDeletionJobRead>(`/v1/collections/${collectionId}/deletion`),
    ...deletionQueryOptions,
  });

  if (job.isPending) return <DeletionStatusFallback detail="Loading deletion status…" />;
  if (job.error) return <DeletionStatusFallback detail="Deletion status unavailable" />;
  return <DeletionStatus status={job.data.status} detail={collectionDeletionDetail(job.data)} />;
}

function DeletionStatus({ status, detail }: { status: string; detail: string }) {
  return (
    <div className="min-w-0 space-y-1">
      <StatusBadge status={status} />
      <p className="truncate text-xs tabular-nums text-muted-foreground" title={detail}>{detail}</p>
    </div>
  );
}

function DeletionStatusFallback({ detail }: { detail: string }) {
  return <DeletionStatus status="DELETING" detail={detail} />;
}

function matterDeletionDetail(job: MatterDeletionJobRead): string {
  switch (job.status) {
    case "QUEUED":
      return "Waiting to start";
    case "VALIDATING":
      return "Checking active work";
    case "DELETING_SEARCH":
      return "Removing search indexes";
    case "DELETING_ARTIFACTS":
      return `${formatCount(job.deleted_artifact_count)} of ${formatCount(job.artifact_count)} artifacts removed`;
    case "DELETING_DATABASE_ROWS":
      return `Removing ${formatCount(job.document_count)} documents`;
    case "COMPLETED":
      return `${formatCount(job.document_count)} documents removed`;
    case "FAILED":
      return job.error_message ?? "Deletion failed";
  }
}

function collectionDeletionDetail(job: CollectionDeletionJobRead): string {
  switch (job.status) {
    case "QUEUED":
      return "Waiting to start";
    case "VALIDATING":
      return "Checking references and search indexes";
    case "DELETING_DATABASE_ROWS":
      return `${formatCount(job.deleted_item_count)} of ${formatCount(job.item_count)} items removed`;
    case "DELETING_BLOBS":
      return `${formatCount(job.deleted_blob_count)} of ${formatCount(job.blob_count)} blobs removed`;
    case "COMPLETED":
      return `${formatCount(job.item_count)} items removed`;
    case "FAILED":
      return job.error_message ?? "Deletion failed";
  }
}

function formatCount(value: number): string {
  return value.toLocaleString();
}
