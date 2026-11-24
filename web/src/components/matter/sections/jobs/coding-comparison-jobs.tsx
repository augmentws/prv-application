"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Scale } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { DataTable } from "@/components/data-table";
import { useMatter } from "@/components/matter/matter-context";
import { ProgressBar } from "@/components/matter/sections/jobs/job-actions";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { MatterCodingComparisonRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export function CodingComparisonJobs() {
  const { clientId, matterId } = useMatter();
  const jobs = useQuery({
    queryKey: ["matter-coding-comparisons", matterId],
    queryFn: () => coreApi<MatterCodingComparisonRead[]>(`/v1/matters/${matterId}/coding-comparisons?limit=500`),
    refetchInterval: (query) => query.state.data?.some((job) => ["QUEUED", "RUNNING"].includes(job.status)) ? 1500 : false,
  });
  const compareHref = `/app/clients/${clientId}/matters/${matterId}/batches/comparisons`;
  const columns = useMemo<ColumnDef<MatterCodingComparisonRead>[]>(() => [
    { accessorKey: "name", header: "Comparison", cell: ({ row }) => <div><p className="font-semibold">{row.original.name}</p><p className="text-xs text-muted-foreground">{Array.isArray(row.original.configuration.review_batch_ids) ? row.original.configuration.review_batch_ids.length : 0} batches · {Array.isArray(row.original.configuration.sources) ? row.original.configuration.sources.length : 0} sources</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <div><StatusBadge status={row.original.status} />{row.original.execution ? <p className="mt-1 text-xs text-muted-foreground">{row.original.execution.stage_label} · attempt {row.original.execution.attempt_number}</p> : null}{row.original.failure_summary ? <p className="mt-1 max-w-72 truncate text-xs text-destructive" title={row.original.failure_summary}>{row.original.failure_summary}</p> : null}</div> },
    { id: "progress", header: "Progress", cell: ({ row }) => <ComparisonProgress job={row.original} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "actions", header: "", cell: () => <Button size="sm" variant="outline" asChild><Link href={compareHref}>View</Link></Button> },
  ], [compareHref]);

  if (jobs.isPending) return <TableLoading />;
  if (jobs.error) return <QueryError message={jobs.error.message} />;
  return <section className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="flex items-center gap-2 text-lg font-semibold"><Scale className="size-5 text-primary" />Coding comparisons</h2><p className="text-sm text-muted-foreground">Execution history for artifact-backed cross-batch and cross-field comparisons.</p></div><Button asChild variant="outline"><Link href={compareHref}>Compare coding</Link></Button></div>
    <DataTable columns={columns} data={jobs.data} emptyMessage="No coding comparison jobs have been created for this matter." />
  </section>;
}

function ComparisonProgress({ job }: { job: MatterCodingComparisonRead }) {
  const total = job.population_document_count;
  const completed = job.compared_document_count;
  const percent = total ? Math.min(100, Math.round((completed / total) * 100)) : job.status === "COMPLETED" ? 100 : 0;
  return <div className="min-w-40"><ProgressBar percent={percent} /><span className="text-xs tabular-nums text-muted-foreground">{completed.toLocaleString()} / {total ? total.toLocaleString() : "—"} documents</span></div>;
}
