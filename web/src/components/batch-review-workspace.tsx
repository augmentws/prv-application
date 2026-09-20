"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronDown, ChevronLeft, ChevronRight, FileText, Save, Search, SkipForward, Sparkles, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { BrandMark } from "@/components/brand-mark";
import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import { HelpLink } from "@/components/help-link";
import { QueryError } from "@/components/query-state";
import { ResultPagination } from "@/components/result-pagination";
import { StatusBadge } from "@/components/status-badge";
import { parseMinimumSimilarity, SemanticThresholdControl } from "@/components/semantic-threshold-control";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type {
  ClientRead,
  BatchTopicTaxonomyRead,
  CollectionItemRead,
  CustodianRead,
  MatterRead,
  MatterFacetValuesResponse,
  MatterSearchFilter,
  MatterSearchHit,
  MatterSearchRequest,
  MatterSearchRequestSearchMode,
  MatterSearchResponse,
  MetadataDefinitionRead,
  ReviewBatchCodingFieldRead,
  ReviewBatchDocumentCodingRead,
  ReviewBatchDocumentAnalysisRead,
  ReviewBatchDocumentRead,
  ReviewBatchRead,
  ReviewBatchRunDocumentValues,
  ReviewBatchRunProgressRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;
const PREFERRED_FACETS = ["custodian", "file_extension", "responsiveness", "privilege", "key_document", "topics"];
const SEARCH_PLACEHOLDERS: Record<MatterSearchRequestSearchMode, string> = {
  KEYWORD: "Search this batch's body, filenames, paths, email headers, and metadata",
  SEMANTIC: "Find documents in this batch by concept or meaning",
  HYBRID: "Combine exact words with conceptually related batch results",
};

type SelectedFilters = Record<string, string[]>;

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join(", ") : "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function resultTitle(hit: MatterSearchHit) {
  const metadata = objectValue(hit.fields.metadata);
  return displayValue(hit.fields.email_subject || metadata.document_title || hit.fields.original_filename || "Untitled document");
}

function resultFileType(hit: MatterSearchHit) {
  const metadata = objectValue(hit.fields.metadata);
  const extension = displayValue(metadata.file_extension);
  return extension === "—" ? displayValue(hit.fields.record_type) : extension.replace(/^\./, "").toUpperCase();
}

function facetToken(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function typedFacetValue(token: string, definition: MetadataDefinitionRead): unknown {
  if (definition.type === "BOOLEAN") return token === "true";
  if (definition.type === "INTEGER" || definition.type === "DECIMAL") return Number(token);
  return token;
}

interface SnapshotOption {
  key: string;
  label: string;
  active?: boolean;
}

interface SnapshotDefinition {
  key: string;
  display_name: string;
  description?: string | null;
  type: "TEXT" | "LONG_TEXT" | "INTEGER" | "DECIMAL" | "BOOLEAN" | "DATE" | "DATETIME" | "ENUM" | "JSON";
  cardinality: "SINGLE" | "MULTIPLE";
  allowed_values: SnapshotOption[];
}

function snapshot(field: ReviewBatchCodingFieldRead): SnapshotDefinition {
  const value = field.definition_snapshot;
  const allowed = Array.isArray(value.allowed_values)
    ? value.allowed_values.filter((item): item is SnapshotOption => Boolean(item && typeof item === "object" && "key" in item && "label" in item))
    : [];
  return {
    key: typeof value.key === "string" ? value.key : field.metadata_definition_id,
    display_name: typeof value.display_name === "string" ? value.display_name : "Coding field",
    description: typeof value.description === "string" ? value.description : null,
    type: typeof value.type === "string" ? value.type as SnapshotDefinition["type"] : "TEXT",
    cardinality: value.cardinality === "MULTIPLE" ? "MULTIPLE" : "SINGLE",
    allowed_values: allowed,
  };
}

function parseValue(value: string, definition: SnapshotDefinition): unknown {
  if (definition.type === "INTEGER") {
    const parsed = Number(value);
    if (!Number.isSafeInteger(parsed)) throw new Error(`${definition.display_name} must be a whole number.`);
    return parsed;
  }
  if (definition.type === "DECIMAL") {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error(`${definition.display_name} must be a number.`);
    return parsed;
  }
  if (definition.type === "BOOLEAN") return value === "true";
  if (definition.type === "JSON") {
    const parsed = JSON.parse(value) as unknown;
    if (parsed === null || typeof parsed !== "object") throw new Error(`${definition.display_name} must be a JSON object or array.`);
    return parsed;
  }
  if (definition.type === "DATETIME") {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) throw new Error(`${definition.display_name} must be a date and time.`);
    return parsed.toISOString();
  }
  return value;
}

