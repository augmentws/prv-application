"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FileSearch,
  FileText,
  Filter,
  GripVertical,
  PanelRight,
  RotateCcw,
  Save,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type CSSProperties, type FormEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { BrandMark } from "@/components/brand-mark";
import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import { HelpLink } from "@/components/help-link";
import { QueryError } from "@/components/query-state";
import { SavedSearchesDialog, SaveSearchDialog } from "@/components/saved-search-dialogs";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type {
  ClientRead,
  CollectionItemRead,
  CustodianRead,
  DocumentMetadataFieldRead,
  MatterRead,
  MatterSavedSearchCreate,
  MatterSavedSearchRead,
  MatterFacetValuesResponse,
  MatterSearchFilter,
  MatterSearchHit,
  MatterSearchRequest,
  MatterSearchRequestSearchMode,
  MatterSearchResponse,
  MetadataDefinitionRead,
  MetadataEventCreate,
  MetadataGroupRead,
  MetadataMutationRead,
  SearchProjectionOperationRead,
  UserRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;
const PREFERRED_FACETS = ["custodian", "file_extension", "responsiveness", "privilege", "key_document"];
const SEARCH_PROJECTION_POLL_MS = 500;
const SEARCH_PROJECTION_POLL_ATTEMPTS = 60;
const SEARCH_PLACEHOLDERS: Record<MatterSearchRequestSearchMode, string> = {
  KEYWORD: "Search document body, filenames, paths, email headers, and metadata",
  SEMANTIC: "Find documents by concept or meaning, not only exact words",
  HYBRID: "Combine exact words with conceptually related results",
};

type SelectedFilters = Record<string, string[]>;

interface CodingAction {
  definitionId: string;
  payload: MetadataEventCreate;
}

interface ReviewWorkspaceProps {
  matterId: string;
  initialQuery?: string;
  initialFilters?: SelectedFilters;
  initialDocumentId?: string;
  initialPage?: number;
  initialSearchMode?: MatterSearchRequestSearchMode;
}

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function resultMetadata(hit: MatterSearchHit | undefined) {
  return objectValue(hit?.fields.metadata);
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join(", ") : "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function facetToken(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function typedFacetValue(token: string, definition: MetadataDefinitionRead): unknown {
  if (definition.type === "BOOLEAN") return token === "true";
  if (definition.type === "INTEGER" || definition.type === "DECIMAL") return Number(token);
  return token;
}

function wait(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function resultTitle(hit: MatterSearchHit) {
  const metadata = resultMetadata(hit);
  return displayValue(hit.fields.email_subject || metadata.document_title || hit.fields.original_filename || "Untitled document");
}

function resultFileType(hit: MatterSearchHit) {
  const metadata = resultMetadata(hit);
  const extension = displayValue(metadata.file_extension);
  return extension === "—" ? displayValue(hit.fields.record_type) : extension.replace(/^\./, "").toUpperCase();
}

function parseEditorValue(value: string, definition: MetadataDefinitionRead): unknown {
  if (definition.type === "INTEGER") {
    const parsed = Number(value);
    if (!Number.isSafeInteger(parsed)) throw new Error("Enter a whole number.");
    return parsed;
  }
  if (definition.type === "DECIMAL") {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error("Enter a number.");
    return parsed;
  }
  if (definition.type === "BOOLEAN") return value === "true";
  if (definition.type === "JSON") {
    const parsed = JSON.parse(value) as unknown;
    if (parsed === null || typeof parsed !== "object") throw new Error("Enter a JSON object or array.");
    return parsed;
  }
  if (definition.type === "DATETIME") {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) throw new Error("Enter a date and time.");
    return parsed.toISOString();
  }
  return value;
}

function editorValue(value: unknown, definition: MetadataDefinitionRead) {
  if (value === null || value === undefined) return "";
  if (definition.type === "JSON") return JSON.stringify(value, null, 2);
  if (definition.type === "DATETIME") {
    const date = new Date(String(value));
    if (!Number.isNaN(date.valueOf())) return date.toISOString().slice(0, 16);
  }
  return String(value);
}

export function ReviewWorkspace({
  matterId,
  initialQuery = "",
  initialFilters = {},
  initialDocumentId,
  initialPage = 1,
  initialSearchMode = "KEYWORD",
}: ReviewWorkspaceProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [draftQuery, setDraftQuery] = useState(initialQuery);
  const [query, setQuery] = useState(initialQuery);
  const [searchMode, setSearchMode] = useState<MatterSearchRequestSearchMode>(initialSearchMode);
  const [filters, setFilters] = useState<SelectedFilters>(initialFilters);
  const [offset, setOffset] = useState(Math.max(0, initialPage - 1) * PAGE_SIZE);
  const [selectedDocumentId, setSelectedDocumentId] = useState(initialDocumentId ?? "");
  const [mobileDocumentOpen, setMobileDocumentOpen] = useState(Boolean(initialDocumentId));
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [codingOpen, setCodingOpen] = useState(false);
  const [facetWidth, setFacetWidth] = useState(248);
  const [resultsWidth, setResultsWidth] = useState(384);
  const [detailsWidth, setDetailsWidth] = useState(352);

  const refreshAfterSearchProjection = useCallback(async (operationIds: string[]) => {
    const pendingIds = new Set(operationIds);
    try {
      for (let attempt = 0; attempt < SEARCH_PROJECTION_POLL_ATTEMPTS; attempt += 1) {
        const operations = await coreApi<SearchProjectionOperationRead[]>(`/v1/matters/${matterId}/search-operations?limit=100`);
        const matching = operations.filter((operation) => pendingIds.has(operation.id));
        const failed = matching.find((operation) => operation.status === "FAILED");
        if (failed) {
          toast.error(`Metadata was saved, but the search update failed: ${failed.error_message ?? "Unknown error"}`);
          return;
        }
        if (matching.length === pendingIds.size && matching.every((operation) => operation.status === "COMPLETED")) {
          await Promise.all([
            queryClient.invalidateQueries({ queryKey: ["matter-search", matterId] }),
            queryClient.invalidateQueries({ queryKey: ["matter-facet-values", matterId] }),
          ]);
          toast.success("Search results updated.");
          return;
        }
        await wait(SEARCH_PROJECTION_POLL_MS);
      }
      toast.info("Metadata was saved. The search update is still processing.");
    } catch {
      toast.info("Metadata was saved. Refresh search to check the indexed value.");
    }
  }, [matterId, queryClient]);

  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const client = useQuery({
    queryKey: ["client", matter.data?.client_id],
    queryFn: () => coreApi<ClientRead>(`/v1/clients/${matter.data!.client_id}`),
    enabled: Boolean(matter.data?.client_id),
  });
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
  });
  const groups = useQuery({
    queryKey: ["metadata-groups", matterId],
    queryFn: () => coreApi<MetadataGroupRead[]>(`/v1/matters/${matterId}/metadata-groups`),
  });
  const custodians = useQuery({
    queryKey: ["custodians", matter.data?.client_id],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/clients/${matter.data!.client_id}/custodians`),
    enabled: Boolean(matter.data?.client_id),
  });
  const currentUser = useQuery({
    queryKey: ["session"],
    queryFn: () => coreApi<UserRead>("/v1/auth/me"),
  });
  const tenantUsers = useQuery({
    queryKey: ["tenant-users", client.data?.tenant_id],
    queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${client.data!.tenant_id}/users`),
    enabled: Boolean(client.data?.tenant_id),
  });
  const savedSearches = useQuery({
    queryKey: ["matter-saved-searches", matterId],
    queryFn: () => coreApi<MatterSavedSearchRead[]>(`/v1/matters/${matterId}/saved-searches`),
  });

  const facetDefinitions = useMemo(() => {
    const available = (definitions.data ?? []).filter((definition) =>
      definition.status === "ACTIVE" && definition.searchable && definition.facetable && ["TEXT", "ENUM", "BOOLEAN"].includes(definition.type),
    );
    return available.sort((left, right) => {
      const leftRank = PREFERRED_FACETS.indexOf(left.key);
      const rightRank = PREFERRED_FACETS.indexOf(right.key);
      return (leftRank < 0 ? 1000 : leftRank) - (rightRank < 0 ? 1000 : rightRank) || left.display_name.localeCompare(right.display_name);
    });
  }, [definitions.data]);

  const searchRequest = useMemo<MatterSearchRequest>(() => {
    const definitionByKey = new Map(facetDefinitions.map((definition) => [definition.key, definition]));
    const searchFilters: MatterSearchFilter[] = Object.entries(filters).flatMap(([key, values]) => {
      const definition = definitionByKey.get(key);
      return definition && values.length ? [{ field: key, operator: "IN", values: values.map((value) => typedFacetValue(value, definition)) }] : [];
    });
    return {
      query: query.trim() || null,
      search_mode: query.trim() ? searchMode : "KEYWORD",
      filters: searchFilters,
      facets: [],
      sort: query.trim() ? [{ field: "_score", direction: "DESC" }] : [{ field: "created_at", direction: "DESC" }],
      offset,
      size: PAGE_SIZE,
    };
  }, [facetDefinitions, filters, offset, query, searchMode]);

  const searchResults = useQuery({
    queryKey: ["matter-search", matterId, searchRequest],
    queryFn: () => coreApi<MatterSearchResponse>(`/v1/matters/${matterId}/search`, { method: "POST", body: JSON.stringify(searchRequest) }),
    enabled: Boolean(matter.data && definitions.data),
    placeholderData: (previous) => previous,
  });

  const effectiveSelectedDocumentId = searchResults.data?.hits.some((hit) => hit.document_id === selectedDocumentId)
    ? selectedDocumentId
    : searchResults.data?.hits[0]?.document_id ?? "";
  const selectedHit = searchResults.data?.hits.find((hit) => hit.document_id === effectiveSelectedDocumentId);
  const selectedCollectionItemId = typeof selectedHit?.fields.collection_item_id === "string" ? selectedHit.fields.collection_item_id : "";
  const collectionItem = useQuery({
    queryKey: ["collection-item", selectedCollectionItemId],
    queryFn: () => coreApi<CollectionItemRead>(`/v1/collection-items/${selectedCollectionItemId}`),
    enabled: Boolean(selectedCollectionItemId),
  });
  const metadataValues = useQuery({
    queryKey: ["document-metadata-values", matterId, effectiveSelectedDocumentId],
    queryFn: () => coreApi<DocumentMetadataFieldRead[]>(`/v1/matters/${matterId}/documents/${effectiveSelectedDocumentId}/metadata-values`),
    enabled: Boolean(effectiveSelectedDocumentId),
  });

  const syncUrl = useCallback((
    nextQuery: string,
    nextFilters: SelectedFilters,
    nextOffset: number,
    documentId?: string,
    nextSearchMode: MatterSearchRequestSearchMode = searchMode,
  ) => {
    const params = new URLSearchParams();
    if (nextQuery.trim()) params.set("q", nextQuery.trim());
    if (nextSearchMode !== "KEYWORD") params.set("mode", nextSearchMode.toLowerCase());
    for (const [key, values] of Object.entries(nextFilters)) {
      for (const value of values) params.append(`f_${key}`, value);
    }
    if (nextOffset) params.set("page", String(Math.floor(nextOffset / PAGE_SIZE) + 1));
    if (documentId) params.set("document", documentId);
    const suffix = params.size ? `?${params.toString()}` : "";
    router.replace(`/review/matters/${matterId}${suffix}`, { scroll: false });
  }, [matterId, router, searchMode]);

  const metadataMutation = useMutation({
    mutationFn: async (actions: CodingAction[]) => {
      const mutations: MetadataMutationRead[] = [];
      for (const action of actions) {
        mutations.push(await coreApi<MetadataMutationRead>(`/v1/matters/${matterId}/documents/${effectiveSelectedDocumentId}/metadata-values/${action.definitionId}/events`, {
          method: "POST",
          body: JSON.stringify(action.payload),
        }));
      }
      return mutations;
    },
    onSuccess: (mutations) => {
      const updates = new Map(mutations.map((mutation) => [mutation.current.metadata_definition_id, mutation.current]));
      queryClient.setQueryData<DocumentMetadataFieldRead[]>(
        ["document-metadata-values", matterId, effectiveSelectedDocumentId],
        (current) => current?.map((field) => updates.get(field.metadata_definition_id) ?? field),
      );
      const fieldCount = updates.size;
      const operationIds = mutations.flatMap((mutation) => mutation.search_operation_id ? [mutation.search_operation_id] : []);
      toast.success(`${fieldCount} ${fieldCount === 1 ? "field" : "fields"} saved.${operationIds.length ? " Updating search…" : ""}`);
      if (operationIds.length) void refreshAfterSearchProjection(operationIds);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The coding changes could not be saved."),
  });
  const createSavedSearchMutation = useMutation({
    mutationFn: (payload: MatterSavedSearchCreate) => coreApi<MatterSavedSearchRead>(`/v1/matters/${matterId}/saved-searches`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (saved) => {
      queryClient.setQueryData<MatterSavedSearchRead[]>(["matter-saved-searches", matterId], (current) => current ? [saved, ...current] : [saved]);
      toast.success(`${saved.name} was saved.`);
    },
  });
  const deleteSavedSearchMutation = useMutation({
    mutationFn: (saved: MatterSavedSearchRead) => coreApi<void>(`/v1/matters/${matterId}/saved-searches/${saved.id}`, { method: "DELETE" }),
    onSuccess: (_, saved) => {
      queryClient.setQueryData<MatterSavedSearchRead[]>(["matter-saved-searches", matterId], (current) => current?.filter((item) => item.id !== saved.id));
      toast.success(`${saved.name} was deleted.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The saved search could not be deleted."),
  });

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setQuery(draftQuery);
    setOffset(0);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(draftQuery, filters, 0);
  };

  const changeSearchMode = (nextMode: MatterSearchRequestSearchMode) => {
    setSearchMode(nextMode);
    setOffset(0);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(query, filters, 0, undefined, nextMode);
  };

  const toggleFilter = (key: string, value: string) => {
    const next = { ...filters };
    const current = next[key] ?? [];
    next[key] = current.includes(value) ? current.filter((item) => item !== value) : [...current, value];
    if (!next[key].length) delete next[key];
    setFilters(next);
    setOffset(0);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(query, next, 0);
  };

  const clearFilters = () => {
    setFilters({});
    setOffset(0);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(query, {}, 0);
  };

  const selectDocument = (documentId: string) => {
    setSelectedDocumentId(documentId);
    setMobileDocumentOpen(true);
    syncUrl(query, filters, offset, documentId);
  };

  const changePage = (nextOffset: number) => {
    setOffset(nextOffset);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(query, filters, nextOffset);
  };

  const runSavedSearch = (saved: MatterSavedSearchRead) => {
    const nextQuery = saved.search.query?.trim() ?? "";
    const nextMode = nextQuery ? (saved.search.search_mode ?? "KEYWORD") : "KEYWORD";
    const nextFilters: SelectedFilters = {};
    for (const filter of saved.search.filters ?? []) {
      const values = filter.operator === "IN" ? filter.values : filter.operator === "EQ" ? [filter.value] : [];
      const tokens = (values ?? []).filter((value) => value !== null && value !== undefined).map(facetToken);
      if (tokens.length) nextFilters[filter.field] = tokens;
    }
    setDraftQuery(nextQuery);
    setQuery(nextQuery);
    setSearchMode(nextMode);
    setFilters(nextFilters);
    setOffset(0);
    setSelectedDocumentId("");
    setMobileDocumentOpen(false);
    syncUrl(nextQuery, nextFilters, 0, undefined, nextMode);
    toast.success(`Running ${saved.name}.`);
  };

  const custodianNames = useMemo(() => new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian.display_name])), [custodians.data]);
  const activeFilterCount = Object.values(filters).reduce((total, values) => total + values.length, 0);
  const total = searchResults.data?.total ?? 0;
  const pageStart = total ? offset + 1 : 0;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);

  if (matter.error || definitions.error || groups.error || savedSearches.error) {
    return <main id="main-content" className="grid min-h-screen place-items-center p-6"><QueryError message={matter.error?.message ?? definitions.error?.message ?? groups.error?.message ?? savedSearches.error?.message} /></main>;
  }

  return (
    <main id="main-content" className="flex h-dvh min-h-[36rem] flex-col overflow-hidden bg-background">
      <header className="shrink-0 border-b bg-card shadow-sm">
        <div className="flex min-h-14 flex-wrap items-center gap-2 px-3 py-2 lg:flex-nowrap">
          <BrandMark className="size-8 shrink-0 rounded-lg" />
          <Button asChild variant="ghost" size="sm" className="shrink-0"><Link href={matter.data ? `/app/clients/${matter.data.client_id}/matters/${matterId}` : "/app/clients"}><ArrowLeft />Matter</Link></Button>
          <div className="min-w-0 border-l pl-3">
            <p className="max-w-52 truncate text-sm font-semibold">{matter.data?.name ?? "Opening matter…"}</p>
            <p className="max-w-52 truncate text-xs text-muted-foreground">{client.data?.name ?? "Search & Review"}</p>
          </div>
          <form onSubmit={submitSearch} role="search" className="order-last flex min-w-0 basis-full items-center gap-2 lg:order-none lg:mx-3 lg:flex-1 lg:basis-auto">
            <Select value={searchMode} onValueChange={(value) => changeSearchMode(value as MatterSearchRequestSearchMode)}>
              <SelectTrigger className="w-32 shrink-0" aria-label="Search mode"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="KEYWORD">Keyword</SelectItem>
                <SelectItem value="SEMANTIC">Semantic</SelectItem>
                <SelectItem value="HYBRID">Hybrid</SelectItem>
              </SelectContent>
            </Select>
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} className="pl-9" placeholder={SEARCH_PLACEHOLDERS[searchMode]} aria-label="Search matter documents" />
            </div>
            <Button type="submit">Search</Button>
          </form>
          <Button variant="outline" size="sm" className="xl:hidden" onClick={() => setFiltersOpen(true)}><Filter />Filters{activeFilterCount ? <Badge variant="accent">{activeFilterCount}</Badge> : null}</Button>
          <Button variant="outline" size="sm" className="2xl:hidden" disabled={!effectiveSelectedDocumentId} onClick={() => setCodingOpen(true)}><PanelRight />Details</Button>
          <SavedSearchesDialog searches={savedSearches.data ?? []} loading={savedSearches.isPending} onRun={runSavedSearch} onDelete={(saved) => deleteSavedSearchMutation.mutateAsync(saved).then(() => undefined)} />
          <SaveSearchDialog search={searchRequest} users={tenantUsers.data ?? []} currentUserId={currentUser.data?.id} onCreate={(payload) => createSavedSearchMutation.mutateAsync(payload).then(() => undefined)} />
          <HelpLink topic="matterReview" />
          <ThemeToggle />
        </div>
        <div className="flex h-9 items-center justify-between gap-3 border-t px-3 text-xs text-muted-foreground">
          <span>{searchResults.isFetching ? "Searching…" : `${total.toLocaleString()} ${total === 1 ? "document" : "documents"}`}{searchResults.data ? ` · ${searchResults.data.took_ms.toLocaleString()} ms · ${searchMode.toLowerCase()}` : ""}</span>
          <div className="flex items-center gap-2 md:hidden">
            <Button variant={!mobileDocumentOpen ? "default" : "ghost"} size="sm" onClick={() => setMobileDocumentOpen(false)}>Results</Button>
            <Button variant={mobileDocumentOpen ? "default" : "ghost"} size="sm" disabled={!effectiveSelectedDocumentId} onClick={() => setMobileDocumentOpen(true)}>Document</Button>
          </div>
          <span className="hidden md:inline">{pageStart.toLocaleString()}–{pageEnd.toLocaleString()}</span>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside
          className="hidden min-h-0 min-w-0 shrink-0 flex-col overflow-hidden bg-card xl:flex xl:w-[var(--review-facet-width)]"
          style={{ "--review-facet-width": `${facetWidth}px` } as CSSProperties}
          aria-label="Search filters"
        >
          <FacetPanel matterId={matterId} searchRequest={searchRequest} definitions={facetDefinitions} filters={filters} custodianNames={custodianNames} onToggle={toggleFilter} onClear={clearFilters} />
        </aside>
        <ResizeHandle className="hidden xl:flex" label="Resize filter panel" value={facetWidth} min={184} max={420} onChange={setFacetWidth} />

        <section
          className={cn("min-h-0 min-w-0 shrink-0 flex-col overflow-hidden bg-card md:w-[var(--review-results-width)]", mobileDocumentOpen ? "hidden md:flex" : "flex w-full")}
          style={{ "--review-results-width": `${resultsWidth}px` } as CSSProperties}
          aria-label="Search results"
        >
          <div className="flex h-11 shrink-0 items-center justify-between border-b px-3">
            <h1 className="text-sm font-semibold">Results</h1>
            {activeFilterCount ? <span className="text-xs text-muted-foreground">{activeFilterCount} active</span> : null}
          </div>
          <ResultsList results={searchResults} selectedDocumentId={effectiveSelectedDocumentId} onSelect={selectDocument} matter={matter.data} />
          <div className="flex h-12 shrink-0 items-center justify-between border-t px-3">
            <Button variant="ghost" size="sm" disabled={!offset || searchResults.isFetching} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft />Previous</Button>
            <span className="text-xs tabular-nums text-muted-foreground">{pageStart.toLocaleString()}–{pageEnd.toLocaleString()} of {total.toLocaleString()}</span>
            <Button variant="ghost" size="sm" disabled={pageEnd >= total || searchResults.isFetching} onClick={() => changePage(offset + PAGE_SIZE)}>Next<ChevronRight /></Button>
          </div>
        </section>
        <ResizeHandle className="hidden md:flex" label="Resize result panel" value={resultsWidth} min={288} max={640} onChange={setResultsWidth} />

        <section className={cn("min-h-0 min-w-0 flex-1 overflow-hidden bg-background", mobileDocumentOpen ? "flex" : "hidden md:flex")} aria-label="Selected document">
          {collectionItem.isPending && selectedCollectionItemId ? <DocumentLoading />
            : collectionItem.error ? <div className="w-full p-5"><QueryError message={collectionItem.error.message} /></div>
            : collectionItem.data ? <DocumentViewerSurface item={collectionItem.data} className="h-full w-full" />
            : <DocumentEmpty />}
        </section>
        <ResizeHandle className="hidden 2xl:flex" label="Resize document details panel" value={detailsWidth} min={304} max={520} direction={-1} onChange={setDetailsWidth} />

        <aside
          className="hidden min-h-0 min-w-0 shrink-0 flex-col overflow-hidden bg-card 2xl:flex 2xl:w-[var(--review-details-width)]"
          style={{ "--review-details-width": `${detailsWidth}px` } as CSSProperties}
          aria-label="Document details"
        >
          <DocumentDetailsPanel definitions={definitions.data ?? []} groups={groups.data ?? []} hit={selectedHit} values={metadataValues.data} valuesVersion={metadataValues.dataUpdatedAt} loading={metadataValues.isPending && Boolean(effectiveSelectedDocumentId)} saving={metadataMutation.isPending} onSave={(actions) => metadataMutation.mutateAsync(actions).then(() => undefined)} />
        </aside>
      </div>

      <Dialog open={filtersOpen} onOpenChange={setFiltersOpen}>
        <DialogContent className="flex h-[min(86vh,48rem)] max-w-md flex-col overflow-hidden p-0 xl:hidden">
          <DialogHeader className="mb-0 border-b px-5 py-4"><DialogTitle>Filter documents</DialogTitle><DialogDescription>Narrow the current matter results.</DialogDescription></DialogHeader>
          <FacetPanel matterId={matterId} searchRequest={searchRequest} definitions={facetDefinitions} filters={filters} custodianNames={custodianNames} onToggle={toggleFilter} onClear={clearFilters} />
        </DialogContent>
      </Dialog>

      <Dialog open={codingOpen} onOpenChange={setCodingOpen}>
        <DialogContent className="flex h-[min(90vh,52rem)] max-w-xl flex-col overflow-hidden p-0 2xl:hidden">
          <DialogTitle className="sr-only">Document details</DialogTitle>
          <DocumentDetailsPanel definitions={definitions.data ?? []} groups={groups.data ?? []} hit={selectedHit} values={metadataValues.data} valuesVersion={metadataValues.dataUpdatedAt} loading={metadataValues.isPending && Boolean(effectiveSelectedDocumentId)} saving={metadataMutation.isPending} onSave={(actions) => metadataMutation.mutateAsync(actions).then(() => undefined)} />
        </DialogContent>
      </Dialog>
    </main>
  );
}

function FacetPanel({ matterId, searchRequest, definitions, filters, custodianNames, onToggle, onClear }: {
  matterId: string;
  searchRequest: MatterSearchRequest;
  definitions: MetadataDefinitionRead[];
  filters: SelectedFilters;
  custodianNames: Map<string, string>;
  onToggle: (key: string, value: string) => void;
  onClear: () => void;
}) {
  const hasFilters = Object.values(filters).some((values) => values.length);
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-11 shrink-0 items-center justify-between border-b px-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold"><SlidersHorizontal className="size-4" />Filters</h2>
        {hasFilters ? <Button variant="ghost" size="sm" onClick={onClear}>Clear</Button> : null}
      </div>
      <div className="min-h-0 min-w-0 flex-1 divide-y overflow-x-hidden overflow-y-auto">
        {definitions.map((definition) => <FacetSection key={definition.id} matterId={matterId} searchRequest={searchRequest} definition={definition} selected={filters[definition.key] ?? []} custodianNames={custodianNames} onToggle={onToggle} />)}
        {!definitions.length ? <p className="p-4 text-sm text-muted-foreground">No facetable fields are configured for this matter.</p> : null}
      </div>
    </div>
  );
}

function useDebouncedValue(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}

function FacetSection({ matterId, searchRequest, definition, selected, custodianNames, onToggle }: {
  matterId: string;
  searchRequest: MatterSearchRequest;
  definition: MetadataDefinitionRead;
  selected: string[];
  custodianNames: Map<string, string>;
  onToggle: (key: string, value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [valueQuery, setValueQuery] = useState("");
  const debouncedQuery = useDebouncedValue(valueQuery.trim(), 250);
  const enumLabels = useMemo(() => new Map((definition.allowed_values ?? []).map((option) => [option.key, option.label])), [definition.allowed_values]);
  const values = useQuery({
    queryKey: ["matter-facet-values", matterId, definition.key, searchRequest, debouncedQuery],
    queryFn: () => coreApi<MatterFacetValuesResponse>(`/v1/matters/${matterId}/facets/${definition.key}/values`, {
      method: "POST",
      body: JSON.stringify({ search: searchRequest, query: debouncedQuery || null, size: debouncedQuery ? 20 : 8 }),
    }),
    enabled: open,
    placeholderData: (previous) => previous,
  });
  const options = useMemo(() => {
    const available = new Map((values.data?.values ?? []).map((option) => [facetToken(option.value), option]));
    for (const token of selected) {
      if (!available.has(token)) available.set(token, { value: token, count: 0 });
    }
    return [...available.values()];
  }, [selected, values.data?.values]);
  const labelFor = (token: string) => definition.key === "custodian" ? custodianNames.get(token) ?? token
    : definition.type === "ENUM" ? enumLabels.get(token) ?? token
    : definition.type === "BOOLEAN" ? token === "true" ? "Yes" : "No"
    : definition.key === "file_extension" ? `.${token.replace(/^\./, "")}` : token;

  return (
    <section className="min-w-0">
      <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="flex min-h-11 w-full min-w-0 items-center gap-2 px-3 py-2 text-left text-sm font-semibold hover:bg-muted/60">
        {open ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
        <span className="min-w-0 flex-1 truncate" title={definition.display_name}>{definition.display_name}</span>
        {selected.length ? <Badge variant="accent">{selected.length}</Badge> : null}
      </button>
      {open ? <div className="space-y-2 px-3 pb-3">
        {definition.type !== "BOOLEAN" ? <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input value={valueQuery} onChange={(event) => setValueQuery(event.target.value)} className="h-8 pl-8 text-sm" placeholder={`Find ${definition.display_name.toLowerCase()}…`} aria-label={`Find ${definition.display_name} values`} /></div> : null}
        {values.isPending ? <p className="px-1 text-xs text-muted-foreground">Loading values…</p>
          : values.error ? <p className="px-1 text-xs text-destructive">Values could not be loaded.</p>
          : options.length ? <div className="min-w-0 space-y-0.5 overflow-hidden">
            {options.map((option) => {
              const token = facetToken(option.value);
              const checked = selected.includes(token);
              const label = labelFor(token);
              return <label key={token} className="flex min-h-8 min-w-0 cursor-pointer items-center gap-2 overflow-hidden rounded-md px-1.5 py-1 text-sm hover:bg-muted">
                <input type="checkbox" checked={checked} onChange={() => onToggle(definition.key, token)} className="size-4 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1 truncate" title={label}>{label}</span>
                <span className="font-mono text-xs tabular-nums text-muted-foreground">{option.count ? option.count.toLocaleString() : "—"}</span>
              </label>;
            })}
          </div> : <p className="px-1 text-xs text-muted-foreground">No matching values.</p>}
      </div> : null}
    </section>
  );
}

function ResultsList({ results, selectedDocumentId, onSelect, matter }: {
  results: ReturnType<typeof useQuery<MatterSearchResponse, Error>>;
  selectedDocumentId: string;
  onSelect: (documentId: string) => void;
  matter?: MatterRead;
}) {
  if (results.isPending) return <div className="space-y-2 p-3">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-20 w-full" />)}</div>;
  if (results.error) {
    return <div className="flex min-h-0 flex-1 items-center p-4"><div className="w-full text-center"><FileSearch className="mx-auto size-8 text-muted-foreground" /><p className="mt-3 font-semibold">Search is not ready</p><p className="mt-1 text-sm text-muted-foreground">{results.error.message}</p>{matter ? <Button asChild variant="outline" size="sm" className="mt-4"><Link href={`/app/clients/${matter.client_id}/matters/${matter.id}?tab=search`}>Open search index</Link></Button> : null}</div></div>;
  }
  if (!results.data.hits.length) return <div className="grid min-h-0 flex-1 place-items-center p-6 text-center"><div><FileSearch className="mx-auto size-8 text-muted-foreground" /><p className="mt-3 font-semibold">No documents found</p><p className="mt-1 text-sm text-muted-foreground">Try fewer filters or a broader search.</p></div></div>;

  return (
    <ol className="min-h-0 flex-1 overflow-y-auto" aria-label="Document search results">
      {results.data.hits.map((hit, index) => {
        const selected = hit.document_id === selectedDocumentId;
        const custodians = displayValue(hit.fields.custodian_names);
        const path = displayValue(hit.fields.source_path);
        return <li key={hit.document_id} className="border-b">
          <button type="button" onClick={() => onSelect(hit.document_id)} aria-current={selected ? "true" : undefined} className={cn("w-full border-l-[3px] px-3 py-3 text-left outline-none transition hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", selected ? "border-l-accent bg-primary/8" : "border-l-transparent")}>
            <div className="flex items-start gap-2">
              <span className="mt-0.5 font-mono text-xs tabular-nums text-muted-foreground">{index + 1}</span>
              <div className="min-w-0 flex-1">
                <p className="line-clamp-2 text-sm font-semibold leading-5">{resultTitle(hit)}</p>
                {hit.best_passage ? <p className="mt-1 line-clamp-3 text-xs leading-5 text-foreground/80">{hit.best_passage.text}</p> : null}
                <p className="mt-1 truncate text-xs text-muted-foreground" title={custodians}>{custodians}</p>
                <p className="mt-1 truncate text-xs text-muted-foreground" title={path}>{path}</p>
              </div>
              <Badge variant="outline" className="shrink-0">{resultFileType(hit)}</Badge>
            </div>
          </button>
        </li>;
      })}
    </ol>
  );
}

function DocumentLoading() {
  return <div className="w-full space-y-3 p-5">{Array.from({ length: 10 }, (_, index) => <Skeleton key={index} className={cn("h-5", index < 2 ? "w-2/3" : "w-full")} />)}</div>;
}

function DocumentEmpty() {
  return <div className="grid h-full w-full place-items-center p-8 text-center"><div><span className="mx-auto grid size-12 place-items-center rounded-xl bg-muted text-muted-foreground"><FileText /></span><p className="mt-4 font-semibold">Select a document</p><p className="mt-1 text-sm text-muted-foreground">Choose a result to read and code it.</p></div></div>;
}

function ResizeHandle({ className, label, value, min, max, direction = 1, onChange }: {
  className?: string;
  label: string;
  value: number;
  min: number;
  max: number;
  direction?: 1 | -1;
  onChange: (value: number) => void;
}) {
  const drag = useRef<{ x: number; width: number } | null>(null);
  const clamp = (next: number) => onChange(Math.min(max, Math.max(min, next)));

  return (
    <div
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={min}
      aria-valuemax={max}
      aria-valuenow={value}
      tabIndex={0}
      className={cn("group relative z-10 w-2 shrink-0 touch-none cursor-col-resize items-center justify-center border-x bg-muted/30 outline-none hover:bg-primary/10 focus-visible:bg-primary/10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", className)}
      onPointerDown={(event) => {
        drag.current = { x: event.clientX, width: value };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        clamp(drag.current.width + ((event.clientX - drag.current.x) * direction));
      }}
      onPointerUp={(event) => {
        drag.current = null;
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => { drag.current = null; }}
      onKeyDown={(event) => {
        if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
        event.preventDefault();
        const delta = event.key === "ArrowRight" ? 16 : -16;
        clamp(value + (delta * direction));
      }}
    >
      <GripVertical className="size-3 text-muted-foreground opacity-45 transition-opacity group-hover:opacity-100 group-focus-visible:opacity-100" />
    </div>
  );
}

type DetailsTab = "coding" | "metadata";

function DocumentDetailsPanel({ definitions, groups, hit, values, valuesVersion, loading, saving, onSave }: {
  definitions: MetadataDefinitionRead[];
  groups: MetadataGroupRead[];
  hit?: MatterSearchHit;
  values?: DocumentMetadataFieldRead[];
  valuesVersion: number;
  loading: boolean;
  saving: boolean;
  onSave: (actions: CodingAction[]) => Promise<void>;
}) {
  const [tab, setTab] = useState<DetailsTab>("coding");
  const tabsId = useId();
  const definitionById = new Map(definitions.map((definition) => [definition.id, definition]));
  const valueByDefinition = new Map((values ?? []).map((field) => [field.metadata_definition_id, field]));
  const visibleGroups = groups.filter((group) => group.status === "ACTIVE" && group.document_visible);

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex h-11 shrink-0 items-end border-b px-2" role="tablist" aria-label="Document details">
        <button id={`${tabsId}-coding`} type="button" role="tab" aria-selected={tab === "coding"} aria-controls={`${tabsId}-panel`} onClick={() => setTab("coding")} className={detailsTabClass(tab === "coding")}>Coding</button>
        <button id={`${tabsId}-metadata`} type="button" role="tab" aria-selected={tab === "metadata"} aria-controls={`${tabsId}-panel`} onClick={() => setTab("metadata")} className={detailsTabClass(tab === "metadata")}>Metadata</button>
      </div>
      {!hit ? <div className="grid min-h-0 flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to view its metadata.</div>
        : loading ? <div className="space-y-3 p-4">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-16 w-full" />)}</div>
        : tab === "coding"
          ? <CodingForm key={`${hit.document_id}:${valuesVersion}`} id={`${tabsId}-panel`} definitions={definitions} visibleGroups={visibleGroups} definitionById={definitionById} valueByDefinition={valueByDefinition} saving={saving} onSave={onSave} />
          : <MetadataPanel id={`${tabsId}-panel`} definitions={definitions} visibleGroups={visibleGroups} definitionById={definitionById} valueByDefinition={valueByDefinition} hit={hit} />}
    </div>
  );
}

function detailsTabClass(active: boolean) {
  return cn("relative h-10 px-3 text-sm font-semibold text-muted-foreground outline-none transition hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", active && "text-primary after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-accent");
}

function CodingForm({ id, definitions, visibleGroups, definitionById, valueByDefinition, saving, onSave }: {
  id: string;
  definitions: MetadataDefinitionRead[];
  visibleGroups: MetadataGroupRead[];
  definitionById: Map<string, MetadataDefinitionRead>;
  valueByDefinition: Map<string, DocumentMetadataFieldRead>;
  saving: boolean;
  onSave: (actions: CodingAction[]) => Promise<void>;
}) {
  const editableIds = new Set(visibleGroups.flatMap((group) => group.definition_ids));
  const editableDefinitions = definitions.filter((definition) => editableIds.has(definition.id) && definition.status === "ACTIVE" && definition.reviewable && definition.value_source === "ASSERTED");
  const initialDrafts = Object.fromEntries(editableDefinitions.map((definition) => [definition.id, (valueByDefinition.get(definition.id)?.values ?? []).map((item) => editorValue(item.value, definition))]));
  const [drafts, setDrafts] = useState<Record<string, string[]>>(initialDrafts);

  const fieldChanged = (definition: MetadataDefinitionRead) => {
    const initial = initialDrafts[definition.id] ?? [];
    const draft = drafts[definition.id] ?? [];
    if (definition.cardinality === "SINGLE") return (initial[0] ?? "") !== (draft[0] ?? "");
    return [...initial].sort().join("\u0000") !== [...draft].sort().join("\u0000");
  };
  const dirtyCount = editableDefinitions.filter(fieldChanged).length;

  const actions = () => editableDefinitions.flatMap((definition): CodingAction[] => {
    if (!fieldChanged(definition)) return [];
    const field = valueByDefinition.get(definition.id);
    const draft = drafts[definition.id] ?? [];
    if (definition.cardinality === "SINGLE") {
      const value = draft[0] ?? "";
      return value.trim()
        ? [{ definitionId: definition.id, payload: { operation: "SET", value: parseEditorValue(value, definition) } }]
        : field?.values.length ? [{ definitionId: definition.id, payload: { operation: "CLEAR" } }] : [];
    }

    const nextValues = [...new Set(draft.filter((value) => value.trim()))];
    const current = new Map((field?.values ?? []).map((item) => [editorValue(item.value, definition), item]));
    if (!nextValues.length && current.size) return [{ definitionId: definition.id, payload: { operation: "CLEAR" } }];
    return [
      ...[...current.entries()].filter(([value]) => !nextValues.includes(value)).map(([, item]) => ({ definitionId: definition.id, payload: { operation: "REMOVE" as const, target_event_id: item.source_event_id } })),
      ...nextValues.filter((value) => !current.has(value)).map((value) => ({ definitionId: definition.id, payload: { operation: "ADD" as const, value: parseEditorValue(value, definition) } })),
    ];
  });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const pending = actions();
      if (pending.length) await onSave(pending);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Check the entered values.");
    }
  };

  return (
    <form id={id} role="tabpanel" aria-labelledby={id.replace("-panel", "-coding")} className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden" onSubmit={submit}>
      <div className="min-h-0 min-w-0 flex-1 divide-y overflow-x-hidden overflow-y-auto">
        {visibleGroups.map((group) => {
          const groupDefinitions = group.definition_ids.map((definitionId) => definitionById.get(definitionId)).filter((definition): definition is MetadataDefinitionRead => Boolean(definition && definition.status === "ACTIVE" && definition.reviewable && definition.value_source === "ASSERTED"));
          if (!groupDefinitions.length) return null;
          return <section key={group.id} className="min-w-0 p-3"><h3 className="mb-3 truncate text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground" title={group.display_name}>{group.display_name}</h3><div className="min-w-0 space-y-4">{groupDefinitions.map((definition) => <CodingField key={definition.id} inputId={`${id}-field-${definition.id}`} definition={definition} field={valueByDefinition.get(definition.id)} values={drafts[definition.id] ?? []} onChange={(next) => setDrafts((current) => ({ ...current, [definition.id]: next }))} />)}</div></section>;
        })}
        {!editableDefinitions.length ? <p className="p-4 text-sm text-muted-foreground">No editable fields are visible for document coding.</p> : null}
      </div>
      <div className="flex shrink-0 items-center justify-between gap-3 border-t bg-card p-3">
        <span className="text-xs text-muted-foreground">{dirtyCount ? `${dirtyCount} ${dirtyCount === 1 ? "field" : "fields"} changed` : "No unsaved changes"}</span>
        <div className="flex gap-2">
          <Button type="button" variant="ghost" size="sm" disabled={!dirtyCount || saving} onClick={() => setDrafts(initialDrafts)}><RotateCcw />Reset</Button>
          <Button type="submit" size="sm" disabled={!dirtyCount || saving}><Save />{saving ? "Saving…" : "Save changes"}</Button>
        </div>
      </div>
    </form>
  );
}

function CodingField({ inputId, definition, field, values, onChange }: { inputId: string; definition: MetadataDefinitionRead; field?: DocumentMetadataFieldRead; values: string[]; onChange: (values: string[]) => void }) {
  return <div className="min-w-0"><div className="flex min-w-0 items-center justify-between gap-2"><label htmlFor={inputId} className="min-w-0 truncate text-xs font-medium text-muted-foreground" title={definition.display_name}>{definition.display_name}</label>{field?.resolution_state && field.resolution_state !== "EMPTY" ? <Badge variant={field.resolution_state === "CONFLICTED" ? "accent" : "outline"}>{field.resolution_state.toLowerCase()}</Badge> : null}</div>{definition.cardinality === "MULTIPLE" ? <MultiValueField inputId={inputId} definition={definition} values={values} onChange={onChange} /> : <div className="mt-1.5"><FieldInput id={inputId} definition={definition} value={values[0] ?? ""} onChange={(value) => onChange([value])} /></div>}</div>;
}

function MultiValueField({ inputId, definition, values, onChange }: { inputId: string; definition: MetadataDefinitionRead; values: string[]; onChange: (values: string[]) => void }) {
  const [entry, setEntry] = useState("");
  const add = () => {
    if (!entry.trim() || values.includes(entry)) return;
    onChange([...values, entry]);
    setEntry("");
  };
  return <div className="min-w-0"><div className="mt-1.5 flex min-w-0 flex-wrap gap-1.5">{values.map((value) => <Badge key={value} variant="outline" className="max-w-full"><span className="truncate">{fieldDisplayValue(value, definition)}</span><button type="button" className="ml-1 rounded-sm outline-none hover:text-destructive focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Remove ${fieldDisplayValue(value, definition)}`} onClick={() => onChange(values.filter((item) => item !== value))}><X className="size-3" /></button></Badge>)}</div><div className="mt-2 flex min-w-0 gap-2"><FieldInput id={inputId} definition={definition} value={entry} onChange={setEntry} /><Button type="button" variant="outline" size="sm" disabled={!entry.trim()} onClick={add}>Add</Button></div></div>;
}

function fieldDisplayValue(value: string, definition: MetadataDefinitionRead) {
  if (definition.type === "ENUM") return definition.allowed_values?.find((option) => option.key === value)?.label ?? value;
  if (definition.type === "BOOLEAN") return value === "true" ? "Yes" : value === "false" ? "No" : value;
  return value;
}

function MetadataPanel({ id, visibleGroups, definitionById, valueByDefinition, hit }: {
  id: string;
  definitions: MetadataDefinitionRead[];
  visibleGroups: MetadataGroupRead[];
  definitionById: Map<string, MetadataDefinitionRead>;
  valueByDefinition: Map<string, DocumentMetadataFieldRead>;
  hit: MatterSearchHit;
}) {
  const metadata = resultMetadata(hit);
  return <div id={id} role="tabpanel" aria-labelledby={id.replace("-panel", "-metadata")} className="min-h-0 min-w-0 flex-1 divide-y overflow-x-hidden overflow-y-auto">{visibleGroups.map((group) => {
    const groupDefinitions = group.definition_ids.map((definitionId) => definitionById.get(definitionId)).filter((definition): definition is MetadataDefinitionRead => Boolean(definition && definition.status === "ACTIVE"));
    if (!groupDefinitions.length) return null;
    return <section key={group.id} className="min-w-0 p-3"><h3 className="mb-3 truncate text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground" title={group.display_name}>{group.display_name}</h3><dl className="min-w-0 space-y-3">{groupDefinitions.map((definition) => {
      const field = valueByDefinition.get(definition.id);
      const asserted = field?.values.map((item) => item.value);
      const value = definition.value_source === "ASSERTED" ? asserted : definition.key === "custodian" ? hit.fields.custodian_names : metadata[definition.key] ?? hit.fields[definition.key];
      return <div key={definition.id} className="min-w-0"><dt className="flex items-center justify-between gap-2 text-xs font-medium text-muted-foreground"><span className="min-w-0 truncate" title={definition.display_name}>{definition.display_name}</span><Badge variant="outline" className="shrink-0">{definition.value_source.toLowerCase()}</Badge></dt><dd className="mt-1 break-words text-sm [overflow-wrap:anywhere]">{displayValue(value)}</dd></div>;
    })}</dl></section>;
  })}{!visibleGroups.length ? <p className="p-4 text-sm text-muted-foreground">No metadata groups are visible on the document surface.</p> : null}</div>;
}

function FieldInput({ definition, value, onChange, id }: { definition: MetadataDefinitionRead; value: string; onChange: (value: string) => void; id?: string }) {
  if (definition.type === "ENUM") return <Select value={value || "__empty"} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id} className="h-9"><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem>{(definition.allowed_values ?? []).filter((option) => option.active).map((option) => <SelectItem key={option.key} value={option.key}>{option.label}</SelectItem>)}</SelectContent></Select>;
  if (definition.type === "BOOLEAN") return <Select value={value || "__empty"} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id} className="h-9"><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem><SelectItem value="true">Yes</SelectItem><SelectItem value="false">No</SelectItem></SelectContent></Select>;
  if (definition.type === "LONG_TEXT" || definition.type === "JSON") return <Textarea id={id} className="min-h-20 text-sm" value={value} onChange={(event) => onChange(event.target.value)} />;
  const type = definition.type === "INTEGER" || definition.type === "DECIMAL" ? "number" : definition.type === "DATE" ? "date" : definition.type === "DATETIME" ? "datetime-local" : "text";
  return <Input id={id} className="h-9" type={type} step={definition.type === "DECIMAL" ? "any" : undefined} value={value} onChange={(event) => onChange(event.target.value)} />;
}
