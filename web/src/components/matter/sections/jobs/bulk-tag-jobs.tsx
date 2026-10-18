"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Tags } from "lucide-react";
import { useMemo } from "react";

import { DataTable } from "@/components/data-table";
import { useMatter } from "@/components/matter/matter-context";
import { DeletableJobActions, ProgressBar, TERMINAL_JOB_STATUSES, useDeleteJob } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import type { MatterBulkTagJobRead, MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { bulkTagDisplayStatus, bulkTagIsWaiting } from "@/lib/bulk-tag-jobs";
import { formatDate } from "@/lib/format";

export function BulkTagJobs() {
  const { matterId, permissionKeys } = useMatter();
  const canDeleteJobs = permissionKeys.has("job.delete");
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const bulkTagJobs = useQuery({
    queryKey: ["matter-bulk-tag-jobs", matterId],
    queryFn: () => coreApi<MatterBulkTagJobRead[]>(`/v1/matters/${matterId}/bulk-tag-jobs?limit=100`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "SNAPSHOTTING", "RUNNING"].includes(job.status)) ? 2000 : false,
  });
  const deleteJob = useDeleteJob(["matter-bulk-tag-jobs", matterId]);
  const columns = useMemo<ColumnDef<MatterBulkTagJobRead>[]>(() => {
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
      { id: "actions", header: "", cell: ({ row }) => TERMINAL_JOB_STATUSES.includes(row.original.status) ? <DeletableJobActions canDelete={canDeleteJobs} jobType="MATTER_BULK_TAG_JOB" jobId={row.original.id} jobName="Bulk tagging job" onDelete={deleteJob} /> : null },
    ];
  }, [canDeleteJobs, definitions.data, deleteJob]);

  if (bulkTagJobs.isPending) return <TableLoading />;
  if (bulkTagJobs.error) return <QueryError message={bulkTagJobs.error.message} />;
  return <section className="space-y-4">
    <div><h2 className="flex items-center gap-2 text-lg font-semibold"><Tags className="size-5 text-primary" />Bulk tagging</h2><p className="text-sm text-muted-foreground">Coding changes applied to the frozen results of a matter search.</p></div>
    <DataTable columns={columns} data={bulkTagJobs.data} emptyMessage="No bulk tag jobs have been started for this matter." />
  </section>;
}

function BulkTagProgress({ job }: { job: MatterBulkTagJobRead }) {
  const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"].includes(job.status);
  const percent = job.matched_count ? Math.min(100, Math.round((job.processed_count / job.matched_count) * 100)) : terminal ? 100 : 0;
  return <div className="min-w-36"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{bulkTagIsWaiting(job) ? "Waiting for capacity" : `${job.processed_count.toLocaleString()} / ${job.matched_count.toLocaleString()} · ${percent}%`}</span></div>;
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
