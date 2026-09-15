"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, Eye, Search } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";

import { DataTable } from "@/components/data-table";
import { DocumentViewerDialog } from "@/components/document-viewer-dialog";
import { ActiveFilterBar, FacetSidebar, type ActiveFilter, type FacetGroup } from "@/components/faceted-filter";
import { AddToMatterDialog } from "@/components/forms/add-to-matter-dialog";
import { HelpLink } from "@/components/help-link";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import type { ClientRead, CollectionCustodianSummary, CollectionItemRead, CollectionItemSearchResponse, CollectionRead, CustodianRead, FacetValue, MatterDocumentImportCreate, MatterDocumentImportRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatBytes, formatDate } from "@/lib/format";

const PAGE_SIZE = 50;

type CollectionFacetKey = "custodians" | "file_extensions" | "record_types" | "processing_statuses";
type CollectionFacetFilters = Record<CollectionFacetKey, string[]>;

function emptyFacetFilters(): CollectionFacetFilters {
  return { custodians: [], file_extensions: [], record_types: [], processing_statuses: [] };
}

interface CollectionCustodianRow {
  id: string;
  displayName: string;
  emailAddresses: string[];
  externalReference: string | null;
  status: string | null;
  itemCount: number;
}

export function CollectionView({ clientId, collectionId }: { clientId: string; collectionId: string }) {
  const router = useRouter();
  const [tab, setTab] = useState<"documents" | "custodians">("documents");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [facetFilters, setFacetFilters] = useState<CollectionFacetFilters>(emptyFacetFilters);
  const [offset, setOffset] = useState(0);
  const [selectedItem, setSelectedItem] = useState<CollectionItemRead | null>(null);
  const [selectedCustodianIds, setSelectedCustodianIds] = useState<string[]>([]);

  const client = useQuery({
    queryKey: ["client", clientId],
    queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`),
  });
  const collection = useQuery({
    queryKey: ["collection", collectionId],
    queryFn: () => coreApi<CollectionRead>(`/v1/collections/${collectionId}`),
  });
  const matters = useQuery({
    queryKey: ["matters", clientId],
    queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`),
  });
  const custodians = useQuery({
    queryKey: ["custodians", clientId],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/clients/${clientId}/custodians`),
  });
  const searchResults = useQuery({
    queryKey: ["collection-search", collectionId, search, facetFilters, offset],
    queryFn: () => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      if (search) params.set("q", search);
      facetFilters.custodians.forEach((value) => params.append("custodian_id", value));
      facetFilters.file_extensions.forEach((value) => params.append("extension", value));
      facetFilters.record_types.forEach((value) => params.append("record_type", value));
      facetFilters.processing_statuses.forEach((value) => params.append("processing_status", value));
      return coreApi<CollectionItemSearchResponse>(`/v1/collections/${collectionId}/search?${params}`);
    },
    enabled: tab === "documents",
    placeholderData: (previousData) => previousData,
  });
  const collectionCustodianSummary = useQuery({
    queryKey: ["collection-custodians", collectionId],
    queryFn: () => coreApi<CollectionCustodianSummary[]>(`/v1/collections/${collectionId}/custodians`),
    enabled: tab === "custodians",
  });

  const custodianNames = useMemo(
    () => new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian.display_name])),
    [custodians.data],
  );
  const columns = useMemo<ColumnDef<CollectionItemRead>[]>(() => [
    {
      accessorKey: "original_filename",
      header: "File",
      cell: ({ row }) => (
        <div className="min-w-52 max-w-md">
          <p className="truncate font-semibold" title={row.original.original_filename}>{row.original.original_filename}</p>
          <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground" title={row.original.source_item_id}>{row.original.source_item_id}</p>
        </div>
      ),
    },
    {
      accessorKey: "record_type",
      header: "Type",
      cell: ({ row }) => <Badge variant="outline">{row.original.record_type.toLowerCase()}</Badge>,
    },
    {
      id: "details",
      header: "Subject / source",
      cell: ({ row }) => (
        <span className="block max-w-sm truncate text-muted-foreground" title={row.original.email?.subject ?? row.original.original_source_path ?? undefined}>
          {row.original.email?.subject || row.original.original_source_path || "—"}
        </span>
      ),
    },
    {
      id: "custodians",
      header: "Custodian",
      cell: ({ row }) => {
        const names = row.original.custodian_ids.map((id) => custodianNames.get(id) ?? "Unknown custodian");
        return <span className="block max-w-52 truncate text-muted-foreground" title={names.join(", ")}>{names.join(", ")}</span>;
      },
    },
    {
      id: "source_date",
      header: "Source date",
      cell: ({ row }) => {
        const value = row.original.source_modified_at ?? row.original.source_created_at;
        return <span className="whitespace-nowrap text-muted-foreground">{value ? formatDate(value) : "—"}</span>;
      },
    },
    {
      id: "size",
      header: "Size",
      cell: ({ row }) => <span className="whitespace-nowrap font-mono text-xs text-muted-foreground">{formatBytes(row.original.native_artifact.byte_length)}</span>,
    },
    {
      accessorKey: "processing_status",
      header: "Status",
      cell: ({ row }) => <StatusBadge status={row.original.processing_status} />,
    },
    {
      id: "actions",
      header: "",
      cell: ({ row }) => <Button variant="ghost" size="sm" onClick={() => setSelectedItem(row.original)}><Eye />Open</Button>,
    },
  ], [custodianNames]);
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
      cell: ({ row }) => <span className="font-semibold">{row.original.displayName}</span>,
    },
    {
      id: "emailAddresses",
      header: "Email",
      cell: ({ row }) => {
        const value = row.original.emailAddresses.join(", ");
        return <span className="block max-w-md truncate text-muted-foreground" title={value || undefined}>{value || "—"}</span>;
      },
    },
    {
      accessorKey: "externalReference",
      header: "Source reference",
      cell: ({ row }) => <span className="text-muted-foreground">{row.original.externalReference || "—"}</span>,
    },
    {
      accessorKey: "itemCount",
      header: "Documents",
      cell: ({ row }) => <span className="font-mono text-sm tabular-nums">{row.original.itemCount.toLocaleString()}</span>,
    },
    {
      accessorKey: "status",
      header: "Status",
      cell: ({ row }) => row.original.status ? <StatusBadge status={row.original.status} /> : <span className="text-muted-foreground">—</span>,
    },
  ], [collectionCustodians, selectedCustodianIds]);
  const facetGroups = useMemo<FacetGroup[]>(() => {
    const facets = searchResults.data?.facets;
    return [
      {
        key: "custodians",
        label: "Custodian",
        options: mapFacetOptions(facets?.custodians, (value) => formatFacetValue("custodians", value, custodianNames)),
      },
      {
        key: "file_extensions",
        label: "File extension",
        options: mapFacetOptions(facets?.file_extensions, (value) => formatFacetValue("file_extensions", value, custodianNames)),
      },
      {
        key: "record_types",
        label: "Record type",
        options: mapFacetOptions(facets?.record_types, (value) => formatFacetValue("record_types", value, custodianNames)),
      },
      {
        key: "processing_statuses",
        label: "Processing status",
        options: mapFacetOptions(facets?.processing_statuses, (value) => formatFacetValue("processing_statuses", value, custodianNames)),
      },
    ];
  }, [custodianNames, searchResults.data?.facets]);
  const activeFilters = useMemo<ActiveFilter[]>(() => {
    const filters: ActiveFilter[] = search ? [{ key: "search", label: `Search: ${search}` }] : [];
    for (const group of facetGroups) {
      for (const value of facetFilters[group.key as CollectionFacetKey]) {
        const label = group.options.find((option) => option.value === value)?.label
          ?? formatFacetValue(group.key as CollectionFacetKey, value, custodianNames);
        filters.push({ key: `${group.key}:${value}`, label: `${group.label}: ${label}` });
      }
    }
    return filters;
  }, [custodianNames, facetFilters, facetGroups, search]);

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setOffset(0);
    setSearch(searchInput.trim());
  }

  function toggleFacet(groupKey: string, value: string) {
    const key = groupKey as CollectionFacetKey;
    setOffset(0);
    setFacetFilters((current) => ({
      ...current,
      [key]: current[key].includes(value)
        ? current[key].filter((selected) => selected !== value)
        : [...current[key], value],
    }));
  }

  function clearFilters() {
    setSearchInput("");
    setSearch("");
    setOffset(0);
    setFacetFilters(emptyFacetFilters());
  }

  function removeActiveFilter(key: string) {
    if (key === "search") {
      setSearchInput("");
      setSearch("");
      setOffset(0);
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
    router.push(`/app/clients/${clientId}/matters/${matterId}?tab=jobs&job=${job.id}`);
  }

  if (client.isPending || collection.isPending) return <TableLoading />;
  if (client.error || collection.error) return <QueryError message={client.error?.message ?? collection.error?.message} />;
  if (collection.data.client_id !== clientId) return <QueryError message="This collection does not belong to the selected client." />;

  const hasFilters = Boolean(search) || Object.values(facetFilters).some((values) => values.length > 0);
  const pageStart = searchResults.data?.items.length ? offset + 1 : 0;
  const pageEnd = offset + (searchResults.data?.items.length ?? 0);

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
        <Link href="/app/clients" className="hover:text-foreground">Clients</Link>
        <ChevronRight className="size-4" />
        <Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link>
        <ChevronRight className="size-4" />
        <span aria-current="page" className="text-foreground">{collection.data.name}</span>
      </nav>
      <PageHeader
        eyebrow="Evidence collection"
        title={collection.data.name}
        description={collection.data.description || "Client-level source data and uploaded artifacts."}
        actions={<div className="flex items-center gap-2"><HelpLink topic="collections" /><StatusBadge status={collection.data.status} /></div>}
      />
      <div className="mb-6 flex gap-1 border-b" role="tablist" aria-label="Collection sections">
        <button role="tab" aria-selected={tab === "documents"} onClick={() => setTab("documents")} className={tabClass(tab === "documents")}>Documents</button>
        <button role="tab" aria-selected={tab === "custodians"} onClick={() => setTab("custodians")} className={tabClass(tab === "custodians")}>Custodians</button>
      </div>

      {tab === "documents" ? (
        <>
          <Card className="mb-5 p-4">
            <form onSubmit={applySearch} className="flex flex-col gap-3 sm:flex-row sm:items-end">
              <div className="min-w-0 flex-1 space-y-2">
                <label htmlFor="collection-search" className="text-sm font-semibold">Search documents</label>
                <div className="relative">
                  <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    id="collection-search"
                    className="pl-9"
                    value={searchInput}
                    onChange={(event) => setSearchInput(event.target.value)}
                    placeholder="Search filename or source path"
                  />
                </div>
              </div>
              <Button type="submit"><Search />Search</Button>
            </form>
          </Card>

          {searchResults.isPending ? <TableLoading /> : searchResults.error ? <QueryError message={searchResults.error.message} /> : (
            <div className="grid items-start gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
              <FacetSidebar
                groups={facetGroups}
                selected={facetFilters}
                hasFilters={hasFilters}
                onToggle={toggleFacet}
                onClear={clearFilters}
              />
              <div className="min-w-0 space-y-4">
                <ActiveFilterBar filters={activeFilters} onRemove={removeActiveFilter} />
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="text-sm font-medium">
                    {searchResults.data.total.toLocaleString()} {searchResults.data.total === 1 ? "document" : "documents"}
                  </p>
                  <div className="flex items-center gap-3">
                    {searchResults.isFetching ? <p className="text-sm text-muted-foreground">Updating…</p> : null}
                    <AddToMatterDialog
                      matters={matters.data ?? []}
                      triggerLabel="Add all matching"
                      disabled={searchResults.data.total === 0 || matters.isPending}
                      selectionDescription={`Add all ${searchResults.data.total.toLocaleString()} documents matching the current search and filters.`}
                      onAdd={(matterId) => createImportJob(matterId, {
                        source_collection_id: collectionId,
                        selection: {
                          mode: "QUERY",
                          q: search || null,
                          custodian_ids: facetFilters.custodians,
                          file_extensions: facetFilters.file_extensions,
                          record_types: facetFilters.record_types as MatterDocumentImportCreate["selection"]["record_types"],
                          processing_statuses: facetFilters.processing_statuses as MatterDocumentImportCreate["selection"]["processing_statuses"],
                          item_ids: [],
                        },
                        selection_summary: hasFilters ? "Current collection search and filters" : "All collection documents",
                      })}
                    />
                  </div>
                </div>
                <DataTable
                  columns={columns}
                  data={searchResults.data.items}
                  emptyMessage={hasFilters ? "No documents match the active search and filters." : "This collection does not contain any imported items yet."}
                />
                <div className="flex flex-col justify-between gap-3 text-sm text-muted-foreground sm:flex-row sm:items-center">
                  <p>{searchResults.data.items.length ? `Showing ${pageStart}–${pageEnd} of ${searchResults.data.total.toLocaleString()}` : "No items to show"}</p>
                  <div className="flex gap-2">
                    <Button variant="outline" size="sm" disabled={offset === 0 || searchResults.isFetching} onClick={() => setOffset((value) => Math.max(0, value - PAGE_SIZE))}>
                      <ChevronLeft />Previous
                    </Button>
                    <Button variant="outline" size="sm" disabled={pageEnd >= searchResults.data.total || searchResults.isFetching} onClick={() => setOffset((value) => value + PAGE_SIZE)}>
                      Next<ChevronRight />
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          )}
          <DocumentViewerDialog item={selectedItem} onClose={() => setSelectedItem(null)} />
        </>
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

function tabClass(active: boolean) {
  return `relative px-4 py-3 text-sm font-semibold outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${active ? "text-primary after:absolute after:inset-x-1 after:bottom-0 after:h-0.5 after:bg-accent" : "text-muted-foreground hover:text-foreground"}`;
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
