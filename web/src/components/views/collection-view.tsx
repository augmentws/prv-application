"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";

import { DataTable } from "@/components/data-table";
import { DateHistogram, type DateHistogramInterval } from "@/components/date-histogram";
import { useCollection } from "@/components/collection/collection-context";
import { useCollectionFilters } from "@/components/collection/use-collection-filters";
import { CollectionProcessingPanel } from "@/components/collection-processing-panel";
import { DocumentViewerDialog } from "@/components/document-viewer-dialog";
import { ActiveFilterBar, DateRangeFacet, FacetSidebar, type ActiveFilter, type FacetGroup } from "@/components/faceted-filter";
import { AddToMatterDialog } from "@/components/forms/add-to-matter-dialog";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { CollectionCustodianSummary, CollectionDateHistogramResponse, CollectionItemRead, CollectionItemSearchResponse, CustodianRead, FacetValue, MatterDocumentImportCreate, MatterDocumentImportRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDateOnly } from "@/lib/date-only";
import { formatBytes, formatDate } from "@/lib/format";
import { matterJobsHref } from "@/lib/matter-routes";
import { COLLECTION_FACET_KEYS, type CollectionFacetKey, collectionSearchApiParams, type FileDateRange, hasCollectionFilters, toImportSelection } from "@/lib/collection-filters";
import { type CollectionSection, collectionHref } from "@/lib/collection-routes";

const PAGE_SIZE = 50;

interface CollectionCustodianRow {
  id: string;
  displayName: string;
  emailAddresses: string[];
  externalReference: string | null;
  status: string | null;
  itemCount: number;
}

/**
 * The collection's sections, rendered one per route inside CollectionShell, which draws the header,
 * status and delete action. Documents filters, paging and the open document live in the page address
 * (see useCollectionFilters), so Back, reload and shared links restore them.
 */
