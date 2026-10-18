"use client";

import { keepPreviousData, useQueries, useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, type ReactNode, useMemo, useState } from "react";

import { type ActiveFilter, ActiveFilterBar, DateRangeFacet, type FacetGroup, FacetSidebar } from "@/components/faceted-filter";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { FactFilters, FactIssueCatalogEntry, MatterEntityRead, MatterFactFacetRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import {
  FACT_FACETS,
  FACT_PRECISION_LABELS,
  FACT_STATUS_LABELS,
  type FactFacetKey,
  type FactListState,
  factFilters,
  hasFactFilters,
  readFactListState,
  writeFactListState,
} from "@/lib/facts";

const BOOLEAN_LABELS: Record<string, string> = { true: "Yes", false: "No" };
const AUTHOR_LABELS: Record<string, string> = { HUMAN: "Reviewer", AGENT: "Agent" };

function bucketLabel(facet: FactFacetKey, value: string, label: string) {
  if (facet === "status") return FACT_STATUS_LABELS[value] ?? label;
  if (facet === "key_fact" || facet === "has_contradiction") return BOOLEAN_LABELS[value] ?? label;
  if (facet === "author_type") return AUTHOR_LABELS[value] ?? label;
  if (facet === "date_precision") return FACT_PRECISION_LABELS[value] ?? label;
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

/** The Facts tab's filter state, read from and written to the URL so the List and Timeline share it. */
export function useFactListState(onUpdate?: () => void) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const state = useMemo(() => readFactListState(new URLSearchParams(searchParams.toString())), [searchParams]);

  function update(patch: Partial<FactListState>) {
    const next: FactListState = { ...state, page: 1, ...patch };
    const params = writeFactListState(new URLSearchParams(searchParams.toString()), next);
    onUpdate?.();
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  return { state, update };
}

/**
 * The filter sidebar, search box and active-filter chips shared by the Facts list and timeline. `baseFilters` turns the
 * state into the request filter, so the timeline can apply its own status rule; facets in `hiddenFacets` are not offered.
 */
export function useFactFilterControls({
  matterId,
  state,
  update,
  baseFilters = factFilters,
  hiddenFacets = [],
}: {
  matterId: string;
  state: FactListState;
  update: (patch: Partial<FactListState>) => void;
  baseFilters?: (state: FactListState) => FactFilters;
  hiddenFacets?: FactFacetKey[];
}) {
  const facets = FACT_FACETS.filter((facet) => !hiddenFacets.includes(facet.key));
  const [openFacets, setOpenFacets] = useState<Set<FactFacetKey>>(
    () => new Set(facets.filter((facet) => state.selected[facet.key]?.length).map((facet) => facet.key)),
  );
  const { names: issueNames } = useIssueNames(matterId);
  const filterEntity = useQuery({
    queryKey: ["matter-entity", matterId, state.entityId],
    queryFn: () => coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities/${state.entityId}`),
    enabled: Boolean(state.entityId),
    retry: false,
  });

  const facetResults = useQueries({
    queries: facets.map((facet) => {
      // Self-excluding counts: each facet ignores its own selection.
      const facetFilters = baseFilters({ ...state, selected: { ...state.selected, [facet.key]: [] } });
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

  const groups: FacetGroup[] = facets.map((facet, index) => {
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

  const activeFilters: ActiveFilter[] = [
    ...(state.query ? [{ key: "query", label: `Search: ${state.query}` }] : []),
    ...facets.flatMap((facet) =>
      (state.selected[facet.key] ?? []).map((value) => ({
        key: `${facet.key}:${value}`,
        label: `${facet.label}: ${groups.find((group) => group.key === facet.key)?.options.find((option) => option.value === value)?.label ?? (facet.key === "issue_key" ? issueNames.get(value) ?? value : bucketLabel(facet.key, value, value))}`,
      })),
    ),
    ...(state.dateFrom || state.dateTo ? [{ key: "dates", label: `Date: ${state.dateFrom || "…"} to ${state.dateTo || "…"}` }] : []),
    ...(state.pendingOnly ? [{ key: "pending", label: "Has pending suggestions" }] : []),
    ...(state.entityId ? [{ key: "entity", label: `Involving: ${filterEntity.data?.display_name ?? "selected person or organization"}` }] : []),
  ];

  const clearAll: Partial<FactListState> = { query: "", selected: {}, dateFrom: "", dateTo: "", pendingOnly: false, entityId: "" };
  const shownState = { ...state, selected: Object.fromEntries(facets.map((facet) => [facet.key, state.selected[facet.key] ?? []])) };

  function removeFilter(key: string) {
    if (key === "query") update({ query: "" });
    else if (key === "dates") update({ dateFrom: "", dateTo: "" });
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

  const sidebar = (children?: ReactNode) => (
    <FacetSidebar
      title="Filter facts"
      groups={groups}
      selected={shownState.selected as Record<string, string[]>}
      hasFilters={hasFactFilters(shownState)}
      onToggle={toggleFacet}
      onOpen={(key) => setOpenFacets((current) => new Set(current).add(key as FactFacetKey))}
      onClear={() => update(clearAll)}
    >
      <DateRangeFacet label="Event date" from={state.dateFrom} to={state.dateTo} onChange={({ from, to }) => update({ dateFrom: from, dateTo: to })} />
      <label className="flex cursor-pointer items-center gap-2 p-4 text-sm font-semibold">
        <input type="checkbox" className="size-4 accent-primary" checked={state.pendingOnly} onChange={(event) => update({ pendingOnly: event.target.checked })} />
        Has pending suggestions
      </label>
      {children}
    </FacetSidebar>
  );

  const activeFilterBar = <ActiveFilterBar filters={activeFilters} onRemove={removeFilter} />;

  return { issueNames, sidebar, activeFilterBar, hasFilters: hasFactFilters(shownState) };
}

/** The keyword search box for facts; it keys on the applied query so clearing a filter chip clears the box. */
export function FactSearchForm({ query, onSearch }: { query: string; onSearch: (query: string) => void }) {
  return <FactSearchFormInner key={query} query={query} onSearch={onSearch} />;
}

function FactSearchFormInner({ query, onSearch }: { query: string; onSearch: (query: string) => void }) {
  const [draft, setDraft] = useState(query);
  function submit(event: FormEvent) {
    event.preventDefault();
    onSearch(draft);
  }
  return (
    <form onSubmit={submit} className="flex min-w-0 flex-1 items-center gap-2" role="search">
      <div className="relative min-w-0 flex-1">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <Input value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Search summaries and quotes" aria-label="Search facts" className="pl-9" />
      </div>
      <Button type="submit" variant="outline">Search</Button>
    </form>
  );
}
