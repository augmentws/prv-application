"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";

import { useCollection } from "@/components/collection/collection-context";
import { useClientCustodians, useClientMatters, useStartCollectionImport } from "@/components/collection/sections/shared";
import { useCollectionFilters } from "@/components/collection/use-collection-filters";
import { DataTable } from "@/components/data-table";
import { DocumentViewerDialog } from "@/components/document-viewer-dialog";
import { ActiveFilterBar, DateRangeFacet, FacetSidebar, type ActiveFilter, type FacetGroup } from "@/components/faceted-filter";
import { AddToMatterDialog } from "@/components/forms/add-to-matter-dialog";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { CollectionItemRead, CollectionItemSearchResponse, FacetValue } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { COLLECTION_FACET_KEYS, type CollectionFacetKey, collectionSearchApiParams, type FileDateRange, hasCollectionFilters, toImportSelection } from "@/lib/collection-filters";
import { formatDateOnly } from "@/lib/date-only";
import { formatBytes, formatDate } from "@/lib/format";

const PAGE_SIZE = 50;

/**
 * The collection's documents: search, facets, file dates and paging. Filters, the page and the open document live
 * in the page address (see useCollectionFilters), so Back, reload and shared links restore them.
 */
export function DocumentsSection() {
  const { clientId, collectionId } = useCollection();
  const { filters, setSearch, setFileDateRange, toggleFacet: toggleFacetValue, clearFilters, setOffset, setItemId } = useCollectionFilters();
  const { search, facets: facetFilters, fileDateRange, offset } = filters;
  // The box is edited freely; Search applies it. Back or a cleared filter resets it to the applied search.
  const [searchInput, setSearchInput] = useState(search);
  const [appliedSearch, setAppliedSearch] = useState(search);
  if (appliedSearch !== search) {
    setAppliedSearch(search);
    setSearchInput(search);
  }
  // The date fields show what's typed straight away; the address follows. Back resets them, as above.
  const [dateInput, setDateInput] = useState<FileDateRange>(fileDateRange);
  const [appliedDates, setAppliedDates] = useState<FileDateRange>(fileDateRange);
  if (appliedDates.from !== fileDateRange.from || appliedDates.to !== fileDateRange.to) {
    setAppliedDates(fileDateRange);
    setDateInput(fileDateRange);
  }
  // Facet groups load their counts only once opened; that stays with the page, not the address.
  const [requestedFacets, setRequestedFacets] = useState<CollectionFacetKey[]>([]);

  const matters = useClientMatters(clientId);
  const custodians = useClientCustodians(clientId);
  const createImportJob = useStartCollectionImport(clientId);
  const searchResults = useQuery({
    queryKey: ["collection-search", collectionId, search, facetFilters, fileDateRange, offset],
    queryFn: () => {
      const params = collectionSearchApiParams(filters);
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(offset));
      return coreApi<CollectionItemSearchResponse>(`/v1/collections/${collectionId}/search?${params}`);
    },
    placeholderData: (previousData) => previousData,
  });
  const collectionFacetQueries = useQueries({
    queries: COLLECTION_FACET_KEYS.map((facet) => ({
      queryKey: ["collection-search-facet", collectionId, facet, search, facetFilters, fileDateRange],
      queryFn: () => {
        const params = collectionSearchApiParams(filters);
        return coreApi<FacetValue[]>(`/v1/collections/${collectionId}/search/facets/${facet}?${params}`);
      },
      enabled: requestedFacets.includes(facet),
      placeholderData: (previous: FacetValue[] | undefined) => previous,
    })),
  });

  const custodianNames = useMemo(
    () => new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian.display_name])),
    [custodians.data],
  );
  const columns = useMemo<ColumnDef<CollectionItemRead>[]>(() => [
    {
      accessorKey: "original_filename",
      header: "File",
      size: 260,
      minSize: 120,
      cell: ({ row }) => (
        <div className="min-w-0">
          <button
            type="button"
            className="block max-w-full truncate text-left font-semibold text-primary underline-offset-4 hover:underline focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title={row.original.original_filename}
            onClick={() => setItemId(row.original.id)}
          >
            {row.original.original_filename}
          </button>
          <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground" title={row.original.source_item_id}>{row.original.source_item_id}</p>
        </div>
      ),
    },
    {
      accessorKey: "record_type",
      header: "Type",
      size: 100,
      minSize: 80,
      cell: ({ row }) => {
        const label = row.original.record_type.toLowerCase();
        return <Badge variant="outline" className="max-w-full"><span className="min-w-0 truncate" title={label}>{label}</span></Badge>;
      },
    },
    {
      id: "details",
      header: "Subject / source",
      size: 260,
      minSize: 100,
      cell: ({ row }) => (
        <span className="block truncate text-muted-foreground" title={row.original.email?.subject ?? row.original.original_source_path ?? undefined}>
          {row.original.email?.subject || row.original.original_source_path || "—"}
        </span>
      ),
    },
    {
      id: "custodians",
      header: "Custodian",
      size: 180,
      minSize: 100,
      cell: ({ row }) => {
        const names = row.original.custodian_ids.map((id) => custodianNames.get(id) ?? "Unknown custodian");
        return <span className="block truncate text-muted-foreground" title={names.join(", ")}>{names.join(", ")}</span>;
      },
    },
    {
      id: "file_date",
      header: "File date",
      size: 140,
      minSize: 105,
      cell: ({ row }) => {
        const value = row.original.file_date;
        const label = value ? formatDate(value) : "—";
        return <span className="block truncate text-muted-foreground" title={label}>{label}</span>;
      },
    },
    {
      id: "size",
      header: "Size",
      size: 90,
      minSize: 72,
      cell: ({ row }) => {
        const label = formatBytes(row.original.native_artifact.byte_length);
        return <span className="block truncate font-mono text-xs text-muted-foreground" title={label}>{label}</span>;
      },
    },
    {
      accessorKey: "processing_status",
      header: "Status",
      size: 145,
      minSize: 90,
      cell: ({ row }) => <StatusBadge status={row.original.processing_status} />,
    },
  ], [custodianNames, setItemId]);
  const facetGroups: FacetGroup[] = COLLECTION_FACET_KEYS.map((key, index) => {
    const query = collectionFacetQueries[index];
    const values = query.data ?? [];
    const definition = {
      custodians: {
        label: "Custodian",
        options: mapFacetOptions(values, (value) => formatFacetValue("custodians", value, custodianNames)),
      },
      file_extensions: {
        label: "File extension",
        options: mapFacetOptions(values, (value) => formatFacetValue("file_extensions", value, custodianNames)),
      },
      record_types: {
        label: "Record type",
        options: mapFacetOptions(values, (value) => formatFacetValue("record_types", value, custodianNames)),
      },
      processing_statuses: {
        label: "Processing status",
        options: mapFacetOptions(values, (value) => formatFacetValue("processing_statuses", value, custodianNames)),
      },
    }[key];
    return {
      key,
      ...definition,
      loaded: query.isSuccess,
      loading: requestedFacets.includes(key) && query.isFetching,
      error: query.isError,
    };
  });
  const activeFilters = useMemo<ActiveFilter[]>(() => {
    const active: ActiveFilter[] = search ? [{ key: "search", label: `Search: ${search}` }] : [];
    if (fileDateRange.from || fileDateRange.to) {
      active.push({ key: "file_date", label: formatFileDateRange(fileDateRange) });
    }
    for (const group of facetGroups) {
      for (const value of facetFilters[group.key as CollectionFacetKey]) {
        const label = group.options.find((option) => option.value === value)?.label
          ?? formatFacetValue(group.key as CollectionFacetKey, value, custodianNames);
        active.push({ key: `${group.key}:${value}`, label: `${group.label}: ${label}` });
      }
    }
    return active;
  }, [custodianNames, facetFilters, facetGroups, fileDateRange, search]);

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setSearch(searchInput);
  }

  function toggleFacet(groupKey: string, value: string) {
    const key = groupKey as CollectionFacetKey;
    if (COLLECTION_FACET_KEYS.includes(key)) toggleFacetValue(key, value);
  }

  function requestFacet(groupKey: string) {
    const key = groupKey as CollectionFacetKey;
    if (!COLLECTION_FACET_KEYS.includes(key)) return;
    setRequestedFacets((current) => current.includes(key) ? current : [...current, key]);
  }

  function removeActiveFilter(key: string) {
    if (key === "search") {
      setSearch("");
      return;
    }
    if (key === "file_date") {
      setFileDateRange({ from: "", to: "" });
      return;
    }
    const separator = key.indexOf(":");
    toggleFacet(key.slice(0, separator), key.slice(separator + 1));
  }

  const hasFilters = hasCollectionFilters(filters);
  // The viewer opens from the address; a document that isn't on this page simply isn't shown.
  const selectedItem = filters.itemId ? searchResults.data?.items.find((item) => item.id === filters.itemId) ?? null : null;
  const pageStart = searchResults.data?.items.length ? offset + 1 : 0;
  const pageEnd = offset + (searchResults.data?.items.length ?? 0);

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center justify-end gap-2">
        <AddToMatterDialog
          matters={matters.data ?? []}
          triggerLabel="Add all matching"
          disabled={!searchResults.data?.total || matters.isPending}
          selectionDescription={`Add all ${(searchResults.data?.total ?? 0).toLocaleString()} documents matching the current search and filters.`}
          onAdd={(matterId) => createImportJob(matterId, {
            source_collection_id: collectionId,
            selection: toImportSelection(filters),
            selection_summary: hasFilters ? "Current collection search and filters" : "All collection documents",
          })}
        />
      </div>
      {searchResults.isPending ? <TableLoading /> : searchResults.error ? <QueryError message={searchResults.error.message} /> : (
        <div className="grid items-start gap-x-5 gap-y-3 lg:grid-cols-[17rem_minmax(0,1fr)]">
          <div className="order-2 flex min-h-9 items-center px-4 lg:order-none lg:col-start-1 lg:row-start-1">
            <p className="text-sm font-semibold">
              {searchResults.data.total.toLocaleString()} {searchResults.data.total === 1 ? "document" : "documents"}
            </p>
          </div>
          <div className="order-1 min-w-0 space-y-3 lg:order-none lg:col-start-2 lg:row-start-1">
            <form onSubmit={applySearch} className="flex min-h-9 flex-col gap-2 sm:flex-row sm:items-center">
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  id="collection-search"
                  aria-label="Search collection"
                  className="h-9 pl-9"
                  value={searchInput}
                  onChange={(event) => setSearchInput(event.target.value)}
                  placeholder="Search filename or source path"
                />
              </div>
              <Button type="submit" size="sm" className="h-9"><Search />Search</Button>
            </form>
            <ActiveFilterBar filters={activeFilters} onRemove={removeActiveFilter} />
            {searchResults.isFetching ? <p className="text-sm text-muted-foreground">Updating…</p> : null}
          </div>
          <div className="order-3 min-w-0 lg:order-none lg:col-start-1 lg:row-start-2">
            <FacetSidebar
              groups={facetGroups}
              selected={facetFilters}
              hasFilters={hasFilters}
              onToggle={toggleFacet}
              onOpen={requestFacet}
              onClear={clearFilters}
            >
              <DateRangeFacet
                from={dateInput.from}
                to={dateInput.to}
                onChange={(range) => {
                  setDateInput(range);
                  setFileDateRange(range, "replace");
                }}
              />
            </FacetSidebar>
          </div>
          <div className="order-4 min-w-0 space-y-4 lg:order-none lg:col-start-2 lg:row-start-2">
            <DataTable
              columns={columns}
              data={searchResults.data.items}
              emptyMessage={hasFilters ? "No documents match the active search and filters." : "This collection does not contain any imported items yet."}
              fitToWidth
            />
            <div className="flex flex-col justify-between gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center">
              <p>{searchResults.data.items.length ? `Showing ${pageStart}–${pageEnd} of ${searchResults.data.total.toLocaleString()}` : "No items to show"}</p>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={offset === 0 || searchResults.isFetching} onClick={() => setOffset(offset - PAGE_SIZE)}>
                  <ChevronLeft />Previous
                </Button>
                <Button variant="outline" size="sm" disabled={pageEnd >= searchResults.data.total || searchResults.isFetching} onClick={() => setOffset(offset + PAGE_SIZE)}>
                  Next<ChevronRight />
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
      <DocumentViewerDialog item={selectedItem} onClose={() => setItemId(null)} showUnmappedMetadata />
    </>
  );
}

function mapFacetOptions(values: FacetValue[] | undefined, label: (value: string) => string) {
  return (values ?? []).map((item) => ({ ...item, label: label(item.value) }));
}

function formatFacetLabel(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function formatFacetValue(key: CollectionFacetKey, value: string, custodianNames: Map<string, string>) {
  if (key === "custodians") return custodianNames.get(value) ?? "Unknown custodian";
  if (key === "file_extensions") return value === "__none__" ? "No extension" : `.${value}`;
  return formatFacetLabel(value);
}

function formatFileDateRange(range: FileDateRange) {
  const from = range.from ? formatDateOnly(range.from) : "Any time";
  const to = range.to ? formatDateOnly(range.to) : "Any time";
  return `File date: ${from} – ${to}`;
}