export function CollectionView({ section }: { section: CollectionSection }) {
  const { clientId, collectionId } = useCollection();
  const router = useRouter();
  const tab = section;
  const { filters, setSearch, setFileDateRange, toggleFacet: toggleFacetValue, clearFilters, setOffset, setItemId } = useCollectionFilters();
  const { search, facets: facetFilters, fileDateRange, offset } = filters;
  const [histogramInterval, setHistogramInterval] = useState<DateHistogramInterval>("month");
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
  const [selectedCustodianIds, setSelectedCustodianIds] = useState<string[]>([]);

  const matters = useQuery({
    queryKey: ["matters", clientId],
    queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`),
  });
  const custodians = useQuery({
    queryKey: ["custodians", clientId],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/clients/${clientId}/custodians`),
  });
  const searchResults = useQuery({
    queryKey: ["collection-search", collectionId, search, facetFilters, fileDateRange, offset],
    queryFn: () => {
      const params = collectionSearchApiParams(filters);
      params.set("limit", String(PAGE_SIZE));
      params.set("offset", String(offset));
      return coreApi<CollectionItemSearchResponse>(`/v1/collections/${collectionId}/search?${params}`);
    },
    enabled: tab === "documents",
    placeholderData: (previousData) => previousData,
  });
  const collectionFacetQueries = useQueries({
    queries: COLLECTION_FACET_KEYS.map((facet) => ({
      queryKey: ["collection-search-facet", collectionId, facet, search, facetFilters, fileDateRange],
      queryFn: () => {
        const params = collectionSearchApiParams(filters);
        return coreApi<FacetValue[]>(`/v1/collections/${collectionId}/search/facets/${facet}?${params}`);
      },
      enabled: tab === "documents" && requestedFacets.includes(facet),
      placeholderData: (previous: FacetValue[] | undefined) => previous,
    })),
  });
  const collectionCustodianSummary = useQuery({
    queryKey: ["collection-custodians", collectionId],
    queryFn: () => coreApi<CollectionCustodianSummary[]>(`/v1/collections/${collectionId}/custodians`),
    enabled: tab === "custodians",
  });
  const collectionDateHistogram = useQuery({
    queryKey: ["collection-date-histogram", collectionId, histogramInterval],
    queryFn: () => coreApi<CollectionDateHistogramResponse>(`/v1/collections/${collectionId}/date-histogram?interval=${histogramInterval}`),
    enabled: tab === "file-dates",
    placeholderData: (previous) => previous,
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
  const collectionCustodians = useMemo<CollectionCustodianRow[]>(() => {
    const detailsById = new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian]));
    return (collectionCustodianSummary.data ?? [])
      .map((summary) => {
        const details = detailsById.get(summary.custodian_id);
        return {
          id: summary.custodian_id,
          displayName: details?.display_name ?? "Unknown custodian",
          emailAddresses: details?.email_addresses ?? [],
          externalReference: details?.external_reference ?? null,
          status: details?.status ?? null,
          itemCount: summary.item_count,
        };
      })
      .sort((left, right) => left.displayName.localeCompare(right.displayName));
  }, [collectionCustodianSummary.data, custodians.data]);
  const custodianColumns = useMemo<ColumnDef<CollectionCustodianRow>[]>(() => [
    {
      id: "select",
      size: 52,
      minSize: 52,
      maxSize: 52,
      enableResizing: false,
      header: () => (
        <input
          type="checkbox"
          aria-label="Select every custodian"
          checked={collectionCustodians.length > 0 && selectedCustodianIds.length === collectionCustodians.length}
          onChange={(event) => setSelectedCustodianIds(event.target.checked ? collectionCustodians.map((row) => row.id) : [])}
          className="size-4 rounded border-input accent-primary"
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label={`Select ${row.original.displayName}`}
          checked={selectedCustodianIds.includes(row.original.id)}
          onChange={(event) => setSelectedCustodianIds((current) => event.target.checked ? [...current, row.original.id] : current.filter((id) => id !== row.original.id))}
          className="size-4 rounded border-input accent-primary"
        />
      ),
    },
    {
      accessorKey: "displayName",
      header: "Custodian",
      size: 210,
      cell: ({ row }) => <span className="font-semibold">{row.original.displayName}</span>,
    },
    {
      id: "emailAddresses",
      header: "Email",
      size: 280,
      cell: ({ row }) => {
        const value = row.original.emailAddresses.join(", ");
        return <span className="block max-w-md truncate text-muted-foreground" title={value || undefined}>{value || "—"}</span>;
      },
    },
    {
      accessorKey: "externalReference",
      header: "Source reference",
      size: 180,
      cell: ({ row }) => <span className="text-muted-foreground">{row.original.externalReference || "—"}</span>,
    },
    {
      accessorKey: "itemCount",
      header: "Documents",
      size: 110,
      cell: ({ row }) => <span className="font-mono text-sm tabular-nums">{row.original.itemCount.toLocaleString()}</span>,
    },
    {
      accessorKey: "status",
      header: "Status",
      size: 130,
      cell: ({ row }) => row.original.status ? <StatusBadge status={row.original.status} /> : <span className="text-muted-foreground">—</span>,
    },
  ], [collectionCustodians, selectedCustodianIds]);
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

  async function createImportJob(matterId: string, payload: MatterDocumentImportCreate) {
    const job = await coreApi<MatterDocumentImportRead>(`/v1/matters/${matterId}/document-imports`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    router.push(matterJobsHref(clientId, matterId, "DOCUMENT_IMPORTS", { job: job.id }));
  }


  const hasFilters = hasCollectionFilters(filters);
  // The viewer opens from the address; a document that isn't on this page simply isn't shown.
  const selectedItem = filters.itemId ? searchResults.data?.items.find((item) => item.id === filters.itemId) ?? null : null;
  const pageStart = searchResults.data?.items.length ? offset + 1 : 0;
  const pageEnd = offset + (searchResults.data?.items.length ?? 0);

  return (
    <>
      {tab === "documents" ? (
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
      ) : null}

      {tab === "documents" ? (
        <>
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
      ) : tab === "file-dates" ? (
        <Card className="p-5">
          <DateHistogram
            buckets={collectionDateHistogram.data?.buckets ?? []}
            interval={histogramInterval}
            onIntervalChange={setHistogramInterval}
            loading={collectionDateHistogram.isPending}
            error={Boolean(collectionDateHistogram.error)}
            onSelect={(range) => {
              const params: Record<string, string> = {};
              if (range.from) params.from = range.from;
              if (range.to) params.to = range.to;
              router.push(collectionHref(clientId, collectionId, "documents", params));
            }}
          />
          {collectionDateHistogram.data?.missing_count ? (
            <p className="mt-4 text-sm text-muted-foreground">
              {collectionDateHistogram.data.missing_count.toLocaleString()} documents do not have a file date.
            </p>
          ) : null}
        </Card>
      ) : tab === "processing" ? (
        <CollectionProcessingPanel collectionId={collectionId} />
      ) : custodians.isPending || collectionCustodianSummary.isPending ? (
        <TableLoading />
      ) : custodians.error || collectionCustodianSummary.error ? (
        <QueryError message={custodians.error?.message ?? collectionCustodianSummary.error?.message} />
      ) : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              {selectedCustodianIds.length ? `${selectedCustodianIds.length.toLocaleString()} selected` : "Select custodians to add all of their documents."}
            </p>
            <AddToMatterDialog
              matters={matters.data ?? []}
              triggerLabel="Add documents to matter"
              disabled={!selectedCustodianIds.length || matters.isPending}
              selectionDescription={`Add documents associated with ${selectedCustodianIds.length.toLocaleString()} selected ${selectedCustodianIds.length === 1 ? "custodian" : "custodians"}.`}
              onAdd={(matterId) => createImportJob(matterId, {
                source_collection_id: collectionId,
                selection: {
                  mode: "QUERY",
                  q: null,
                  custodian_ids: selectedCustodianIds,
                  file_extensions: [],
                  record_types: [],
                  processing_statuses: [],
                  item_ids: [],
                },
                selection_summary: `${selectedCustodianIds.length} selected ${selectedCustodianIds.length === 1 ? "custodian" : "custodians"}`,
              })}
            />
          </div>
          <DataTable
            columns={custodianColumns}
            data={collectionCustodians}
            emptyMessage="No custodians are represented in this collection yet."
          />
        </div>
      )}
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