function editorValue(value: unknown, definition: SnapshotDefinition) {
  if (value === null || value === undefined) return "";
  if (definition.type === "JSON") return JSON.stringify(value, null, 2);
  if (definition.type === "DATETIME") {
    const parsed = new Date(String(value));
    if (!Number.isNaN(parsed.valueOf())) return parsed.toISOString().slice(0, 16);
  }
  return String(value);
}

function statusLabel(status: ReviewBatchDocumentRead["review_status"]) {
  return status.toLowerCase().replace("_", " ");
}

export function BatchReviewWorkspace({ matterId, batchId, initialDocumentId, initialPage = 1 }: {
  matterId: string;
  batchId: string;
  initialDocumentId?: string;
  initialPage?: number;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState<MatterSearchRequestSearchMode>("KEYWORD");
  const [draftMinimumSimilarity, setDraftMinimumSimilarity] = useState("");
  const [minimumSimilarity, setMinimumSimilarity] = useState<number | null>(null);
  const [filters, setFilters] = useState<SelectedFilters>({});
  const [offset, setOffset] = useState(Math.max(0, initialPage - 1) * PAGE_SIZE);
  const [selectedDocumentId, setSelectedDocumentId] = useState(initialDocumentId ?? "");
  const [selectedTopicKeys, setSelectedTopicKeys] = useState<string[]>([]);
  const [sidePanel, setSidePanel] = useState<"analysis" | "coding">("analysis");

  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const client = useQuery({
    queryKey: ["client", matter.data?.client_id],
    queryFn: () => coreApi<ClientRead>(`/v1/clients/${matter.data!.client_id}`),
    enabled: Boolean(matter.data?.client_id),
  });
  const custodians = useQuery({
    queryKey: ["custodians", matter.data?.client_id],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/clients/${matter.data!.client_id}/custodians`),
    enabled: Boolean(matter.data?.client_id),
  });
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
  });
  const batch = useQuery({ queryKey: ["review-batch", matterId, batchId], queryFn: () => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}`) });
  const taxonomy = useQuery({
    queryKey: ["review-batch-topic-taxonomy", matterId, batchId],
    queryFn: () => coreApi<BatchTopicTaxonomyRead | null>(`/v1/matters/${matterId}/review-batches/${batchId}/topic-taxonomy`),
    enabled: batch.data?.status === "READY",
  });
  const run = useQuery({
    queryKey: ["review-batch-run", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchRunRead>(`/v1/matters/${matterId}/review-batches/${batchId}/review-run`, { method: "POST" }),
    enabled: batch.data?.status === "READY",
    retry: false,
  });
  const progress = useQuery({
    queryKey: ["review-batch-progress", matterId, batchId, run.data?.id],
    queryFn: () => coreApi<ReviewBatchRunProgressRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/progress`),
    enabled: Boolean(run.data?.id),
  });

  const facetDefinitions = useMemo(() => (definitions.data ?? [])
    .filter((definition) => definition.status === "ACTIVE" && definition.searchable && definition.facetable && ["TEXT", "ENUM", "BOOLEAN"].includes(definition.type))
    .sort((left, right) => {
      const leftRank = PREFERRED_FACETS.indexOf(left.key);
      const rightRank = PREFERRED_FACETS.indexOf(right.key);
      return (leftRank < 0 ? 1000 : leftRank) - (rightRank < 0 ? 1000 : rightRank) || left.display_name.localeCompare(right.display_name);
    }), [definitions.data]);
  const searchRequest = useMemo<MatterSearchRequest>(() => {
    const definitionByKey = new Map(facetDefinitions.map((definition) => [definition.key, definition]));
    const searchFilters: MatterSearchFilter[] = Object.entries(filters).flatMap(([key, values]) => {
      const definition = definitionByKey.get(key);
      return definition && values.length ? [{ field: key, operator: "IN", values: values.map((value) => typedFacetValue(value, definition)) }] : [];
    });
    const effectiveMode = query.trim() ? searchMode : "KEYWORD";
    return {
      query: query.trim() || null,
      search_mode: effectiveMode,
      minimum_similarity: effectiveMode === "SEMANTIC" ? minimumSimilarity : null,
      filters: searchFilters,
      facets: [],
      sort: query.trim() ? [{ field: "_score", direction: "DESC" }] : [{ field: "created_at", direction: "DESC" }],
      offset,
      size: PAGE_SIZE,
    };
  }, [facetDefinitions, filters, minimumSimilarity, offset, query, searchMode]);
  const searchResults = useQuery({
    queryKey: ["review-batch-search", matterId, batchId, searchRequest, selectedTopicKeys],
    queryFn: () => {
      const params = new URLSearchParams();
      for (const topic of selectedTopicKeys) params.append("topic_key", topic);
      return coreApi<MatterSearchResponse>(`/v1/matters/${matterId}/review-batches/${batchId}/search${params.size ? `?${params}` : ""}`, { method: "POST", body: JSON.stringify(searchRequest) });
    },
    enabled: batch.data?.search_status === "READY" && Boolean(definitions.data),
    placeholderData: (previous) => previous,
  });
  const topicFacets = useQuery({
    queryKey: ["review-batch-topic-facets", matterId, batchId, searchRequest],
    queryFn: () => coreApi<MatterFacetValuesResponse>(`/v1/matters/${matterId}/review-batches/${batchId}/topic-facets`, { method: "POST", body: JSON.stringify(searchRequest) }),
    enabled: batch.data?.search_status === "READY" && Boolean(taxonomy.data),
  });
  const pageDocumentIds = searchResults.data?.hits.map((hit) => hit.document_id) ?? [];
  const statusQuery = useQuery({
    queryKey: ["review-batch-document-statuses", matterId, batchId, run.data?.id, pageDocumentIds],
    queryFn: () => {
      const params = new URLSearchParams({ run_id: run.data!.id, offset: "0", limit: String(PAGE_SIZE) });
      for (const documentId of pageDocumentIds) params.append("document_id", documentId);
      return coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?${params}`);
    },
    enabled: Boolean(run.data?.id && pageDocumentIds.length),
  });
  const statusByDocument = useMemo(() => new Map((statusQuery.data ?? []).map((document) => [document.matter_document_id, document])), [statusQuery.data]);
  const effectiveDocumentId = searchResults.data?.hits.some((hit) => hit.document_id === selectedDocumentId)
    ? selectedDocumentId
    : searchResults.data?.hits.find((hit) => statusByDocument.get(hit.document_id)?.review_status === "NOT_STARTED")?.document_id
      ?? searchResults.data?.hits[0]?.document_id
      ?? "";
  const selectedHit = searchResults.data?.hits.find((hit) => hit.document_id === effectiveDocumentId);
  const selectedCollectionItemId = typeof selectedHit?.fields.collection_item_id === "string" ? selectedHit.fields.collection_item_id : "";
  const collectionItem = useQuery({
    queryKey: ["collection-item", selectedCollectionItemId],
    queryFn: () => coreApi<CollectionItemRead>(`/v1/collection-items/${selectedCollectionItemId}`),
    enabled: Boolean(selectedCollectionItemId),
  });
  const coding = useQuery({
    queryKey: ["review-batch-document-coding", matterId, batchId, run.data?.id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchDocumentCodingRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}`),
    enabled: Boolean(run.data?.id && effectiveDocumentId),
  });
  const analysis = useQuery({
    queryKey: ["review-batch-document-analysis", matterId, batchId, taxonomy.data?.review_batch_run_id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchDocumentAnalysisRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${taxonomy.data!.review_batch_run_id}/documents/${effectiveDocumentId}/analysis`),
    enabled: Boolean(taxonomy.data?.review_batch_run_id && effectiveDocumentId),
    retry: false,
  });

  const syncUrl = (documentId?: string, nextOffset = offset) => {
    const params = new URLSearchParams({ batch: batchId });
    if (query.trim()) params.set("q", query.trim());
    if (searchMode !== "KEYWORD") params.set("mode", searchMode.toLowerCase());
    if (query.trim() && searchMode === "SEMANTIC" && minimumSimilarity !== null) {
      params.set("similarity", String(minimumSimilarity));
    }
    if (nextOffset) params.set("page", String(Math.floor(nextOffset / PAGE_SIZE) + 1));
    for (const [key, values] of Object.entries(filters)) {
      for (const value of values) params.append(`f_${key}`, value);
    }
    if (documentId) params.set("document", documentId);
    router.replace(`/review/matters/${matterId}?${params}`, { scroll: false });
  };
  const selectDocument = (documentId: string) => {
    setSelectedDocumentId(documentId);
    syncUrl(documentId);
  };
  const changePage = (nextOffset: number) => {
    setOffset(nextOffset);
    setSelectedDocumentId("");
    syncUrl(undefined, nextOffset);
  };

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setMinimumSimilarity(
      searchMode === "SEMANTIC" ? parseMinimumSimilarity(draftMinimumSimilarity) : null,
    );
    setQuery(draftQuery);
    setOffset(0);
    setSelectedDocumentId("");
  };

  const changeSearchMode = (nextMode: MatterSearchRequestSearchMode) => {
    setSearchMode(nextMode);
    setMinimumSimilarity(nextMode === "SEMANTIC" ? parseMinimumSimilarity(draftMinimumSimilarity) : null);
    setOffset(0);
    setSelectedDocumentId("");
  };

  const toggleFilter = (key: string, value: string) => {
    setFilters((current) => {
      const next = { ...current };
      const selected = next[key] ?? [];
      next[key] = selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value];
      if (!next[key].length) delete next[key];
      return next;
    });
    setOffset(0);
    setSelectedDocumentId("");
  };
  const toggleTopic = (topicKey: string) => {
    setSelectedTopicKeys((current) => current.includes(topicKey) ? current.filter((item) => item !== topicKey) : [...current, topicKey]);
    setOffset(0);
    setSelectedDocumentId("");
  };

  const refreshReview = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["review-batch-document-statuses", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-progress", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-document-coding", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-run", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] }),
    ]);
  };

  const moveNext = () => {
    const index = searchResults.data?.hits.findIndex((hit) => hit.document_id === effectiveDocumentId) ?? -1;
    const next = searchResults.data?.hits[index + 1];
    if (next) selectDocument(next.document_id);
    else if (offset + PAGE_SIZE < (searchResults.data?.total ?? 0)) {
      setOffset((current) => current + PAGE_SIZE);
      setSelectedDocumentId("");
    }
  };

  const save = useMutation({
    mutationFn: (payload: ReviewBatchRunDocumentValues) => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}/values`, { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document coding saved.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document coding could not be saved."),
  });
  const skip = useMutation({
    mutationFn: () => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}/skip`, { method: "POST" }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document skipped.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document could not be skipped."),
  });

  const currentIndex = searchResults.data?.hits.findIndex((hit) => hit.document_id === effectiveDocumentId) ?? -1;
  const processed = (progress.data?.completed_count ?? 0) + (progress.data?.skipped_count ?? 0);
  const percent = progress.data?.document_count ? Math.round((processed / progress.data.document_count) * 100) : 0;
  const fatalError = matter.error ?? client.error ?? custodians.error ?? definitions.error ?? batch.error ?? run.error ?? taxonomy.error ?? searchResults.error;

  if (fatalError) return <main className="grid h-dvh place-items-center p-6"><QueryError message={fatalError.message} /></main>;

  return (
    <main id="main-content" className="flex h-dvh min-h-[36rem] flex-col overflow-hidden bg-background">
      <header className="shrink-0 border-b bg-card shadow-sm">
        <div className="flex min-h-14 flex-wrap items-center gap-3 px-3 py-2">
          <BrandMark className="size-8 shrink-0 rounded-lg" />
          <Button asChild variant="ghost" size="sm"><Link href={matter.data ? `/app/clients/${matter.data.client_id}/matters/${matterId}?tab=batches` : "/app/clients"}><ArrowLeft />Batches</Link></Button>
          <div className="min-w-0 flex-1 border-l pl-3">
            <h1 className="truncate text-sm font-semibold">{batch.data?.name ?? "Opening batch…"}</h1>
            <p className="truncate text-xs text-muted-foreground">{client.data?.name ? `${client.data.name} · ` : ""}{matter.data?.name ?? "Batch review"}</p>
          </div>
          {batch.data?.search_status && batch.data.search_status !== "READY" ? <Badge variant="outline">Search {batch.data.search_status.toLowerCase().replace("_", " ")}</Badge> : null}
          {run.data?.status === "COMPLETED" ? <Badge variant="accent"><Check />Review complete</Badge> : null}
          <HelpLink topic="reviewBatches" />
          <ThemeToggle />
        </div>
        <form onSubmit={submitSearch} role="search" className="flex items-center gap-2 border-t px-3 py-2">
          <Select value={searchMode} onValueChange={(value) => changeSearchMode(value as MatterSearchRequestSearchMode)} disabled={batch.data?.search_status !== "READY"}>
            <SelectTrigger className="w-32 shrink-0" aria-label="Search mode"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="KEYWORD">Keyword</SelectItem><SelectItem value="SEMANTIC">Semantic</SelectItem><SelectItem value="HYBRID">Hybrid</SelectItem></SelectContent>
          </Select>
          <div className="relative min-w-0 flex-1"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} className="pl-9" placeholder={SEARCH_PLACEHOLDERS[searchMode]} aria-label="Search batch documents" disabled={batch.data?.search_status !== "READY"} /></div>
          {searchMode === "SEMANTIC" ? <SemanticThresholdControl value={draftMinimumSimilarity} onChange={setDraftMinimumSimilarity} disabled={batch.data?.search_status !== "READY"} /> : null}
          <Button type="submit" disabled={batch.data?.search_status !== "READY"}>Search</Button>
        </form>
        <div className="flex h-10 items-center gap-3 border-t px-3 text-xs text-muted-foreground">
          <div className="h-1.5 min-w-24 flex-1 overflow-hidden rounded-full bg-muted" aria-label={`${percent}% reviewed`}><div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} /></div>
          <span className="shrink-0 tabular-nums">{processed.toLocaleString()} of {(progress.data?.document_count ?? batch.data?.document_count ?? 0).toLocaleString()} reviewed · {percent}%</span>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="flex w-96 shrink-0 flex-col border-r bg-card" aria-label="Batch documents">
          <div className="flex h-11 shrink-0 items-center justify-between border-b px-3">
            <h2 className="text-sm font-semibold">Batch results</h2>
            <span className="text-xs text-muted-foreground">{searchResults.data ? query.trim() && searchMode === "SEMANTIC" && minimumSimilarity === null ? `Top ${searchResults.data.total.toLocaleString()} candidates` : `${searchResults.data.total.toLocaleString()} matches` : ""}</span>
          </div>
          <div className="max-h-[40%] shrink-0 divide-y overflow-y-auto border-b">
            {taxonomy.data ? <BatchTopicFacet taxonomy={taxonomy.data} values={topicFacets.data} selected={selectedTopicKeys} onToggle={toggleTopic} /> : null}
            {facetDefinitions.map((definition) => <BatchFacetSection key={definition.id} matterId={matterId} batchId={batchId} searchRequest={searchRequest} definition={definition} selected={filters[definition.key] ?? []} custodianNames={new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian.display_name]))} onToggle={toggleFilter} />)}
            {!facetDefinitions.length ? <p className="p-3 text-xs text-muted-foreground">No batch filters are configured.</p> : null}
          </div>
          {batch.data?.search_status !== "READY" ? <div className="grid min-h-0 flex-1 place-items-center p-5 text-center"><div><p className="font-semibold">Preparing batch search</p><p className="mt-1 text-sm text-muted-foreground">Review search and document titles will appear when the batch projection is ready.</p>{batch.data?.search_error_message ? <p className="mt-2 text-xs text-destructive">{batch.data.search_error_message}</p> : null}</div></div>
            : searchResults.isPending ? <div className="space-y-2 p-3">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-20" />)}</div>
              : searchResults.data?.hits.length ? <ol className="min-h-0 flex-1 overflow-y-auto">{searchResults.data.hits.map((hit, index) => {
                const selected = hit.document_id === effectiveDocumentId;
                const state = statusByDocument.get(hit.document_id);
                return <li key={hit.document_id} className="border-b"><button type="button" onClick={() => selectDocument(hit.document_id)} aria-current={selected ? "true" : undefined} className={cn("w-full border-l-[3px] px-3 py-3 text-left outline-none hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", selected ? "border-l-accent bg-primary/8" : "border-l-transparent")}><div className="flex items-start gap-2"><span aria-label={`Result ${offset + index + 1}`} className="mt-0.5 w-8 shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{offset + index + 1}</span><div className="min-w-0 flex-1"><p className="line-clamp-2 text-sm font-semibold">{resultTitle(hit)}</p>{hit.best_passage ? <p className="mt-1 line-clamp-2 text-xs text-foreground/80">{hit.best_passage.text}</p> : null}<p className="mt-1 truncate text-xs text-muted-foreground">{displayValue(hit.fields.source_path)}</p></div><div className="flex shrink-0 flex-col items-end gap-1"><Badge variant="outline">{resultFileType(hit)}</Badge>{state ? <Badge variant={state.review_status === "COMPLETED" ? "accent" : "outline"}>{statusLabel(state.review_status)}</Badge> : null}</div></div></button></li>;
              })}</ol>
                : <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">No documents match this batch search.</div>}
          <div className="flex min-h-12 shrink-0 items-center border-t px-2 py-2">
            <ResultPagination total={searchResults.data?.total ?? 0} offset={offset} pageSize={PAGE_SIZE} disabled={searchResults.isFetching} onPageChange={changePage} />
          </div>
        </aside>

        <section className="flex min-h-0 min-w-0 flex-1 bg-background" aria-label="Selected document">
          {collectionItem.isPending && selectedHit ? <div className="w-full space-y-3 p-5">{Array.from({ length: 10 }, (_, index) => <Skeleton key={index} className="h-5" />)}</div>
            : collectionItem.error ? <div className="w-full p-5"><QueryError message={collectionItem.error.message} /></div>
              : collectionItem.data ? <DocumentViewerSurface item={collectionItem.data} className="h-full w-full" />
                : <div className="grid h-full w-full place-items-center p-8 text-center"><div><FileText className="mx-auto text-muted-foreground" /><p className="mt-3 font-semibold">Select a document</p></div></div>}
        </section>

        <aside className="flex w-[25rem] shrink-0 flex-col border-l bg-card" aria-label="Batch analysis and coding">
          <div className="flex h-11 shrink-0 items-center gap-1 border-b px-2"><Button type="button" size="sm" variant={sidePanel === "analysis" ? "outline" : "ghost"} disabled={!taxonomy.data} onClick={() => setSidePanel("analysis")}><Sparkles />Analysis</Button><Button type="button" size="sm" variant={sidePanel === "coding" ? "outline" : "ghost"} onClick={() => setSidePanel("coding")}>Coding</Button><span className="ml-auto">{coding.data ? <Badge variant="outline">{statusLabel(coding.data.review_status)}</Badge> : null}</span></div>
          {sidePanel === "analysis" && taxonomy.data ? <DocumentAnalysisPanel analysis={analysis.data} loading={analysis.isPending} error={analysis.error?.message} />
            : coding.isPending && effectiveDocumentId ? <div className="space-y-3 p-4">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-16" />)}</div>
            : coding.error ? <div className="p-4"><QueryError message={coding.error.message} /></div>
              : coding.data && batch.data && run.data ? <BatchCodingForm
                key={`${effectiveDocumentId}:${coding.dataUpdatedAt}`}
                batch={batch.data}
                coding={coding.data}
                disabled={run.data.status !== "RUNNING"}
                saving={save.isPending || skip.isPending}
                onSave={(payload) => save.mutateAsync(payload).then(() => undefined)}
                onSkip={() => skip.mutateAsync().then(() => undefined)}
              />
                : <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to begin coding.</div>}
        </aside>
      </div>
      <div className="flex h-11 shrink-0 items-center justify-center gap-2 border-t bg-card px-3">
        <Button variant="ghost" size="sm" disabled={currentIndex <= 0} onClick={() => searchResults.data?.hits[currentIndex - 1] && selectDocument(searchResults.data.hits[currentIndex - 1].document_id)}><ChevronLeft />Previous document</Button>
        <Button variant="ghost" size="sm" disabled={currentIndex < 0 || currentIndex >= (searchResults.data?.hits.length ?? 0) - 1} onClick={() => searchResults.data?.hits[currentIndex + 1] && selectDocument(searchResults.data.hits[currentIndex + 1].document_id)}>Next document<ChevronRight /></Button>
      </div>
    </main>
  );
}

