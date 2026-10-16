"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Bot, CheckCircle2, MessageSquareWarning, XCircle } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { FactExportDialog } from "@/components/facts/fact-export-dialog";
import { FactSearchForm, useFactFilterControls, useFactListState } from "@/components/facts/fact-filters";
import { FactStatusBadge } from "@/components/facts/fact-status-badge";
import { EmptyState, QueryError, TableLoading } from "@/components/query-state";
import { ResultPagination } from "@/components/result-pagination";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { MatterFactDetailRead, MatterFactPage, MatterFactRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { FACT_SORT_LABELS, type FactSort, factDetailHref, factFilters, formatFactDate, hasFactFilters } from "@/lib/facts";

export const FACT_PAGE_SIZE = 50;
const REFRESH_INTERVAL_MS = 15_000;
export function FactsListPanel({ clientId, matterId, canReview, canExport = false }: { clientId: string; matterId: string; canReview: boolean; canExport?: boolean }) {
  const queryClient = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const { state, update } = useFactListState(() => setSelectedIds(new Set()));
  const { issueNames, sidebar, activeFilterBar } = useFactFilterControls({ matterId, state, update });
  const filters = useMemo(() => factFilters(state), [state]);
  const offset = (state.page - 1) * FACT_PAGE_SIZE;

  const facts = useQuery({
    queryKey: ["matter-facts", matterId, filters, state.sort, offset],
    queryFn: () =>
      coreApi<MatterFactPage>(`/v1/matters/${matterId}/facts/search`, {
        method: "POST",
        body: JSON.stringify({ filters, sort: state.sort, limit: FACT_PAGE_SIZE, offset }),
      }),
    placeholderData: keepPreviousData,
    // Agent runs add and change facts while reviewers work; keep the visible page current.
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const review = useMutation({
    mutationFn: async ({ action, rows }: { action: "confirm" | "reject"; rows: MatterFactRead[] }) => {
      const results = await Promise.allSettled(
        rows.map((row) =>
          coreApi<MatterFactDetailRead>(`/v1/matters/${matterId}/facts/${row.id}/${action}`, {
            method: "POST",
            body: JSON.stringify({ lock_version: row.lock_version }),
          }),
        ),
      );
      return { action, succeeded: results.filter((result) => result.status === "fulfilled").length, failed: results.filter((result) => result.status === "rejected").length };
    },
    onSuccess: ({ action, succeeded, failed }) => {
      const verb = action === "confirm" ? "Confirmed" : "Rejected";
      if (failed) toast.error(`${verb} ${succeeded}; ${failed} could not be changed (they may have changed since loading). Refresh and try again.`);
      else toast.success(`${verb} ${succeeded} ${succeeded === 1 ? "fact" : "facts"}`);
      setSelectedIds(new Set());
      void queryClient.invalidateQueries({ queryKey: ["matter-facts", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["matter-fact-facet", matterId] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The facts could not be changed"),
  });

  const rows = facts.data?.items ?? [];
  const selectedRows = rows.filter((row) => selectedIds.has(row.id));
  const reviewable = (row: MatterFactRead) => row.status === "WORKING" || row.status === "CONFIRMED";

  const columns: ColumnDef<MatterFactRead>[] = [
    ...(canReview
      ? [
          {
            id: "select",
            size: 44,
            minSize: 44,
            header: () => (
              <input
                type="checkbox"
                aria-label="Select all facts on this page"
                className="size-4 accent-primary"
                checked={rows.length > 0 && rows.filter(reviewable).every((row) => selectedIds.has(row.id))}
                onChange={(event) =>
                  setSelectedIds(event.target.checked ? new Set(rows.filter(reviewable).map((row) => row.id)) : new Set())
                }
              />
            ),
            cell: ({ row }) => (
              <input
                type="checkbox"
                aria-label={`Select fact: ${row.original.summary}`}
                className="size-4 accent-primary"
                disabled={!reviewable(row.original)}
                checked={selectedIds.has(row.original.id)}
                onChange={(event) =>
                  setSelectedIds((current) => {
                    const next = new Set(current);
                    if (event.target.checked) next.add(row.original.id);
                    else next.delete(row.original.id);
                    return next;
                  })
                }
              />
            ),
          } satisfies ColumnDef<MatterFactRead>,
        ]
      : []),
    { id: "date", header: "Date", size: 130, cell: ({ row }) => <span className="whitespace-nowrap tabular-nums">{formatFactDate(row.original)}</span> },
    {
      id: "summary",
      header: "Summary",
      size: 420,
      cell: ({ row }) => (
        <div className="min-w-0">
          <Link href={factDetailHref(clientId, matterId, row.original.id)} className="line-clamp-2 font-medium hover:text-primary">
            {row.original.summary}
          </Link>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            {row.original.origin_workflow_run_id ? <span className="inline-flex items-center gap-1 text-agent"><Bot className="size-3" aria-hidden="true" />Agent</span> : null}
            {row.original.key_fact ? <Badge variant="accent">Key fact</Badge> : null}
            {row.original.has_contradiction ? <Badge variant="outline" className="text-conflict">Contradicted</Badge> : null}
            {row.original.pending_suggestion_count ? (
              <Badge variant="outline" className="gap-1 text-agent">
                <MessageSquareWarning className="size-3" aria-hidden="true" />
                {row.original.pending_suggestion_count} {row.original.pending_suggestion_count === 1 ? "suggestion" : "suggestions"}
              </Badge>
            ) : null}
          </div>
        </div>
      ),
    },
    { id: "owner", header: "Owner", size: 170, cell: ({ row }) => <span className="truncate">{row.original.owner?.canonical_display_name ?? "—"}</span> },
    {
      id: "issues",
      header: "Issues",
      size: 200,
      cell: ({ row }) => (
        <div className="flex flex-wrap gap-1">
          {row.original.issues.map((issue) => (
            <Badge key={`${issue.matter_analysis_task_id}:${issue.issue_key}`} variant="outline">{issueNames.get(issue.issue_key) ?? issue.issue_key}</Badge>
          ))}
        </div>
      ),
    },
    { id: "status", header: "Status", size: 120, cell: ({ row }) => <FactStatusBadge status={row.original.status} /> },
    { id: "evidence", header: "Evidence", size: 100, cell: ({ row }) => <span className="tabular-nums">{row.original.evidence_count} in {row.original.document_count} {row.original.document_count === 1 ? "doc" : "docs"}</span> },
    { id: "updated", header: "Updated", size: 120, cell: ({ row }) => <span className="whitespace-nowrap text-muted-foreground">{formatDate(row.original.updated_at)}</span> },
  ];

  return (
    <div className="grid gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
      {sidebar()}
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <FactSearchForm query={state.query} onSearch={(query) => update({ query })} />
          <Select value={state.sort} onValueChange={(value) => update({ sort: value as FactSort })}>
            <SelectTrigger className="w-52" aria-label="Sort facts"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(Object.keys(FACT_SORT_LABELS) as FactSort[]).map((sort) => <SelectItem key={sort} value={sort}>{FACT_SORT_LABELS[sort]}</SelectItem>)}
            </SelectContent>
          </Select>
          {canExport ? <FactExportDialog matterId={matterId} filters={filters} factCount={facts.data?.total} /> : null}
        </div>
        {activeFilterBar}
        {canReview && selectedRows.length ? (
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-sm" aria-label="Bulk actions">
            <span className="font-medium">{selectedRows.length} selected</span>
            <Button size="sm" variant="outline" disabled={review.isPending || !selectedRows.some((row) => row.status === "WORKING" && row.evidence_count > 0)} onClick={() => review.mutate({ action: "confirm", rows: selectedRows.filter((row) => row.status === "WORKING" && row.evidence_count > 0) })}>
              <CheckCircle2 aria-hidden="true" />Confirm
            </Button>
            <Button size="sm" variant="outline" disabled={review.isPending} onClick={() => review.mutate({ action: "reject", rows: selectedRows })}>
              <XCircle aria-hidden="true" />Reject
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelectedIds(new Set())}>Clear selection</Button>
          </div>
        ) : null}
        {facts.isPending ? <TableLoading />
          : facts.error ? <QueryError message={facts.error.message} />
          : !rows.length && !hasFactFilters(state) ? (
            <EmptyState title="No facts yet" description="Facts appear here as a fact-development run proposes them or a reviewer adds them." />
          ) : (
            <>
              <p className="text-sm text-muted-foreground">{facts.data.total.toLocaleString()} {facts.data.total === 1 ? "fact" : "facts"}</p>
              <DataTable columns={columns} data={rows} emptyMessage="No facts match these filters." />
              <ResultPagination total={facts.data.total} offset={offset} pageSize={FACT_PAGE_SIZE} disabled={facts.isFetching} onPageChange={(nextOffset) => update({ page: Math.floor(nextOffset / FACT_PAGE_SIZE) + 1 })} />
            </>
          )}
      </div>
    </div>
  );
}
