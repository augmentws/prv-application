"use client";

import { keepPreviousData, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Bot, CheckCircle2, MessageSquareWarning, Search, XCircle } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { FactStatusBadge } from "@/components/facts/fact-status-badge";
import { ActiveFilterBar, DateRangeFacet, type FacetGroup, FacetSidebar } from "@/components/faceted-filter";
import { EmptyState, QueryError, TableLoading } from "@/components/query-state";
import { ResultPagination } from "@/components/result-pagination";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { FactIssueCatalogEntry, MatterEntityRead, MatterFactDetailRead, MatterFactFacetRead, MatterFactPage, MatterFactRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import {
  FACT_FACETS,
  FACT_SORT_LABELS,
  FACT_STATUS_LABELS,
  type FactFacetKey,
  type FactListState,
  type FactSort,
  factDetailHref,
  factFilters,
  formatFactDate,
  hasFactFilters,
  readFactListState,
  writeFactListState,
} from "@/lib/facts";

export const FACT_PAGE_SIZE = 50;
const REFRESH_INTERVAL_MS = 15_000;
const BOOLEAN_LABELS: Record<string, string> = { true: "Yes", false: "No" };
const AUTHOR_LABELS: Record<string, string> = { HUMAN: "Reviewer", AGENT: "Agent" };
const PRECISION_LABELS: Record<string, string> = {
  DAY: "Day",
  MONTH: "Month",
  QUARTER: "Quarter",
  YEAR: "Year",
  RANGE: "Range",
  UNKNOWN: "Undated",
};

function bucketLabel(facet: FactFacetKey, value: string, label: string) {
  if (facet === "status") return FACT_STATUS_LABELS[value] ?? label;
  if (facet === "key_fact" || facet === "has_contradiction") return BOOLEAN_LABELS[value] ?? label;
  if (facet === "author_type") return AUTHOR_LABELS[value] ?? label;
  if (facet === "date_precision") return PRECISION_LABELS[value] ?? label;
  return label;
}

export function useIssueNames(matterId: string) {
  const catalog = useQuery({
    queryKey: ["fact-issue-catalog", matterId],
    queryFn: () => coreApi<FactIssueCatalogEntry[]>(`/v1/matters/${matterId}/facts/issue-catalog`),
    staleTime: 60_000,
  });
  const names = useMemo(
    () => new Map((catalog.data ?? []).map((entry) => [entry.issue_key, entry.name])),
    [catalog.data],
  );
  return { catalog, names };
}

export function FactsListPanel({ clientId, matterId, canReview }: { clientId: string; matterId: string; canReview: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const state = useMemo(() => readFactListState(new URLSearchParams(searchParams.toString())), [searchParams]);
  const [queryDraft, setQueryDraft] = useState(state.query);
  const [openFacets, setOpenFacets] = useState<Set<FactFacetKey>>(
    () => new Set(FACT_FACETS.filter((facet) => state.selected[facet.key]?.length).map((facet) => facet.key)),
  );
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const { names: issueNames } = useIssueNames(matterId);
  const filterEntity = useQuery({
    queryKey: ["matter-entity", matterId, state.entityId],
    queryFn: () => coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities/${state.entityId}`),
    enabled: Boolean(state.entityId),
    retry: false,
  });
  const filters = useMemo(() => factFilters(state), [state]);
  const offset = (state.page - 1) * FACT_PAGE_SIZE;

  function update(patch: Partial<FactListState>) {
    const next: FactListState = { ...state, page: 1, ...patch };
    const params = writeFactListState(new URLSearchParams(searchParams.toString()), next);
    setSelectedIds(new Set());
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

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

  const facetResults = useQueries({
    queries: FACT_FACETS.map((facet) => {
      const facetFilters = factFilters(state, { without: facet.key });
      return {
        queryKey: ["matter-fact-facet", matterId, facet.key, facetFilters],
        queryFn: () =>
          coreApi<MatterFactFacetRead>(`/v1/matters/${matterId}/facts/facets`, {
            method: "POST",
            body: JSON.stringify({ filters: facetFilters, facet: facet.key }),
          }),
        enabled: openFacets.has(facet.key),
        placeholderData: keepPreviousData,
      };
    }),
  });

  const groups: FacetGroup[] = FACT_FACETS.map((facet, index) => {
    const result = facetResults[index];
    return {
      key: facet.key,
      label: facet.label,
      loaded: result.isSuccess,
      loading: result.isPending && openFacets.has(facet.key),
      error: result.isError,
      options: (result.data?.buckets ?? []).map((bucket) => {
        const value = String(bucket.value);
        return { value, label: bucketLabel(facet.key, value, bucket.label), count: bucket.count };
      }),
    };
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

  const activeFilters = [
    ...(state.query ? [{ key: "query", label: `Search: ${state.query}` }] : []),
    ...FACT_FACETS.flatMap((facet) =>
      (state.selected[facet.key] ?? []).map((value) => ({
        key: `${facet.key}:${value}`,
        label: `${facet.label}: ${groups.find((group) => group.key === facet.key)?.options.find((option) => option.value === value)?.label ?? (facet.key === "issue_key" ? issueNames.get(value) ?? value : bucketLabel(facet.key, value, value))}`,
      })),
    ),
    ...(state.dateFrom || state.dateTo ? [{ key: "dates", label: `Date: ${state.dateFrom || "…"} to ${state.dateTo || "…"}` }] : []),
    ...(state.pendingOnly ? [{ key: "pending", label: "Has pending suggestions" }] : []),
    ...(state.entityId ? [{ key: "entity", label: `Involving: ${filterEntity.data?.display_name ?? "selected person or organization"}` }] : []),
  ];

  function removeFilter(key: string) {
    if (key === "query") {
      setQueryDraft("");
      update({ query: "" });
    } else if (key === "dates") update({ dateFrom: "", dateTo: "" });
    else if (key === "pending") update({ pendingOnly: false });
    else if (key === "entity") update({ entityId: "" });
    else {
      const separator = key.indexOf(":");
      const facetKey = key.slice(0, separator) as FactFacetKey;
      const value = key.slice(separator + 1);
      update({ selected: { ...state.selected, [facetKey]: (state.selected[facetKey] ?? []).filter((item) => item !== value) } });
    }
  }

  function toggleFacet(groupKey: string, value: string) {
    const key = groupKey as FactFacetKey;
    const current = state.selected[key] ?? [];
    update({ selected: { ...state.selected, [key]: current.includes(value) ? current.filter((item) => item !== value) : [...current, value] } });
  }

  function submitQuery(event: FormEvent) {
    event.preventDefault();
    update({ query: queryDraft });
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
      <FacetSidebar
        title="Filter facts"
        groups={groups}
        selected={state.selected as Record<string, string[]>}
        hasFilters={hasFactFilters(state)}
        onToggle={toggleFacet}
        onOpen={(key) => setOpenFacets((current) => new Set(current).add(key as FactFacetKey))}
        onClear={() => {
          setQueryDraft("");
          update({ query: "", selected: {}, dateFrom: "", dateTo: "", pendingOnly: false, entityId: "" });
        }}
      >
        <DateRangeFacet label="Event date" from={state.dateFrom} to={state.dateTo} onChange={({ from, to }) => update({ dateFrom: from, dateTo: to })} />
        <label className="flex cursor-pointer items-center gap-2 p-4 text-sm font-semibold">
          <input type="checkbox" className="size-4 accent-primary" checked={state.pendingOnly} onChange={(event) => update({ pendingOnly: event.target.checked })} />
          Has pending suggestions
        </label>
      </FacetSidebar>
      <div className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <form onSubmit={submitQuery} className="flex min-w-0 flex-1 items-center gap-2" role="search">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
              <Input value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="Search summaries and quotes" aria-label="Search facts" className="pl-9" />
            </div>
            <Button type="submit" variant="outline">Search</Button>
          </form>
          <Select value={state.sort} onValueChange={(value) => update({ sort: value as FactSort })}>
            <SelectTrigger className="w-52" aria-label="Sort facts"><SelectValue /></SelectTrigger>
            <SelectContent>
              {(Object.keys(FACT_SORT_LABELS) as FactSort[]).map((sort) => <SelectItem key={sort} value={sort}>{FACT_SORT_LABELS[sort]}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
        <ActiveFilterBar filters={activeFilters} onRemove={removeFilter} />
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