function BatchTopicFacet({ taxonomy, values, selected, onToggle }: { taxonomy: BatchTopicTaxonomyRead; values?: MatterFacetValuesResponse; selected: string[]; onToggle: (topicKey: string) => void }) {
  const counts = new Map((values?.values ?? []).map((item) => [String(item.value), item.count]));
  return <section className="border-b bg-primary/5 p-3"><div className="mb-2 flex items-center justify-between gap-2"><h3 className="flex items-center gap-1.5 text-sm font-semibold"><Sparkles className="size-3.5 text-primary" />Batch topics</h3><Badge variant="outline">v{taxonomy.version}</Badge></div><div className="space-y-1">{taxonomy.topics.map((topic) => <label key={topic.id} className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" className="size-4 accent-primary" checked={selected.includes(topic.topic_key)} onChange={() => onToggle(topic.topic_key)} /><span className="min-w-0 flex-1 truncate" title={topic.description ?? topic.label}>{topic.label}</span><span className="font-mono text-xs text-muted-foreground">{(counts.get(topic.topic_key) ?? topic.assignment_count).toLocaleString()}</span></label>)}</div></section>;
}

function DocumentAnalysisPanel({ analysis, loading, error }: { analysis?: ReviewBatchDocumentAnalysisRead; loading: boolean; error?: string }) {
  const [citation, setCitation] = useState<string | null>(null);
  if (loading) return <div className="space-y-3 p-4">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-16" />)}</div>;
  if (error) return <div className="p-4"><QueryError message={error} /></div>;
  if (!analysis) return <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to view its assessment analysis.</div>;
  if (analysis.status !== "COMPLETED" || !analysis.analysis) return <div className="p-4"><StatusBadge status={analysis.status} /><p className="mt-2 text-sm text-muted-foreground">No completed structured analysis is available for this document.</p></div>;
  const payload = objectValue(analysis.analysis);
  const result = objectValue(payload.result);
  const paragraphMap = objectValue(payload.paragraph_map);
  const paragraphs = Array.isArray(paragraphMap.paragraphs) ? paragraphMap.paragraphs.map(objectValue) : [];
  const selectedParagraph = paragraphs.find((item) => item.paragraph_id === citation);
  return <div className="min-h-0 flex-1 overflow-y-auto p-4"><div className="mb-4 flex items-center justify-between gap-2"><StatusBadge status={String(result.determination ?? "UNCLEAR")} />{typeof result.confidence === "number" ? <Badge variant="outline">{Math.round(result.confidence * 100)}% confidence</Badge> : null}</div><AnalysisSection title="Document Summary" items={result.summary} onCitation={setCitation} /><AnalysisSection title="Responsiveness Summary" items={result.responsiveness_summary} onCitation={setCitation} /><AnalysisSection title="Clarification Requests" items={result.clarification_requests} onCitation={setCitation} />{selectedParagraph ? <div className="sticky bottom-0 mt-4 rounded-lg border border-primary/30 bg-background p-3 shadow-lg"><div className="flex items-center justify-between"><p className="text-xs font-bold text-primary">{String(selectedParagraph.paragraph_id)}</p><button type="button" className="text-xs text-muted-foreground" onClick={() => setCitation(null)}>Close</button></div><p className="mt-1 whitespace-pre-wrap text-xs leading-5">{String(selectedParagraph.text ?? "")}</p></div> : null}</div>;
}

function AnalysisSection({ title, items, onCitation }: { title: string; items: unknown; onCitation: (id: string) => void }) {
  const values = Array.isArray(items) ? items.map(objectValue) : [];
  return <section className="mb-5"><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{title}</h3>{values.length ? <div className="space-y-3">{values.map((item, index) => { const citations = Array.isArray(item.citation_ids) ? item.citation_ids.map(String) : []; return <div key={index} className="text-sm leading-6"><p>{String(item.text ?? item.question ?? item.reasoning ?? "")}</p>{item.rationale ? <p className="text-xs text-muted-foreground">{String(item.rationale)}</p> : null}<div className="mt-1 flex flex-wrap gap-1">{citations.map((id) => <button key={id} type="button" className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary hover:bg-primary/20" onClick={() => onCitation(id)}>{id}</button>)}</div></div>; })}</div> : <p className="text-sm text-muted-foreground">None.</p>}</section>;
}

function useDebouncedValue(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}

function BatchFacetSection({ matterId, batchId, searchRequest, definition, selected, custodianNames, onToggle }: {
  matterId: string;
  batchId: string;
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
    queryKey: ["review-batch-facet-values", matterId, batchId, definition.key, searchRequest, debouncedQuery],
    queryFn: () => coreApi<MatterFacetValuesResponse>(`/v1/matters/${matterId}/review-batches/${batchId}/facets/${definition.key}/values`, {
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

  return <section>
    <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="flex min-h-10 w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold hover:bg-muted/60">
      {open ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
      <span className="min-w-0 flex-1 truncate">{definition.display_name}</span>{selected.length ? <Badge variant="accent">{selected.length}</Badge> : null}
    </button>
    {open ? <div className="space-y-2 px-3 pb-3">
      {definition.type !== "BOOLEAN" ? <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input value={valueQuery} onChange={(event) => setValueQuery(event.target.value)} className="h-8 pl-8 text-sm" placeholder={`Find ${definition.display_name.toLowerCase()}…`} aria-label={`Find ${definition.display_name} values`} /></div> : null}
      {values.isPending ? <p className="text-xs text-muted-foreground">Loading values…</p>
        : values.error ? <p className="text-xs text-destructive">Values could not be loaded.</p>
          : options.length ? <div className="space-y-0.5">{options.map((option) => {
            const token = facetToken(option.value);
            const label = labelFor(token);
            return <label key={token} className="flex min-h-8 cursor-pointer items-center gap-2 overflow-hidden rounded-md px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" checked={selected.includes(token)} onChange={() => onToggle(definition.key, token)} className="size-4 shrink-0 accent-primary" /><span className="min-w-0 flex-1 truncate" title={label}>{label}</span><span className="font-mono text-xs tabular-nums text-muted-foreground">{option.count.toLocaleString()}</span></label>;
          })}</div> : <p className="text-xs text-muted-foreground">No matching values.</p>}
    </div> : null}
  </section>;
}

function BatchCodingForm({ batch, coding, disabled, saving, onSave, onSkip }: {
  batch: ReviewBatchRead;
  coding: ReviewBatchDocumentCodingRead;
  disabled: boolean;
  saving: boolean;
  onSave: (payload: ReviewBatchRunDocumentValues) => Promise<void>;
  onSkip: () => Promise<void>;
}) {
  const fields = useMemo(() => {
    const unique = new Map<string, { field: ReviewBatchCodingFieldRead; definition: SnapshotDefinition }>();
    for (const group of batch.coding_groups) {
      for (const field of group.fields) {
        if (!unique.has(field.metadata_definition_id)) {
          unique.set(field.metadata_definition_id, { field, definition: snapshot(field) });
        }
      }
    }
    return [...unique.values()];
  }, [batch.coding_groups]);
  const initial = useMemo(() => Object.fromEntries(fields.map(({ field, definition }) => [
    field.metadata_definition_id,
    coding.values.filter((value) => value.metadata_definition_id === field.metadata_definition_id).map((value) => editorValue(value.value, definition)),
  ])), [coding.values, fields]);
  const [drafts, setDrafts] = useState<Record<string, string[]>>(initial);
  const dirty = JSON.stringify(drafts) !== JSON.stringify(initial);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await onSave({
        matter_document_id: coding.matter_document_id,
        fields: fields.map(({ field, definition }) => ({
          metadata_definition_id: field.metadata_definition_id,
          values: (drafts[field.metadata_definition_id] ?? []).filter((value) => value.trim()).map((value) => parseValue(value, definition)),
          confidence: null,
        })),
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Check the entered values.");
    }
  };

  return <form className="flex min-h-0 flex-1 flex-col overflow-hidden" onSubmit={submit}>
    <div className="min-h-0 flex-1 divide-y overflow-y-auto">
      {batch.coding_groups.map((group) => <section key={group.id} className="p-3"><h3 className="mb-3 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{group.display_name}</h3><div className="space-y-4">{group.fields.map((field) => {
        const definition = snapshot(field);
        const visible = coding.reviewer_values.filter((value) => value.metadata_definition_id === field.metadata_definition_id);
        return <div key={field.id}><label className="text-xs font-medium text-muted-foreground" htmlFor={`batch-field-${field.id}`}>{definition.display_name}</label>{definition.description ? <p className="mt-0.5 text-xs text-muted-foreground">{definition.description}</p> : null}<div className="mt-1.5"><BatchFieldInput id={`batch-field-${field.id}`} definition={definition} values={drafts[field.metadata_definition_id] ?? []} disabled={disabled || saving} onChange={(values) => setDrafts((current) => ({ ...current, [field.metadata_definition_id]: values }))} /></div>{visible.map((value) => <div key={value.review_batch_run_id} className="mt-2 rounded-md border bg-muted/35 px-2.5 py-2 text-xs"><span className="font-medium">{value.actor_user.display_name}</span><span className="text-muted-foreground"> · {value.values.map(String).join(", ") || "Not set"}</span></div>)}</div>;
      })}</div></section>)}
      {!fields.length ? <p className="p-4 text-sm text-muted-foreground">This batch has no coding groups. Saving will still mark the document reviewed.</p> : null}
    </div>
    <div className="flex shrink-0 items-center justify-between gap-2 border-t p-3"><Button type="button" variant="ghost" size="sm" disabled={disabled || saving} onClick={onSkip}><SkipForward />Skip</Button><div className="flex items-center gap-2"><span className="text-xs text-muted-foreground">{dirty ? "Unsaved changes" : coding.review_status === "COMPLETED" ? "Saved" : "Ready"}</span><Button type="submit" size="sm" disabled={disabled || saving}><Save />{saving ? "Saving…" : "Save & next"}</Button></div></div>
  </form>;
}

function BatchFieldInput({ id, definition, values, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  if (definition.cardinality === "MULTIPLE") return <MultiValueInput id={id} definition={definition} values={values} disabled={disabled} onChange={onChange} />;
  return <SingleValueInput id={id} definition={definition} value={values[0] ?? ""} disabled={disabled} onChange={(value) => onChange([value])} />;
}

function MultiValueInput({ id, definition, values, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  const [entry, setEntry] = useState("");
  const add = () => {
    if (!entry.trim() || values.includes(entry)) return;
    onChange([...values, entry]);
    setEntry("");
  };
  return <div><div className="flex flex-wrap gap-1.5">{values.map((value) => <Badge key={value} variant="outline"><span>{value}</span><button type="button" disabled={disabled} className="ml-1" aria-label={`Remove ${value}`} onClick={() => onChange(values.filter((item) => item !== value))}><X className="size-3" /></button></Badge>)}</div><div className="mt-2 flex gap-2"><SingleValueInput id={id} definition={definition} value={entry} disabled={disabled} onChange={setEntry} /><Button type="button" variant="outline" size="sm" disabled={disabled || !entry.trim()} onClick={add}>Add</Button></div></div>;
}

function SingleValueInput({ id, definition, value, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  if (definition.type === "ENUM") return <Select value={value || "__empty"} disabled={disabled} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id}><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem>{definition.allowed_values.filter((option) => option.active !== false).map((option) => <SelectItem key={option.key} value={option.key}>{option.label}</SelectItem>)}</SelectContent></Select>;
  if (definition.type === "BOOLEAN") return <Select value={value || "__empty"} disabled={disabled} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id}><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem><SelectItem value="true">Yes</SelectItem><SelectItem value="false">No</SelectItem></SelectContent></Select>;
  if (definition.type === "LONG_TEXT" || definition.type === "JSON") return <Textarea id={id} disabled={disabled} className="min-h-20" value={value} onChange={(event) => onChange(event.target.value)} />;
  const type = definition.type === "INTEGER" || definition.type === "DECIMAL" ? "number" : definition.type === "DATE" ? "date" : definition.type === "DATETIME" ? "datetime-local" : "text";
  return <Input id={id} disabled={disabled} type={type} step={definition.type === "DECIMAL" ? "any" : undefined} value={value} onChange={(event) => onChange(event.target.value)} />;
}
