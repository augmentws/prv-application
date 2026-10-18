"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, BarChart3, Check, ChevronDown, ChevronLeft, ChevronRight, Clock3, Files, FileText, Filter, LoaderCircle, MessageSquareText, Play, RotateCcw, Save, Search, SkipForward, Sparkles, Upload, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { BrandMark } from "@/components/brand-mark";
import { MatterSectionMenu } from "@/components/matter/matter-section-menu";
import { BatchChatPanel } from "@/components/batch-chat-panel";
import { ClassificationAnalysisJobDetail } from "@/components/classification-analysis-job-detail";
import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import { AssignBatchCodingGroupsDialog } from "@/components/forms/assign-batch-coding-groups-dialog";
import { HelpLink } from "@/components/help-link";
import { QueryError } from "@/components/query-state";
import { ResultPagination } from "@/components/result-pagination";
import { SearchExportDialog } from "@/components/search-export-dialog";
import { StatusBadge } from "@/components/status-badge";
import { parseMinimumSimilarity, SemanticThresholdControl } from "@/components/semantic-threshold-control";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type {
  ClientRead,
  BatchTopicTaxonomyRead,
  CollectionItemRead,
  CustodianRead,
  JobExecutionRead,
  MatterRead,
  MatterAnalysisTaskRead,
  MatterAnalysisTaskRunRead,
  MatterFacetValuesResponse,
  MatterSearchFilter,
  MatterSearchHit,
  MatterSearchRequest,
  MatterSearchRequestSearchMode,
  MatterSearchResponse,
  MetadataDefinitionRead,
  MetadataGroupRead,
  ReviewBatchCodingFieldRead,
  ReviewBatchCodingHistoryEntryRead,
  ReviewBatchDocumentCodingRead,
  ReviewBatchDocumentAnalysisRead,
  ReviewBatchDocumentRead,
  ReviewBatchRead,
  ReviewBatchAnalysisJobRead,
  ReviewBatchRunDocumentValues,
  ReviewBatchRunProgressRead,
  ReviewBatchReviewerCodingRead,
  ReviewBatchReviewerProgressRead,
  ReviewBatchRunPromotionRead,
  ReviewBatchRunRead,
  ReviewBatchSearchCodingFieldRead,
  ReviewBatchSearchRequest,
  ReviewDecisionResultRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import type { BatchDocumentReference } from "@/lib/document-references";
import { normalizeParagraphReference, parseParagraphNumbers } from "@/lib/document-references";
import { HAS_VALUE_FILTER_TOKEN, NO_VALUE_FILTER_TOKEN, toggleFacetFilterToken } from "@/lib/search-filters";
import { cn } from "@/lib/utils";
import { matterHref } from "@/lib/matter-routes";

const PAGE_SIZE = 50;
const FACET_PAGE_SIZE = 8;
const PREFERRED_FACETS = ["custodian", "file_extension", "responsiveness", "privilege", "key_document", "topics"];
const SEARCH_PLACEHOLDERS: Record<MatterSearchRequestSearchMode, string> = {
  KEYWORD: "Search this batch's body, filenames, paths, email headers, and metadata",
  SEMANTIC: "Find documents in this batch by concept or meaning",
  HYBRID: "Combine exact words with conceptually related batch results",
};

type SelectedFilters = Record<string, string[]>;
type CodingConfidencePreset = "any" | "90-plus" | "80-90" | "70-80" | "custom";

interface CodingConfidenceRange {
  preset: CodingConfidencePreset;
  minimum: number | null;
  maximum: number | null;
}

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

function typedBatchCodingValue(token: string, field: ReviewBatchSearchCodingFieldRead): unknown {
  if (field.type === "BOOLEAN") return token === "true";
  if (field.type === "INTEGER" || field.type === "DECIMAL") return Number(token);
  return token;
}

interface SnapshotOption {
  key: string;
  label: string;
  active?: boolean;
}

interface AnalysisDestinationFieldSnapshot {
  id: string;
  key: string;
  display_name: string;
}

function analysisDestinationFields(
  run: ReviewBatchRunRead | undefined,
  promotion?: ReviewBatchRunPromotionRead | null,
): AnalysisDestinationFieldSnapshot[] {
  const configured = promotion?.destination_snapshot.length
    ? promotion.destination_snapshot
    : run?.configuration_snapshot.destination_fields;
  if (!Array.isArray(configured)) return [];
  return configured.filter((value): value is AnalysisDestinationFieldSnapshot => Boolean(
    value
    && typeof value === "object"
    && "id" in value && typeof value.id === "string"
    && "key" in value && typeof value.key === "string"
    && "display_name" in value && typeof value.display_name === "string",
  ));
}

function decisionProviderLabel(value: unknown) {
  return value === "OPENAI_DECISIONS" ? "OpenAI Decisions" : "Jev";
}

function publishedTaskProvider(task: MatterAnalysisTaskRead) {
  const version = task.published_version_details
    ?? (task.published_version === task.current_version ? task.version : null);
  return {
    label: decisionProviderLabel(version?.decision_provider),
    model: version?.decision_model ?? null,
  };
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

export function BatchReviewWorkspace({ matterId, batchId, initialDocumentId, initialParagraphReference, initialPage = 1, initialAnalysisDialogOpen = false }: {
  matterId: string;
  batchId: string;
  initialDocumentId?: string;
  initialParagraphReference?: string;
  initialPage?: number;
  initialAnalysisDialogOpen?: boolean;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [searchMode, setSearchMode] = useState<MatterSearchRequestSearchMode>("KEYWORD");
  const [draftMinimumSimilarity, setDraftMinimumSimilarity] = useState("");
  const [minimumSimilarity, setMinimumSimilarity] = useState<number | null>(null);
  const [filters, setFilters] = useState<SelectedFilters>({});
  const [negatedFilterFields, setNegatedFilterFields] = useState(() => new Set<string>());
  const [codingFilters, setCodingFilters] = useState<SelectedFilters>({});
  const [codingConfidence, setCodingConfidence] = useState<Record<string, CodingConfidenceRange>>({});
  const [offset, setOffset] = useState(Math.max(0, initialPage - 1) * PAGE_SIZE);
  const [selectedDocumentId, setSelectedDocumentId] = useState(initialDocumentId ?? "");
  const [paragraphReference, setParagraphReference] = useState(() => normalizeParagraphReference(initialParagraphReference));
  const [selectedTopicKeys, setSelectedTopicKeys] = useState<string[]>([]);
  const [leftPanel, setLeftPanel] = useState<"filters" | "documents">("documents");
  const [filterFocusRequest, setFilterFocusRequest] = useState(0);
  const [sidePanel, setSidePanel] = useState<"analysis" | "coding" | "history" | "chat">("analysis");
  const [analysisDialogOpen, setAnalysisDialogOpen] = useState(initialAnalysisDialogOpen);
  const [promotionDialogOpen, setPromotionDialogOpen] = useState(false);
  const [analysisTaskId, setAnalysisTaskId] = useState("");
  const [selectedAnalysisRunId, setSelectedAnalysisRunId] = useState("");
  const filterScrollRef = useRef<HTMLDivElement>(null);
  const searchableCodingFieldsRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (leftPanel !== "filters" || filterFocusRequest === 0) return;
    const container = filterScrollRef.current;
    const target = searchableCodingFieldsRef.current;
    if (!container || !target) return;
    const containerRect = container.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    container.scrollTo?.({
      top: Math.max(0, container.scrollTop + targetRect.top - containerRect.top),
      behavior: "smooth",
    });
    target.focus({ preventScroll: true });
  }, [filterFocusRequest, leftPanel]);

  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const client = useQuery({
    queryKey: ["client", matter.data?.client_id],
    queryFn: () => coreApi<ClientRead>(`/v1/clients/${matter.data!.client_id}`),
    enabled: Boolean(matter.data?.client_id),
  });
  const custodians = useQuery({
    queryKey: ["matter-custodians", matterId],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/matters/${matterId}/custodians`),
  });
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
  });
  const groups = useQuery({
    queryKey: ["metadata-groups", matterId],
    queryFn: () => coreApi<MetadataGroupRead[]>(`/v1/matters/${matterId}/metadata-groups`),
  });
  const batch = useQuery({
    queryKey: ["review-batch", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}`),
    refetchInterval: (query) => ["QUEUED", "SYNCING"].includes(query.state.data?.searchable_coding_status ?? "") ? 1000 : false,
  });
  const analysisTasks = useQuery({
    queryKey: ["analysis-tasks", matterId],
    queryFn: () => coreApi<MatterAnalysisTaskRead[]>(`/v1/matters/${matterId}/analysis-tasks`),
    enabled: analysisDialogOpen,
  });
  const batchRuns = useQuery({
    queryKey: ["review-batch-runs", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchRunRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/runs`),
    enabled: batch.data?.status === "READY",
    refetchInterval: (query) => query.state.data?.some((item) => ["WORKFLOW", "CLASSIFIER"].includes(item.run_type) && ["QUEUED", "RUNNING"].includes(item.status)) ? 1000 : false,
  });
  const batchAnalysisJobs = useQuery({
    queryKey: ["review-batch-analysis-jobs", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchAnalysisJobRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/analysis-runs`),
    enabled: batch.data?.status === "READY",
    refetchInterval: (query) => query.state.data?.some((item) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(item.status)) ? 1000 : false,
  });
  const analysisJobs = batchAnalysisJobs.data ?? [];
  const selectedCodingRun = (batchRuns.data ?? []).find((item) => item.id === batch.data?.searchable_coding_run_id);
  const projectedMinimum = selectedCodingRun?.run_type === "CLASSIFIER" && typeof selectedCodingRun.configuration_snapshot.minimum_confidence === "number"
    ? selectedCodingRun.configuration_snapshot.minimum_confidence
    : null;
  const projectedMaximum = selectedCodingRun?.run_type === "CLASSIFIER" && typeof selectedCodingRun.configuration_snapshot.maximum_confidence === "number"
    ? selectedCodingRun.configuration_snapshot.maximum_confidence
    : null;
  const taxonomy = useQuery({
    queryKey: ["review-batch-topic-taxonomy", matterId, batchId],
    queryFn: () => coreApi<BatchTopicTaxonomyRead | null>(`/v1/matters/${matterId}/review-batches/${batchId}/topic-taxonomy`),
    enabled: batch.data?.status === "READY",
  });
  const matterDefinitionAssessment = taxonomy.data?.review_batch_run_id ? {
    runId: taxonomy.data.review_batch_run_id,
    version: taxonomy.data.version,
    createdAt: taxonomy.data.created_at,
  } : undefined;
  const viewingMatterDefinition = Boolean(
    matterDefinitionAssessment
    && (selectedAnalysisRunId === matterDefinitionAssessment.runId || analysisJobs.length === 0),
  );
  const selectedAnalysisJob = viewingMatterDefinition
    ? undefined
    : analysisJobs.find((item) => item.id === selectedAnalysisRunId) ?? analysisJobs[0];
  const selectedAnalysisRun = selectedAnalysisJob?.review_batch_run ?? undefined;
  const selectedClassificationRun = selectedAnalysisJob?.classification_analysis_run ?? undefined;
  const selectedClassifierProjection = selectedClassificationRun?.classification_scoring_run_id
    ? (batchRuns.data ?? []).find((item) => item.run_type === "CLASSIFIER" && item.classification_scoring_run_id === selectedClassificationRun.classification_scoring_run_id)
    : undefined;
  const selectedClassifierProjectionId = selectedClassifierProjection?.id;
  const selectedClassifierProjectionStatus = selectedClassifierProjection?.status;
  const selectedAnalysisViewId = selectedAnalysisJob?.id ?? matterDefinitionAssessment?.runId ?? "";

  useEffect(() => {
    if (!selectedClassifierProjectionId || !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"].includes(selectedClassifierProjectionStatus ?? "")) return;
    void queryClient.invalidateQueries({ queryKey: ["review-batch", matterId, batchId] });
  }, [batchId, matterId, queryClient, selectedClassifierProjectionId, selectedClassifierProjectionStatus]);
  const run = useQuery({
    queryKey: ["review-batch-run", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchReviewerCodingRead>(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding`, { method: "POST" }),
    enabled: batch.data?.status === "READY",
    retry: false,
  });
  const progress = useQuery({
    queryKey: ["review-batch-progress", matterId, batchId, run.data?.id],
    queryFn: () => coreApi<ReviewBatchReviewerProgressRead>(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding/progress`),
    enabled: Boolean(run.data?.id),
  });

  const facetDefinitions = useMemo(() => (definitions.data ?? [])
    .filter((definition) => definition.status === "ACTIVE" && definition.searchable && definition.facetable && ["TEXT", "ENUM", "BOOLEAN"].includes(definition.type))
    .sort((left, right) => {
      const leftRank = PREFERRED_FACETS.indexOf(left.key);
      const rightRank = PREFERRED_FACETS.indexOf(right.key);
      return (leftRank < 0 ? 1000 : leftRank) - (rightRank < 0 ? 1000 : rightRank) || left.display_name.localeCompare(right.display_name);
    }), [definitions.data]);
  const searchRequest = useMemo<ReviewBatchSearchRequest>(() => {
    const definitionByKey = new Map(facetDefinitions.map((definition) => [definition.key, definition]));
    const searchFilters = Object.entries(filters).flatMap<MatterSearchFilter>(([key, values]) => {
      const definition = definitionByKey.get(key);
      if (!definition || !values.length) return [];
      const negated = negatedFilterFields.has(key);
      if (values.includes(HAS_VALUE_FILTER_TOKEN)) return [{ field: key, operator: negated ? "NOT_EXISTS" : "EXISTS" }];
      if (values.includes(NO_VALUE_FILTER_TOKEN) && negated) return [{ field: key, operator: "EXISTS" }];
      const includeMissing = values.includes(NO_VALUE_FILTER_TOKEN);
      const selectedValues = values.filter((value) => ![HAS_VALUE_FILTER_TOKEN, NO_VALUE_FILTER_TOKEN].includes(value));
      if (!selectedValues.length && includeMissing) return [{ field: key, operator: "NOT_EXISTS" }];
      return [{ field: key, operator: negated ? "NOT_IN" : "IN", values: selectedValues.map((value) => typedFacetValue(value, definition)), ...(!negated && includeMissing ? { include_missing: true } : {}) }];
    });
    const effectiveMode = query.trim() ? searchMode : "KEYWORD";
    return {
      query: query.trim() || null,
      search_mode: effectiveMode,
      minimum_similarity: effectiveMode === "SEMANTIC" ? minimumSimilarity : null,
      filters: searchFilters,
      coding_filters: Object.entries(codingFilters).flatMap(([key, values]) => {
        const field = batch.data?.searchable_coding_fields.find((item) => item.key === key);
        if (!field || !values.length) return [];
        const confidence = codingConfidence[key];
        return [{
          field: key,
          values: values.map((value) => typedBatchCodingValue(value, field)),
          minimum_confidence: confidence?.minimum ?? null,
          maximum_confidence: confidence?.maximum ?? null,
        }];
      }),
      facets: [],
      sort: query.trim() ? [{ field: "_score", direction: "DESC" }] : [{ field: "created_at", direction: "DESC" }],
      offset,
      size: PAGE_SIZE,
    };
  }, [batch.data?.searchable_coding_fields, codingConfidence, codingFilters, facetDefinitions, filters, minimumSimilarity, negatedFilterFields, offset, query, searchMode]);
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
      const params = new URLSearchParams({ reviewer_coding_id: run.data!.id, offset: "0", limit: String(PAGE_SIZE) });
      for (const documentId of pageDocumentIds) params.append("document_id", documentId);
      return coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?${params}`);
    },
    enabled: Boolean(run.data?.id && pageDocumentIds.length),
  });
  const statusByDocument = useMemo(() => new Map((statusQuery.data ?? []).map((document) => [document.matter_document_id, document])), [statusQuery.data]);
  const selectedHit = searchResults.data?.hits.find((hit) => hit.document_id === selectedDocumentId);
  const selectedDocument = useQuery({
    queryKey: ["review-batch-document", matterId, batchId, run.data?.id, selectedDocumentId],
    queryFn: async () => {
      const params = new URLSearchParams({ reviewer_coding_id: run.data!.id, document_id: selectedDocumentId, offset: "0", limit: "1" });
      const documents = await coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?${params}`);
      return documents[0] ?? null;
    },
    enabled: Boolean(run.data?.id && selectedDocumentId && !selectedHit),
  });
  const effectiveDocumentId = selectedDocumentId || (
    searchResults.data?.hits.find((hit) => statusByDocument.get(hit.document_id)?.review_status === "NOT_STARTED")?.document_id
      ?? searchResults.data?.hits[0]?.document_id
      ?? ""
  );
  const effectiveHit = searchResults.data?.hits.find((hit) => hit.document_id === effectiveDocumentId);
  const selectedCollectionItemId = typeof effectiveHit?.fields.collection_item_id === "string"
    ? effectiveHit.fields.collection_item_id
    : selectedDocument.data?.collection_item_id ?? "";
  const highlightedParagraphs = useMemo(() => parseParagraphNumbers(paragraphReference), [paragraphReference]);
  const collectionItem = useQuery({
    queryKey: ["matter-document-collection-item", matterId, effectiveDocumentId],
    queryFn: () => coreApi<CollectionItemRead>(`/v1/matters/${matterId}/documents/${effectiveDocumentId}/collection-item`),
    enabled: Boolean(effectiveDocumentId),
  });
  const coding = useQuery({
    queryKey: ["review-batch-document-coding", matterId, batchId, run.data?.id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchDocumentCodingRead>(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding/documents/${effectiveDocumentId}`),
    enabled: Boolean(run.data?.id && effectiveDocumentId),
  });
  const analysis = useQuery({
    queryKey: ["review-batch-document-analysis", matterId, batchId, taxonomy.data?.review_batch_run_id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchDocumentAnalysisRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${taxonomy.data!.review_batch_run_id}/documents/${effectiveDocumentId}/analysis`),
    enabled: sidePanel === "analysis" && viewingMatterDefinition && Boolean(taxonomy.data?.review_batch_run_id && effectiveDocumentId),
    retry: false,
  });
  const codingHistory = useQuery({
    queryKey: ["review-batch-coding-history", matterId, batchId, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchCodingHistoryEntryRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents/${effectiveDocumentId}/coding-history`),
    enabled: sidePanel === "history" && Boolean(effectiveDocumentId),
  });
  const analysisRunProgress = useQuery({
    queryKey: ["review-batch-progress", matterId, batchId, selectedAnalysisRun?.id],
    queryFn: () => coreApi<ReviewBatchRunProgressRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${selectedAnalysisRun!.id}/progress`),
    enabled: Boolean(selectedAnalysisRun?.id),
    refetchInterval: ["QUEUED", "RUNNING"].includes(selectedAnalysisRun?.status ?? "") ? 1000 : false,
  });
  const analysisDocumentStatus = useQuery({
    queryKey: ["review-batch-analysis-document", matterId, batchId, selectedAnalysisRun?.id, effectiveDocumentId],
    queryFn: async () => {
      const params = new URLSearchParams({ run_id: selectedAnalysisRun!.id, document_id: effectiveDocumentId, offset: "0", limit: "1" });
      const documents = await coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?${params}`);
      return documents[0] ?? null;
    },
    enabled: Boolean(selectedAnalysisRun?.id && effectiveDocumentId),
    refetchInterval: ["QUEUED", "RUNNING"].includes(selectedAnalysisRun?.status ?? "") ? 1000 : false,
  });
  const decisionResult = useQuery({
    queryKey: ["review-decision-result", matterId, batchId, selectedAnalysisRun?.id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewDecisionResultRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${selectedAnalysisRun!.id}/documents/${effectiveDocumentId}/decision-result`),
    enabled: sidePanel === "analysis" && analysisDocumentStatus.data?.review_status === "COMPLETED",
    retry: false,
  });
  const analysisPromotion = useQuery({
    queryKey: ["review-batch-analysis-promotion", matterId, batchId, selectedAnalysisRun?.id],
    queryFn: () => coreApi<ReviewBatchRunPromotionRead | null>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${selectedAnalysisRun!.id}/promotion`),
    enabled: Boolean(selectedAnalysisRun?.id && ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(selectedAnalysisRun.status)),
    refetchInterval: (query) => ["QUEUED", "RUNNING"].includes(query.state.data?.status ?? "") ? 1000 : false,
  });

  const syncUrl = (documentId?: string, nextOffset = offset, nextParagraphReference?: string) => {
    const params = new URLSearchParams({ batch: batchId });
    if (query.trim()) params.set("q", query.trim());
    if (searchMode !== "KEYWORD") params.set("mode", searchMode.toLowerCase());
    if (query.trim() && searchMode === "SEMANTIC" && minimumSimilarity !== null) {
      params.set("similarity", String(minimumSimilarity));
    }
    if (nextOffset) params.set("page", String(Math.floor(nextOffset / PAGE_SIZE) + 1));
    for (const [key, values] of Object.entries(filters)) {
      for (const value of values) {
        if (value === NO_VALUE_FILTER_TOKEN) params.set(`m_${key}`, "1");
        else if (value === HAS_VALUE_FILTER_TOKEN) params.set(`e_${key}`, "1");
        else params.append(`f_${key}`, value);
      }
      if (negatedFilterFields.has(key) && values.length) params.set(`n_${key}`, "1");
    }
    if (documentId) params.set("document", documentId);
    if (documentId && nextParagraphReference) params.set("paragraph", nextParagraphReference);
    router.replace(`/review/matters/${matterId}?${params}`, { scroll: false });
  };
  const selectDocument = (documentId: string, nextParagraphReference?: string) => {
    const normalizedParagraphReference = normalizeParagraphReference(nextParagraphReference);
    setSelectedDocumentId(documentId);
    setParagraphReference(normalizedParagraphReference);
    syncUrl(documentId, offset, normalizedParagraphReference);
  };
  const openDocumentReference = (reference: BatchDocumentReference) => {
    selectDocument(reference.documentId, reference.paragraphReference);
  };
  const changePage = (nextOffset: number) => {
    setOffset(nextOffset);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
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
    setParagraphReference(undefined);
    setLeftPanel("documents");
  };

  const changeSearchMode = (nextMode: MatterSearchRequestSearchMode) => {
    setSearchMode(nextMode);
    setMinimumSimilarity(nextMode === "SEMANTIC" ? parseMinimumSimilarity(draftMinimumSimilarity) : null);
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };

  const toggleFilter = (key: string, value: string) => {
    setFilters((current) => {
      const next = { ...current };
      const selected = next[key] ?? [];
      next[key] = toggleFacetFilterToken(selected, value);
      if (!next[key].length) delete next[key];
      return next;
    });
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };
  const toggleFilterNegation = (key: string) => {
    setNegatedFilterFields((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };
  const toggleCodingFilter = (key: string, value: string) => {
    setCodingFilters((current) => {
      const next = { ...current };
      const selected = next[key] ?? [];
      next[key] = selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value];
      if (!next[key].length) delete next[key];
      return next;
    });
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };
  const setCodingConfidenceRange = (key: string, range: CodingConfidenceRange) => {
    setCodingConfidence((current) => ({ ...current, [key]: range }));
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };
  const toggleTopic = (topicKey: string) => {
    setSelectedTopicKeys((current) => current.includes(topicKey) ? current.filter((item) => item !== topicKey) : [...current, topicKey]);
    setOffset(0);
    setSelectedDocumentId("");
    setParagraphReference(undefined);
  };

  const refreshReview = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["review-batch-document-statuses", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-progress", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-document-coding", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-coding-history", matterId, batchId] }),
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
    mutationFn: (payload: ReviewBatchRunDocumentValues) => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding/documents/${effectiveDocumentId}/values`, { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document coding saved.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document coding could not be saved."),
  });
  const skip = useMutation({
    mutationFn: () => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding/documents/${effectiveDocumentId}/skip`, { method: "POST" }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document skipped.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document could not be skipped."),
  });
  const assignCodingGroups = useMutation({
    mutationFn: ({ targetBatchId, codingGroupIds }: { targetBatchId: string; codingGroupIds: string[] }) => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${targetBatchId}/coding-groups`, { method: "PUT", body: JSON.stringify({ coding_group_ids: codingGroupIds }) }),
    onSuccess: async (updated) => {
      queryClient.setQueryData(["review-batch", matterId, batchId], updated);
      await queryClient.invalidateQueries({ queryKey: ["review-batch-document-coding", matterId, batchId] });
      toast.success("Coding groups for this batch were updated.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The coding groups could not be updated."),
  });
  const publishedAnalysisTasks = (analysisTasks.data ?? []).filter((task) => task.status === "ACTIVE" && task.published_version !== null);
  const effectiveAnalysisTask = publishedAnalysisTasks.find((task) => task.id === analysisTaskId) ?? publishedAnalysisTasks[0];
  const effectiveAnalysisTaskId = effectiveAnalysisTask?.id ?? "";
  const effectiveAnalysisProvider = effectiveAnalysisTask?.task_type === "QUESTION_ANSWERING" ? publishedTaskProvider(effectiveAnalysisTask) : null;
  const effectiveClassificationVersion = effectiveAnalysisTask?.task_type === "CLASSIFICATION"
    ? effectiveAnalysisTask.published_version_details
    : null;
  const effectiveClassificationEmbeddingJobId = effectiveClassificationVersion?.classification_embedding_job_id ?? null;
  const hasUnpublishedAnalysisDraft = Boolean(
    effectiveAnalysisTask?.published_version
    && effectiveAnalysisTask.current_version > effectiveAnalysisTask.published_version,
  );
  const openAnalysisDialog = (taskId?: string) => {
    setAnalysisTaskId(taskId ?? "");
    setAnalysisDialogOpen(true);
  };
  const viewSearchableCodingFields = () => {
    setLeftPanel("filters");
    setFilterFocusRequest((current) => current + 1);
  };
  const launchAnalysis = useMutation({
    mutationFn: async () => {
      if (!effectiveAnalysisTask) throw new Error("Select a published Analysis Task");
      if (effectiveAnalysisTask.task_type === "CLASSIFICATION") {
        if (!effectiveClassificationEmbeddingJobId) {
          throw new Error("The published classifier does not identify compatible document embeddings");
        }
        const created = await coreApi<MatterAnalysisTaskRunRead>(`/v1/matters/${matterId}/analysis-tasks/${effectiveAnalysisTask.id}/runs`, {
          method: "POST",
          body: JSON.stringify({
            scope_type: "REVIEW_BATCH",
            scope_id: batchId,
            embedding_job_id: effectiveClassificationEmbeddingJobId,
            idempotency_key: crypto.randomUUID(),
          }),
        });
        return { taskType: "CLASSIFICATION" as const, created };
      }
      const created = await coreApi<ReviewBatchRunRead>(`/v1/matters/${matterId}/review-batches/${batchId}/analysis-runs`, {
        method: "POST",
        body: JSON.stringify({ matter_analysis_task_id: effectiveAnalysisTask.id }),
      });
      return { taskType: "QUESTION_ANSWERING" as const, created };
    },
    onSuccess: ({ taskType, created }) => {
      const task = effectiveAnalysisTask;
      const analysisJob: ReviewBatchAnalysisJobRead = {
        id: created.id,
        job_type: taskType === "CLASSIFICATION" ? "CLASSIFICATION_ANALYSIS_TASK" : "ANALYSIS_TASK_BATCH_RUN",
        task_type: taskType,
        matter_analysis_task_id: task?.id ?? null,
        task_name: task?.name ?? (taskType === "CLASSIFICATION" ? "Classification analysis" : "Question answering run"),
        task_version: task?.published_version ?? null,
        status: created.status,
        created_at: created.created_at,
        updated_at: created.updated_at,
        execution: created.execution,
        review_batch_run: taskType === "QUESTION_ANSWERING" ? created : null,
        classification_analysis_run: taskType === "CLASSIFICATION" ? created : null,
      };
      queryClient.setQueryData<ReviewBatchAnalysisJobRead[]>(["review-batch-analysis-jobs", matterId, batchId], (current) => [analysisJob, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setSelectedAnalysisRunId(created.id);
      setSidePanel("analysis");
      if (taskType === "QUESTION_ANSWERING") {
        queryClient.setQueryData<ReviewBatchRunRead[]>(["review-batch-runs", matterId, batchId], (current) => current ? [created, ...current] : [created]);
        toast.success("Batch analysis was queued.");
      } else {
        void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId, effectiveAnalysisTaskId] });
        void queryClient.invalidateQueries({ queryKey: ["matter-classification-analysis-jobs", matterId] });
        toast.success("Batch classification was queued.");
      }
      setAnalysisDialogOpen(false);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The batch analysis could not be started."),
  });
  const cancelAnalysis = useMutation({
    mutationFn: ({ jobType, runId }: { jobType: ReviewBatchAnalysisJobRead["job_type"]; runId: string }) => coreApi<JobExecutionRead>(`/v1/jobs/${jobType}/${runId}/cancel`, { method: "POST" }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["review-batch-runs", matterId, batchId] }),
        queryClient.invalidateQueries({ queryKey: ["review-batch-analysis-jobs", matterId, batchId] }),
        queryClient.invalidateQueries({ queryKey: ["review-batch-progress", matterId, batchId] }),
        queryClient.invalidateQueries({ queryKey: ["review-batch-analysis-document", matterId, batchId] }),
      ]);
      toast.success("The analysis run was canceled.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The analysis run could not be canceled."),
  });
  const makeAnalysisSearchable = useMutation({
    mutationFn: (runId: string) => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${runId}/coding-search`, { method: "POST" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["review-batch", matterId, batchId] });
      toast.success("Batch Coding is being published to this batch's search index.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The coding run could not be made searchable."),
  });
  const promoteAnalysisCoding = useMutation({
    mutationFn: () => coreApi<ReviewBatchRunPromotionRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${selectedAnalysisRun!.id}/promotion`, { method: "POST" }),
    onSuccess: (promotion) => {
      queryClient.setQueryData(["review-batch-analysis-promotion", matterId, batchId, selectedAnalysisRun?.id], promotion);
      setPromotionDialogOpen(false);
      toast.success("Analysis coding promotion was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The analysis coding could not be promoted."),
  });

  const currentIndex = searchResults.data?.hits.findIndex((hit) => hit.document_id === effectiveDocumentId) ?? -1;
  const processed = (progress.data?.completed_count ?? 0) + (progress.data?.skipped_count ?? 0);
  const percent = progress.data?.document_count ? Math.round((processed / progress.data.document_count) * 100) : 0;
  const activeFilterCount = selectedTopicKeys.length
    + Object.values(filters).reduce((total, values) => total + values.length, 0)
    + Object.values(codingFilters).reduce((total, values) => total + values.length, 0);
  const promotionDestinations = analysisDestinationFields(selectedAnalysisRun, analysisPromotion.data);
  const fatalError = matter.error ?? client.error ?? custodians.error ?? definitions.error ?? groups.error ?? batch.error ?? run.error ?? taxonomy.error ?? searchResults.error;
  const exportParams = new URLSearchParams();
  for (const topicKey of selectedTopicKeys) exportParams.append("topic_key", topicKey);
  const exportPath = `/v1/matters/${matterId}/review-batches/${batchId}/search-exports${exportParams.size ? `?${exportParams}` : ""}`;

  if (fatalError) return <main className="grid h-dvh place-items-center p-6"><QueryError message={fatalError.message} /></main>;

  return (
    <main id="main-content" className="fixed inset-0 flex h-dvh min-h-0 w-full max-w-full flex-col overscroll-none bg-background" style={{ overflow: "clip" }}>
      <header className="shrink-0 border-b bg-card shadow-sm">
        <div className="flex min-h-14 flex-wrap items-center gap-3 px-3 py-2">
          <BrandMark className="size-8 shrink-0 rounded-lg" />
          <Button asChild variant="ghost" size="sm"><Link href={matter.data ? matterHref(matter.data.client_id, matterId, "batches") : "/app/clients"}><ArrowLeft />Batches</Link></Button>
          {matter.data ? <MatterSectionMenu clientId={matter.data.client_id} matterId={matterId} matterName={matter.data.name} clientName={client.data?.name} current="batches" /> : null}
          <div className="min-w-0 flex-1 border-l pl-3">
            <h1 className="truncate text-sm font-semibold">{batch.data?.name ?? "Opening batch…"}</h1>
            <p className="truncate text-xs text-muted-foreground">Batch review</p>
          </div>
          {batch.data?.search_status && batch.data.search_status !== "READY" ? <Badge variant="outline">Search {batch.data.search_status.toLowerCase().replace("_", " ")}</Badge> : null}
          {progress.data && progress.data.completed_count + progress.data.skipped_count >= progress.data.document_count ? <Badge variant="accent"><Check />Review complete</Badge> : null}
          <SearchExportDialog matterId={matterId} search={searchRequest} resultCount={searchResults.data?.total ?? 0} createPath={exportPath} scopeLabel={batch.data ? `batch “${batch.data.name}”` : "this batch"} disabled={batch.data?.search_status !== "READY" || searchResults.isPending} />
          <Button type="button" variant="outline" size="sm" disabled={batch.data?.status !== "READY"} onClick={() => openAnalysisDialog()}><Play />Run analysis</Button>
          {batch.data ? <Button asChild variant="outline" size="sm"><Link href={`/review/matters/${matterId}/batches/${batchId}/statistics`} target="_blank" rel="noopener noreferrer"><BarChart3 />Statistics</Link></Button> : null}
          {batch.data ? <AssignBatchCodingGroupsDialog batch={batch.data} groups={groups.data ?? []} onSave={(targetBatchId, codingGroupIds) => assignCodingGroups.mutateAsync({ targetBatchId, codingGroupIds }).then(() => undefined)} /> : null}
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

      <div
        className="grid min-h-0 w-full max-w-full flex-1 overflow-hidden"
        style={{
          gridTemplateColumns: "24rem minmax(0, 1fr) 30rem",
          gridTemplateRows: "minmax(0, 1fr)",
        }}
        aria-label="Batch review layout"
      >
        <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden border-r bg-card" aria-label="Batch navigation">
          <div className="grid shrink-0 grid-cols-2 gap-1 border-b p-1" role="tablist" aria-label="Batch navigation sections">
            <Button type="button" role="tab" aria-selected={leftPanel === "filters"} variant={leftPanel === "filters" ? "outline" : "ghost"} size="sm" onClick={() => setLeftPanel("filters")}><Filter />Filters{activeFilterCount ? <Badge variant="accent">{activeFilterCount}</Badge> : null}</Button>
            <Button type="button" role="tab" aria-selected={leftPanel === "documents"} variant={leftPanel === "documents" ? "outline" : "ghost"} size="sm" onClick={() => setLeftPanel("documents")}><Files />Documents{searchResults.data ? <Badge variant="outline">{searchResults.data.total.toLocaleString()}</Badge> : null}</Button>
          </div>
          {leftPanel === "filters" ? (
            <div ref={filterScrollRef} className="min-h-0 flex-1 divide-y overflow-y-auto overscroll-contain" role="tabpanel" aria-label="Filters">
              {taxonomy.data ? <BatchTopicFacet taxonomy={taxonomy.data} values={topicFacets.data} selected={selectedTopicKeys} onToggle={toggleTopic} /> : null}
              {batch.data?.searchable_coding_status === "READY" && batch.data.searchable_coding_fields.length ? <section ref={searchableCodingFieldsRef} tabIndex={-1} aria-label="Batch Coding fields" className="scroll-mt-2 border-b bg-accent/5 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"><div className="px-3 pb-1 pt-3"><h3 className="flex items-center gap-1.5 text-sm font-semibold"><Sparkles className="size-3.5 text-accent" />Batch Coding</h3><p className="mt-0.5 text-xs text-muted-foreground">Selected run only · open a field to filter this batch</p>{selectedCodingRun?.run_type === "CLASSIFIER" ? <p className="mt-1 text-xs text-muted-foreground">Classifier probabilities are model-specific and should not be compared directly with question-answering confidence.</p> : null}</div>{batch.data.searchable_coding_fields.map((field) => <BatchCodingFacetSection key={field.metadata_definition_id} matterId={matterId} batchId={batchId} searchRequest={searchRequest} field={field} selected={codingFilters[field.key] ?? []} confidence={codingConfidence[field.key] ?? { preset: "any", minimum: null, maximum: null }} projectedMinimum={projectedMinimum} projectedMaximum={projectedMaximum} onToggle={toggleCodingFilter} onConfidenceChange={setCodingConfidenceRange} />)}</section> : null}
              {facetDefinitions.map((definition) => <BatchFacetSection key={definition.id} matterId={matterId} batchId={batchId} searchRequest={searchRequest} definition={definition} selected={filters[definition.key] ?? []} negated={negatedFilterFields.has(definition.key)} custodianNames={new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian.display_name]))} onToggle={toggleFilter} onToggleNegation={() => toggleFilterNegation(definition.key)} />)}
              {!facetDefinitions.length ? <p className="p-3 text-xs text-muted-foreground">No batch filters are configured.</p> : null}
            </div>
          ) : (
            <div className="flex min-h-0 flex-1 flex-col" role="tabpanel" aria-label="Documents">
              <div className="flex h-9 shrink-0 items-center justify-end border-b px-3 text-xs text-muted-foreground">{searchResults.data ? query.trim() && searchMode === "SEMANTIC" && minimumSimilarity === null ? `Top ${searchResults.data.total.toLocaleString()} candidates` : `${searchResults.data.total.toLocaleString()} matches` : ""}</div>
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
            </div>
          )}
        </aside>

        <section className="flex min-h-0 min-w-0 flex-1 bg-background" aria-label="Selected document">
          {(selectedDocument.isPending && selectedDocumentId && !effectiveHit) || (collectionItem.isPending && selectedCollectionItemId) ? <div className="w-full space-y-3 p-5">{Array.from({ length: 10 }, (_, index) => <Skeleton key={index} className="h-5" />)}</div>
            : selectedDocument.error ? <div className="w-full p-5"><QueryError message={selectedDocument.error.message} /></div>
            : collectionItem.error ? <div className="w-full p-5"><QueryError message={collectionItem.error.message} /></div>
              : collectionItem.data ? <DocumentViewerSurface item={collectionItem.data} contentPath={`/v1/matters/${matterId}/documents/${effectiveDocumentId}/content`} highlightedParagraphs={highlightedParagraphs} className="h-full w-full" />
                : <div className="grid h-full w-full place-items-center p-8 text-center"><div><FileText className="mx-auto text-muted-foreground" /><p className="mt-3 font-semibold">Select a document</p></div></div>}
        </section>

        <aside className="flex min-h-0 min-w-0 flex-col overflow-hidden border-l bg-card" aria-label="Batch analysis, coding, history, and chat">
          <div className="flex h-11 shrink-0 items-center gap-1 border-b px-2"><Button type="button" size="sm" variant={sidePanel === "analysis" ? "outline" : "ghost"} onClick={() => setSidePanel("analysis")}><Sparkles />Analysis</Button><Button type="button" size="sm" variant={sidePanel === "coding" ? "outline" : "ghost"} onClick={() => setSidePanel("coding")}>Coding</Button><Button type="button" size="sm" variant={sidePanel === "history" ? "outline" : "ghost"} onClick={() => setSidePanel("history")}><Clock3 />History</Button><Button type="button" size="sm" variant={sidePanel === "chat" ? "outline" : "ghost"} onClick={() => setSidePanel("chat")}><MessageSquareText />Chat</Button><span className="ml-auto">{sidePanel !== "chat" && coding.data ? <Badge variant="outline">{statusLabel(coding.data.review_status)}</Badge> : null}</span></div>
          {sidePanel === "chat" ? <BatchChatPanel matterId={matterId} batchId={batchId} searchReady={batch.data?.search_status === "READY"} onOpenDocument={openDocumentReference} />
            : sidePanel === "analysis" ? selectedClassificationRun && selectedAnalysisJob
            ? <BatchClassificationAnalysisPanel clientId={matter.data!.client_id} matterId={matterId} batchId={batchId} jobs={analysisJobs} matterDefinitionAssessment={matterDefinitionAssessment} job={selectedAnalysisJob} run={selectedClassificationRun} projectionRun={selectedClassifierProjection} searchableRunId={batch.data?.searchable_coding_run_id} searchableStatus={batch.data?.searchable_coding_status} onProjectionChanged={(projection) => { queryClient.setQueryData<ReviewBatchRunRead[]>(["review-batch-runs", matterId, batchId], (current) => [projection, ...(current ?? []).filter((item) => item.id !== projection.id)]); void queryClient.invalidateQueries({ queryKey: ["review-batch", matterId, batchId] }); }} onViewFilters={viewSearchableCodingFields} onSelectRun={setSelectedAnalysisRunId} onRunNew={() => openAnalysisDialog()} onRunAgain={(taskId) => openAnalysisDialog(taskId)} canceling={cancelAnalysis.isPending} onCancel={(runId) => cancelAnalysis.mutate({ jobType: "CLASSIFICATION_ANALYSIS_TASK", runId })} />
            : selectedAnalysisRun
            ? <BatchDecisionAnalysisPanel jobs={analysisJobs} matterDefinitionAssessment={matterDefinitionAssessment} selectedRun={selectedAnalysisRun} onSelectRun={setSelectedAnalysisRunId} progress={analysisRunProgress.data} documentStatus={analysisDocumentStatus.data?.review_status} result={decisionResult.data} resultLoading={decisionResult.isPending && decisionResult.isFetching} resultError={decisionResult.error?.message} searchableRunId={batch.data?.searchable_coding_run_id} searchableStatus={batch.data?.searchable_coding_status} searchableError={batch.data?.searchable_coding_error_message} publishing={makeAnalysisSearchable.isPending} onMakeSearchable={(runId) => makeAnalysisSearchable.mutate(runId)} onViewSearchableFields={viewSearchableCodingFields} promotion={analysisPromotion.data} promotionLoading={analysisPromotion.isPending || promoteAnalysisCoding.isPending} onPromote={() => setPromotionDialogOpen(true)} onRunNew={() => openAnalysisDialog()} onRunAgain={(taskId) => openAnalysisDialog(taskId)} canceling={cancelAnalysis.isPending} onCancel={(runId) => cancelAnalysis.mutate({ jobType: "ANALYSIS_TASK_BATCH_RUN", runId })} />
            : taxonomy.data && matterDefinitionAssessment ? <MatterDefinitionAnalysisPanel jobs={analysisJobs} assessment={matterDefinitionAssessment} selectedRunId={selectedAnalysisViewId} onSelectRun={setSelectedAnalysisRunId} analysis={analysis.data} loading={analysis.isPending} error={analysis.error?.message} onRunAnalysis={() => openAnalysisDialog()} />
            : <div className="grid flex-1 place-items-center p-5 text-center"><div><Sparkles className="mx-auto text-muted-foreground" /><p className="mt-3 font-semibold">No batch analysis yet</p><p className="mt-1 text-sm text-muted-foreground">Run a published Analysis Task against this frozen batch.</p><Button className="mt-4" size="sm" variant="outline" onClick={() => openAnalysisDialog()}><Play />Run analysis</Button></div></div>
            : sidePanel === "history" ? <BatchCodingHistoryPanel entries={codingHistory.data} loading={codingHistory.isPending} error={codingHistory.error?.message} hasDocument={Boolean(effectiveDocumentId)} />
            : coding.isPending && effectiveDocumentId ? <div className="space-y-3 p-4">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-16" />)}</div>
            : coding.error ? <div className="p-4"><QueryError message={coding.error.message} /></div>
              : coding.data && batch.data && run.data ? <BatchCodingForm
                key={`${effectiveDocumentId}:${coding.dataUpdatedAt}`}
                batch={batch.data}
                coding={coding.data}
                disabled={false}
                saving={save.isPending || skip.isPending}
                onSave={(payload) => save.mutateAsync(payload).then(() => undefined)}
                onSkip={() => skip.mutateAsync().then(() => undefined)}
              />
                : <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to begin coding.</div>}
        </aside>
      </div>
      <Dialog open={analysisDialogOpen} onOpenChange={setAnalysisDialogOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>Run analysis against this batch</DialogTitle><DialogDescription>Select a published question-answering or classification Analysis Task. Its exact version, model configuration, and every document in this batch will be frozen into an isolated run.</DialogDescription></DialogHeader>
          {analysisTasks.isPending ? <div className="space-y-2 py-3"><Skeleton className="h-10" /><Skeleton className="h-16" /></div>
            : analysisTasks.error ? <QueryError message={analysisTasks.error.message} />
            : publishedAnalysisTasks.length ? <div className="space-y-3 py-2"><label className="space-y-1.5 text-sm font-medium" htmlFor="batch-analysis-task"><span>Published task</span><Select value={effectiveAnalysisTaskId} onValueChange={setAnalysisTaskId}><SelectTrigger id="batch-analysis-task"><SelectValue placeholder="Select an analysis task" /></SelectTrigger><SelectContent>{publishedAnalysisTasks.map((task) => { const typeLabel = task.task_type === "CLASSIFICATION" ? "Classifier" : publishedTaskProvider(task).label; return <SelectItem key={task.id} value={task.id}>{task.name} · published v{task.published_version} · {typeLabel}</SelectItem>; })}</SelectContent></Select></label>{effectiveAnalysisProvider ? <div className="rounded-lg border bg-muted/30 p-3 text-sm"><p><span className="text-muted-foreground">Decision provider:</span> <strong>{effectiveAnalysisProvider.label}</strong></p><p className="mt-1"><span className="text-muted-foreground">Model:</span> <strong>{effectiveAnalysisProvider.model ?? "Provider default"}</strong></p></div> : null}{effectiveClassificationVersion ? <div className="rounded-lg border bg-muted/30 p-3 text-sm"><p><span className="text-muted-foreground">Analysis type:</span> <strong>Classification</strong></p><p className="mt-1"><span className="text-muted-foreground">Pinned model version:</span> <strong className="break-all font-mono text-xs">{effectiveClassificationVersion.classification_model_version_id ?? "Unavailable"}</strong></p><p className="mt-1"><span className="text-muted-foreground">Document embeddings:</span> <strong className="break-all font-mono text-xs">{effectiveClassificationEmbeddingJobId ?? "Unavailable"}</strong></p></div> : null}{effectiveAnalysisTask?.task_type === "CLASSIFICATION" && !effectiveClassificationEmbeddingJobId ? <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-5">This published classifier does not identify the compatible embedding job required for scoring. Republish the task after activating a model version with a valid feature snapshot.</p> : null}{hasUnpublishedAnalysisDraft ? <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-5"><p className="font-semibold">A newer draft is not published</p><p className="mt-1 text-muted-foreground">This run will use published v{effectiveAnalysisTask?.published_version}, not draft v{effectiveAnalysisTask?.current_version}. Publish the draft from Analysis tasks first if it contains the corrected guidance.</p>{matter.data ? <Button asChild className="mt-2" size="sm" variant="outline"><Link href={matterHref(matter.data.client_id, matterId, "analysis")}>Open Analysis tasks</Link></Button> : null}</div> : null}<p className="rounded-lg bg-muted p-3 text-xs leading-5 text-muted-foreground">Each run creates an isolated result set. It does not update final matter tags.</p></div>
              : <div className="rounded-lg border border-dashed p-4 text-sm"><p className="font-semibold">No published Analysis Tasks</p><p className="mt-1 text-muted-foreground">Generate, validate, and publish a task version before launching a batch run.</p>{matter.data ? <Button asChild className="mt-3" size="sm" variant="outline"><Link href={matterHref(matter.data.client_id, matterId, "analysis")}>Open Analysis tasks</Link></Button> : null}</div>}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setAnalysisDialogOpen(false)} disabled={launchAnalysis.isPending}>Cancel</Button><Button type="button" onClick={() => launchAnalysis.mutate()} disabled={!effectiveAnalysisTaskId || effectiveAnalysisTask?.task_type === "CLASSIFICATION" && !effectiveClassificationEmbeddingJobId || launchAnalysis.isPending}>{launchAnalysis.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}{launchAnalysis.isPending ? "Queueing…" : `Run on ${batch.data?.document_count.toLocaleString() ?? 0} documents`}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={promotionDialogOpen} onOpenChange={setPromotionDialogOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>Promote analysis coding to the matter?</DialogTitle><DialogDescription>This publishes the selected run&apos;s policy-approved values to the Analysis Task&apos;s configured destination fields.</DialogDescription></DialogHeader>
          <div className="space-y-3 text-sm">{promotionDestinations.length ? <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Destination fields</p><p className="mt-1 font-medium">{promotionDestinations.map((field) => field.display_name).join(", ")}</p></div> : null}<p>For each document with complete classification coverage, the destination coding is replaced with this run&apos;s values. A document with no matched values is explicitly cleared.</p><p className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs leading-5">This changes live matter coding and triggers search reindexing. Filters use each destination field&apos;s display name. The generated metadata events retain the batch, run, task version, scores, and promoting user for audit.</p></div>
          <DialogFooter><Button type="button" variant="outline" onClick={() => setPromotionDialogOpen(false)} disabled={promoteAnalysisCoding.isPending}>Cancel</Button><Button type="button" onClick={() => promoteAnalysisCoding.mutate()} disabled={promoteAnalysisCoding.isPending}>{promoteAnalysisCoding.isPending ? <LoaderCircle className="animate-spin" /> : <Upload />}{promoteAnalysisCoding.isPending ? "Queueing…" : "Promote to matter coding"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <div className="flex h-11 shrink-0 items-center justify-center gap-2 border-t bg-card px-3">
        <Button variant="ghost" size="sm" disabled={currentIndex <= 0} onClick={() => searchResults.data?.hits[currentIndex - 1] && selectDocument(searchResults.data.hits[currentIndex - 1].document_id)}><ChevronLeft />Previous document</Button>
        <Button variant="ghost" size="sm" disabled={currentIndex < 0 || currentIndex >= (searchResults.data?.hits.length ?? 0) - 1} onClick={() => searchResults.data?.hits[currentIndex + 1] && selectDocument(searchResults.data.hits[currentIndex + 1].document_id)}>Next document<ChevronRight /></Button>
      </div>
    </main>
  );
}

function formatHistoryDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function historyScore(entry: ReviewBatchCodingHistoryEntryRead) {
  if (entry.score === null || entry.score === undefined) return null;
  const kind = entry.score_kind?.toLowerCase().replaceAll("_", " ");
  return `${Math.round(entry.score * 1000) / 10}%${kind ? ` ${kind}` : " score"}`;
}

function reviewerHistoryOrigin(entry: ReviewBatchCodingHistoryEntryRead) {
  if (!entry.reviewer_origin) return null;
  if (entry.reviewer_origin === "REVIEW_UI") return "Review workspace";
  if (entry.reviewer_origin === "SYSTEM_MIGRATION") return "Migrated baseline";
  return "CSV import";
}

function BatchCodingHistoryPanel({ entries, loading, error, hasDocument }: {
  entries?: ReviewBatchCodingHistoryEntryRead[];
  loading: boolean;
  error?: string;
  hasDocument: boolean;
}) {
  if (!hasDocument) return <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to view its batch coding history.</div>;
  if (loading) return <div className="space-y-3 p-4">{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-28" />)}</div>;
  if (error) return <div className="p-4"><QueryError message={error} /></div>;
  if (!entries?.length) return <div className="grid flex-1 place-items-center p-5 text-center"><div><Clock3 className="mx-auto text-muted-foreground" /><p className="mt-3 font-semibold">No batch coding history</p><p className="mt-1 text-sm text-muted-foreground">Coding from human, Jev, agent, and workflow runs will appear here.</p></div></div>;
  return <div className="min-h-0 flex-1 overflow-y-auto p-4"><ol className="space-y-3">{entries.map((entry) => {
    const rawCode = displayValue(entry.value);
    const showRawCode = entry.value_label !== rawCode;
    const score = historyScore(entry);
    const reviewerOrigin = reviewerHistoryOrigin(entry);
    return <li key={`${entry.reviewer_coding_id ?? entry.review_batch_run_id}:${entry.reviewer_revision ?? "run"}:${entry.event_kind ?? "value"}:${entry.metadata_definition_id}:${entry.recorded_at}:${rawCode}`} className="rounded-lg border bg-background p-3 shadow-xs">
      <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{entry.field_display_name}</p><p className="mt-1 break-words text-sm font-semibold">{entry.value_label}</p>{showRawCode ? <p className="mt-0.5 break-all font-mono text-[11px] text-muted-foreground">{rawCode}</p> : null}</div><Badge variant={entry.source_kind === "HUMAN" ? "accent" : "outline"}>{entry.source_kind === "JEV" ? "Jev" : entry.source_kind.toLowerCase()}</Badge></div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs"><dt className="text-muted-foreground">Date</dt><dd>{formatHistoryDate(entry.recorded_at)}</dd><dt className="text-muted-foreground">Source</dt><dd className="min-w-0 break-words">{entry.source_label}{entry.source_detail ? <span className="text-muted-foreground"> · {entry.source_detail}</span> : null}</dd>{entry.reviewer_revision ? <><dt className="text-muted-foreground">Revision</dt><dd>#{entry.reviewer_revision}{reviewerOrigin ? <span className="text-muted-foreground"> · {reviewerOrigin}</span> : null}</dd></> : null}<dt className="text-muted-foreground">Score</dt><dd>{score ?? "—"}</dd></dl>
    </li>;
  })}</ol></div>;
}

function BatchTopicFacet({ taxonomy, values, selected, onToggle }: { taxonomy: BatchTopicTaxonomyRead; values?: MatterFacetValuesResponse; selected: string[]; onToggle: (topicKey: string) => void }) {
  const counts = new Map((values?.values ?? []).map((item) => [String(item.value), item.count]));
  return <section className="border-b bg-primary/5 p-3"><div className="mb-2 flex items-center justify-between gap-2"><h3 className="flex items-center gap-1.5 text-sm font-semibold"><Sparkles className="size-3.5 text-primary" />Batch topics</h3><Badge variant="outline">v{taxonomy.version}</Badge></div><div className="max-h-[min(50dvh,32rem)] space-y-1 overflow-y-auto overscroll-contain pr-1">{taxonomy.topics.map((topic) => <label key={topic.id} className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" className="size-4 accent-primary" checked={selected.includes(topic.topic_key)} onChange={() => onToggle(topic.topic_key)} /><span className="min-w-0 flex-1 truncate" title={topic.description ?? topic.label}>{topic.label}</span><span className="font-mono text-xs text-muted-foreground">{(counts.get(topic.topic_key) ?? topic.assignment_count).toLocaleString()}</span></label>)}</div></section>;
}

type MatterDefinitionAssessmentOption = {
  runId: string;
  version: number;
  createdAt: string;
};

function BatchClassificationAnalysisPanel({ clientId, matterId, batchId, jobs, matterDefinitionAssessment, job, run, projectionRun, searchableRunId, searchableStatus, onProjectionChanged, onViewFilters, onSelectRun, onRunNew, onRunAgain, canceling, onCancel }: {
  clientId: string;
  matterId: string;
  batchId: string;
  jobs: ReviewBatchAnalysisJobRead[];
  matterDefinitionAssessment?: MatterDefinitionAssessmentOption;
  job: ReviewBatchAnalysisJobRead;
  run: MatterAnalysisTaskRunRead;
  projectionRun?: ReviewBatchRunRead;
  searchableRunId?: string | null;
  searchableStatus?: string | null;
  onProjectionChanged: (run: ReviewBatchRunRead) => void;
  onViewFilters: () => void;
  onSelectRun: (runId: string) => void;
  onRunNew: () => void;
  onRunAgain: (taskId: string) => void;
  canceling: boolean;
  onCancel: (runId: string) => void;
}) {
  const runActive = !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status);
  const canRunAgain = !runActive;
  return <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
    <div className="shrink-0 space-y-3 border-b p-3">
      <div className="flex flex-wrap items-center justify-end gap-2"><StatusBadge status={run.status} />{runActive ? <Button type="button" size="sm" variant="outline" disabled={canceling} onClick={() => onCancel(run.id)}>{canceling ? <LoaderCircle className="animate-spin" /> : <Ban />}Cancel</Button> : null}<Button type="button" size="sm" variant="outline" onClick={onRunNew}><Play />Run new task</Button>{canRunAgain ? <Button type="button" size="sm" variant="outline" onClick={() => onRunAgain(run.matter_analysis_task_id)}><RotateCcw />Run again</Button> : null}</div>
      <AnalysisRunSelect jobs={jobs} matterDefinitionAssessment={matterDefinitionAssessment} selectedRunId={job.id} onSelectRun={onSelectRun} />
    </div>
    <div className="min-h-0 flex-1 overflow-y-auto p-3"><ClassificationAnalysisJobDetail clientId={clientId} matterId={matterId} run={{ ...run, task_name: job.task_name }} batchProjection={{ batchId, projectionRun, searchableRunId, searchableStatus, onProjectionChanged, onViewFilters }} /></div>
  </div>;
}

export function BatchDecisionAnalysisPanel({ jobs, matterDefinitionAssessment, selectedRun, onSelectRun, progress, documentStatus, result, resultLoading, resultError, searchableRunId, searchableStatus, searchableError, publishing, onMakeSearchable, onViewSearchableFields, promotion, promotionLoading, onPromote, onRunNew, onRunAgain, canceling = false, onCancel }: {
  jobs: ReviewBatchAnalysisJobRead[];
  matterDefinitionAssessment?: MatterDefinitionAssessmentOption;
  selectedRun: ReviewBatchRunRead;
  onSelectRun: (runId: string) => void;
  progress?: ReviewBatchRunProgressRead;
  documentStatus?: ReviewBatchDocumentRead["review_status"];
  result?: ReviewDecisionResultRead;
  resultLoading: boolean;
  resultError?: string;
  searchableRunId?: string | null;
  searchableStatus?: string | null;
  searchableError?: string | null;
  publishing: boolean;
  onMakeSearchable: (runId: string) => void;
  onViewSearchableFields: () => void;
  promotion?: ReviewBatchRunPromotionRead | null;
  promotionLoading: boolean;
  onPromote: () => void;
  onRunNew: () => void;
  onRunAgain: (taskId: string) => void;
  canceling?: boolean;
  onCancel?: (runId: string) => void;
}) {
  const taskId = typeof selectedRun.configuration_snapshot.task_id === "string" ? selectedRun.configuration_snapshot.task_id : null;
  const successful = progress?.completed_count ?? 0;
  const percent = progress?.document_count ? Math.round((successful / progress.document_count) * 100) : 0;
  const canRunAgain = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(selectedRun.status);
  const runActive = ["QUEUED", "RUNNING"].includes(selectedRun.status);
  const searchReady = searchableRunId === selectedRun.id && searchableStatus === "READY";
  const destinationNames = analysisDestinationFields(selectedRun, promotion).map((field) => field.display_name);
  return <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
    <div className="shrink-0 space-y-3 border-b p-3">
      <div className="flex flex-wrap items-center justify-end gap-2"><StatusBadge status={selectedRun.status} />{runActive && onCancel ? <Button type="button" size="sm" variant="outline" disabled={canceling} onClick={() => onCancel(selectedRun.id)}>{canceling ? <LoaderCircle className="animate-spin" /> : <Ban />}Cancel</Button> : null}<Button type="button" size="sm" variant="outline" onClick={onRunNew}><Play />Run new task</Button>{canRunAgain ? <Button type="button" size="sm" variant="outline" disabled={!taskId} onClick={() => taskId && onRunAgain(taskId)}><RotateCcw />Run again</Button> : null}</div>
      <AnalysisRunSelect jobs={jobs} matterDefinitionAssessment={matterDefinitionAssessment} selectedRunId={selectedRun.id} onSelectRun={onSelectRun} />
      {progress ? <div><div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-primary transition-[width]" style={{ width: `${percent}%` }} /></div><p className="mt-1 text-xs tabular-nums text-muted-foreground">{successful.toLocaleString()} of {progress.document_count.toLocaleString()} successful · {(progress.failed_count ?? 0).toLocaleString()} failed</p></div> : null}
      {["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(selectedRun.status) ? <div className="rounded-lg border bg-background p-3"><div className="flex items-center justify-between gap-3"><div><p className="text-sm font-medium">Batch Coding</p><p className="mt-0.5 text-xs text-muted-foreground">Expose the policy-approved codes from this run as batch-only filters and facets.</p></div><Button type="button" size="sm" variant={searchReady ? "outline" : "default"} disabled={publishing || searchableRunId === selectedRun.id && ["QUEUED", "SYNCING"].includes(searchableStatus ?? "")} onClick={() => searchReady ? onViewSearchableFields() : onMakeSearchable(selectedRun.id)}>{publishing || searchableRunId === selectedRun.id && ["QUEUED", "SYNCING"].includes(searchableStatus ?? "") ? <LoaderCircle className="animate-spin" /> : <Search />}{searchReady ? "View filters" : searchableRunId === selectedRun.id && searchableStatus === "FAILED" ? "Retry projection" : searchableRunId === selectedRun.id ? "Publishing…" : "Make searchable"}</Button></div>{searchReady ? <p className="mt-2 text-xs text-muted-foreground">The fields are available under Batch Coding in the batch search filters.</p> : null}{searchableRunId === selectedRun.id && searchableStatus === "FAILED" ? <p className="mt-2 text-xs text-destructive">{searchableError ?? "Projection failed. Retry the Batch Coding projection."}</p> : null}</div> : null}
      {["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(selectedRun.status) ? <div className="rounded-lg border bg-background p-3"><div className="flex items-center justify-between gap-3"><div><p className="text-sm font-medium">Matter Coding</p><p className="mt-0.5 text-xs text-muted-foreground">Replace {destinationNames.length ? destinationNames.join(", ") : "the configured destination fields"} with this run&apos;s decisions.</p></div>{promotion?.status === "COMPLETED" ? <Badge variant="accent"><Check />Promoted</Badge> : <Button type="button" size="sm" disabled={promotionLoading || ["QUEUED", "RUNNING"].includes(promotion?.status ?? "")} onClick={onPromote}>{promotionLoading || ["QUEUED", "RUNNING"].includes(promotion?.status ?? "") ? <LoaderCircle className="animate-spin" /> : <Upload />}{promotion?.status === "FAILED" ? "Retry promotion" : promotion ? "Promoting…" : "Promote coding"}</Button>}</div>{promotion ? <p className={cn("mt-2 text-xs tabular-nums text-muted-foreground", promotion.status === "FAILED" && "text-destructive")}>{promotion.status === "FAILED" ? promotion.error_message ?? "Promotion failed." : `${promotion.processed_document_count.toLocaleString()} of ${promotion.eligible_document_count.toLocaleString()} eligible documents promoted · ${promotion.skipped_document_count.toLocaleString()} skipped`}</p> : <p className="mt-2 text-xs text-muted-foreground">This writes auditable AGENT coding events and reindexes affected documents. Matter filters show the field display name.</p>}</div> : null}
    </div>
    {!documentStatus ? <div className="grid flex-1 place-items-center p-5 text-sm text-muted-foreground">Select a document to inspect its decision result.</div>
      : documentStatus === "FAILED" ? <div className="grid flex-1 place-items-center p-5 text-center"><div><StatusBadge status="FAILED" /><p className="mt-3 text-sm text-muted-foreground">The decision provider could not complete this document. The run remains isolated and the failure is included in batch progress.</p></div></div>
        : selectedRun.status === "CANCELED" && documentStatus === "SKIPPED" ? <div className="grid flex-1 place-items-center p-5 text-center"><div><StatusBadge status="CANCELED" /><p className="mt-3 text-sm font-semibold">Document analysis canceled</p><p className="mt-1 text-xs text-muted-foreground">This document was not evaluated before the run was canceled. Start a new run to analyze it.</p></div></div>
          : documentStatus === "SKIPPED" ? <div className="grid flex-1 place-items-center p-5 text-center"><div><StatusBadge status="SKIPPED" /><p className="mt-3 text-sm font-semibold">Document analysis skipped</p><p className="mt-1 text-xs text-muted-foreground">This run did not produce a result for the document.</p></div></div>
            : documentStatus !== "COMPLETED" && runActive ? <div className="grid flex-1 place-items-center p-5 text-center"><div><LoaderCircle className="mx-auto size-5 animate-spin text-primary" /><p className="mt-3 text-sm font-semibold">Document analysis {documentStatus.toLowerCase().replaceAll("_", " ")}</p><p className="mt-1 text-xs text-muted-foreground">The result will appear here when evaluation completes.</p></div></div>
              : documentStatus !== "COMPLETED" ? <div className="grid flex-1 place-items-center p-5 text-center"><div><StatusBadge status={documentStatus} /><p className="mt-3 text-sm font-semibold">No analysis result</p><p className="mt-1 text-xs text-muted-foreground">This run ended before the document was evaluated.</p></div></div>
          : resultLoading ? <div className="space-y-3 p-4">{Array.from({ length: 5 }, (_, index) => <Skeleton key={index} className="h-20" />)}</div>
            : resultError ? <div className="p-4"><QueryError message={resultError} /></div>
              : result ? <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
                <div className="flex flex-wrap items-center gap-2"><StatusBadge status={result.status} /><Badge variant="outline">{result.provider ?? "provider"} · {result.model ?? "model"}</Badge>{result.coverage.complete === false ? <Badge variant="outline">partial coverage</Badge> : null}</div>
                <DecisionResultSection title="Answers" value={result.answers} />
                <DecisionResultSection title="Recommendations" value={result.recommendations} />
                <DecisionResultSection title="Routes" value={result.routes} />
                <DecisionResultSection title="Coverage" value={result.coverage} />
                <p className="text-xs tabular-nums text-muted-foreground">{result.request_count.toLocaleString()} requests · {result.input_tokens.toLocaleString()} input tokens · {result.output_tokens.toLocaleString()} output tokens · {result.latency_ms.toLocaleString()} ms</p>
              </div> : <div className="grid flex-1 place-items-center p-5 text-sm text-muted-foreground">The completed document has no stored Decision Result.</div>}
  </div>;
}

function MatterDefinitionAnalysisPanel({ jobs, assessment, selectedRunId, onSelectRun, analysis, loading, error, onRunAnalysis }: {
  jobs: ReviewBatchAnalysisJobRead[];
  assessment: MatterDefinitionAssessmentOption;
  selectedRunId: string;
  onSelectRun: (runId: string) => void;
  analysis?: ReviewBatchDocumentAnalysisRead;
  loading: boolean;
  error?: string;
  onRunAnalysis: () => void;
}) {
  return <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
    <div className="shrink-0 space-y-3 border-b bg-primary/5 p-3">
      <div className="flex items-center justify-between gap-3"><div className="min-w-0"><p className="text-sm font-semibold">Matter Definition assessment</p><p className="mt-0.5 text-xs text-muted-foreground">This is the original assessment output, not a question-answering Analysis Task run.</p></div><Button type="button" size="sm" className="shrink-0" onClick={onRunAnalysis}><Play />Run analysis</Button></div>
      <AnalysisRunSelect jobs={jobs} matterDefinitionAssessment={assessment} selectedRunId={selectedRunId} onSelectRun={onSelectRun} />
    </div>
    <DocumentAnalysisPanel analysis={analysis} loading={loading} error={error} />
  </div>;
}

export function AnalysisRunSelect({ jobs, matterDefinitionAssessment, selectedRunId, onSelectRun }: {
  jobs: ReviewBatchAnalysisJobRead[];
  matterDefinitionAssessment?: MatterDefinitionAssessmentOption;
  selectedRunId: string;
  onSelectRun: (runId: string) => void;
}) {
  const optionCount = jobs.length + (matterDefinitionAssessment ? 1 : 0);
  return <label className="block space-y-1.5 text-xs font-medium" htmlFor="batch-analysis-run">
    <span>Analysis run</span>
    <Select value={selectedRunId} onValueChange={onSelectRun} disabled={optionCount < 2}>
      <SelectTrigger id="batch-analysis-run" aria-label="Analysis run"><SelectValue /></SelectTrigger>
      <SelectContent>
        {jobs.map((job) => <SelectItem key={job.id} value={job.id}>{job.task_name} · published v{job.task_version ?? "—"} · {formatHistoryDate(job.created_at)} · {job.status.toLowerCase().replaceAll("_", " ")}</SelectItem>)}
        {matterDefinitionAssessment ? <SelectItem value={matterDefinitionAssessment.runId}>Matter Definition assessment · v{matterDefinitionAssessment.version} · {formatHistoryDate(matterDefinitionAssessment.createdAt)}</SelectItem> : null}
      </SelectContent>
    </Select>
  </label>;
}

function DecisionResultSection({ title, value }: { title: string; value: unknown }) {
  return <section><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{title}</h3><pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-background p-3 text-xs leading-5 [overflow-wrap:anywhere]">{JSON.stringify(value, null, 2)}</pre></section>;
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
  return <div className="min-h-0 min-w-0 flex-1 overflow-y-auto p-4"><div className="w-full min-w-0"><div className="mb-4 flex items-center justify-between gap-2"><StatusBadge status={String(result.determination ?? "UNCLEAR")} />{typeof result.confidence === "number" ? <Badge variant="outline">{Math.round(result.confidence * 100)}% confidence</Badge> : null}</div><AnalysisSection title="Document Summary" items={result.summary} onCitation={setCitation} /><AnalysisSection title="Responsiveness Summary" items={result.responsiveness_summary} onCitation={setCitation} /><AnalysisSection title="Clarification Requests" items={result.clarification_requests} onCitation={setCitation} />{selectedParagraph ? <div className="sticky bottom-0 mt-4 rounded-lg border border-primary/30 bg-background p-3 shadow-lg"><div className="flex items-center justify-between"><p className="text-xs font-bold text-primary">{String(selectedParagraph.paragraph_id)}</p><button type="button" className="text-xs text-muted-foreground" onClick={() => setCitation(null)}>Close</button></div><p className="mt-1 whitespace-pre-wrap break-words text-xs leading-5 [overflow-wrap:anywhere]">{String(selectedParagraph.text ?? "")}</p></div> : null}</div></div>;
}

function AnalysisSection({ title, items, onCitation }: { title: string; items: unknown; onCitation: (id: string) => void }) {
  const values = Array.isArray(items) ? items.map(objectValue) : [];
  return <section className="mb-5 min-w-0"><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{title}</h3>{values.length ? <div className="space-y-3">{values.map((item, index) => { const citations = Array.isArray(item.citation_ids) ? item.citation_ids.map(String) : []; return <div key={index} className="min-w-0 text-sm leading-6"><p className="break-words [overflow-wrap:anywhere]">{String(item.text ?? item.question ?? item.reasoning ?? "")}</p>{item.rationale ? <p className="break-words text-xs text-muted-foreground [overflow-wrap:anywhere]">{String(item.rationale)}</p> : null}<div className="mt-1 flex flex-wrap gap-1">{citations.map((id) => <button key={id} type="button" className="rounded bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary hover:bg-primary/20" onClick={() => onCitation(id)}>{id}</button>)}</div></div>; })}</div> : <p className="text-sm text-muted-foreground">None.</p>}</section>;
}

function useDebouncedValue(value: string, delay: number) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [delay, value]);
  return debounced;
}

function BatchCodingFacetSection({ matterId, batchId, searchRequest, field, selected, confidence, projectedMinimum, projectedMaximum, onToggle, onConfidenceChange }: {
  matterId: string;
  batchId: string;
  searchRequest: ReviewBatchSearchRequest;
  field: ReviewBatchSearchCodingFieldRead;
  selected: string[];
  confidence: CodingConfidenceRange;
  projectedMinimum: number | null;
  projectedMaximum: number | null;
  onToggle: (key: string, value: string) => void;
  onConfidenceChange: (key: string, range: CodingConfidenceRange) => void;
}) {
  const [open, setOpen] = useState(false);
  const values = useQuery({
    queryKey: ["review-batch-coding-facet-values", matterId, batchId, field.key, searchRequest],
    queryFn: () => coreApi<MatterFacetValuesResponse>(`/v1/matters/${matterId}/review-batches/${batchId}/coding-facets/${field.key}/values`, {
      method: "POST",
      body: JSON.stringify(searchRequest),
    }),
    enabled: open,
    placeholderData: (previous) => previous,
  });
  const options = useMemo(() => {
    const available = new Map((values.data?.values ?? []).map((option) => [facetToken(option.value), option]));
    for (const token of selected) if (!available.has(token)) available.set(token, { value: token, count: 0 });
    return [...available.values()];
  }, [selected, values.data?.values]);
  const labelFor = (token: string) => field.type === "BOOLEAN" ? token === "true" ? "Yes" : "No" : token;
  const materializedRange = projectedMinimum !== null
    ? `${Math.round(projectedMinimum * 100)}% inclusive to ${projectedMaximum === null ? "100%" : `${Math.round(projectedMaximum * 100)}% exclusive`}`
    : null;
  const presetRanges: Record<Exclude<CodingConfidencePreset, "custom">, Pick<CodingConfidenceRange, "minimum" | "maximum">> = {
    any: { minimum: null, maximum: null },
    "90-plus": { minimum: 0.9, maximum: null },
    "80-90": { minimum: 0.8, maximum: 0.9 },
    "70-80": { minimum: 0.7, maximum: 0.8 },
  };
  const rangeAvailable = (minimum: number | null, maximum: number | null) => {
    if (projectedMinimum !== null && minimum !== null && minimum < projectedMinimum) return false;
    if (projectedMinimum !== null && minimum === null) return false;
    if (projectedMaximum !== null && (maximum === null || maximum > projectedMaximum)) return false;
    return !(projectedMaximum !== null && minimum !== null && minimum >= projectedMaximum);
  };
  const choosePreset = (preset: CodingConfidencePreset) => {
    if (preset === "custom") {
      onConfidenceChange(field.key, {
        preset,
        minimum: confidence.minimum ?? projectedMinimum,
        maximum: confidence.maximum ?? projectedMaximum,
      });
      return;
    }
    onConfidenceChange(field.key, { preset, ...presetRanges[preset] });
  };
  const updateCustomBound = (bound: "minimum" | "maximum", raw: string) => {
    const parsed = raw === ""
      ? bound === "minimum" ? projectedMinimum : projectedMaximum
      : Number(raw) / 100;
    if (parsed !== null && (!Number.isFinite(parsed) || parsed < 0 || parsed > 1)) return;
    if (bound === "minimum" && parsed !== null && projectedMinimum !== null && parsed < projectedMinimum) return;
    if (bound === "maximum" && projectedMaximum !== null && (parsed === null || parsed > projectedMaximum)) return;
    onConfidenceChange(field.key, { ...confidence, preset: "custom", [bound]: parsed });
  };
  return <div>
    <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="flex min-h-10 w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold hover:bg-muted/60">
      {open ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}<span className="min-w-0 flex-1 truncate">{field.display_name}</span>{selected.length ? <Badge variant="accent">{selected.length}</Badge> : null}
    </button>
    {open ? <div className="max-h-[min(50dvh,32rem)] space-y-2 overflow-y-auto overscroll-contain px-3 pb-3">
      <div className="space-y-1.5 rounded-md border bg-background/60 p-2">
        <Label className="text-xs">Confidence</Label>
        <Select value={confidence.preset} onValueChange={(value) => choosePreset(value as CodingConfidencePreset)}>
          <SelectTrigger aria-label={`Confidence for ${field.display_name}`} className="h-8 text-xs"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="any">Any projected confidence</SelectItem>
            <SelectItem value="90-plus" disabled={!rangeAvailable(0.9, null)}>90% and above</SelectItem>
            <SelectItem value="80-90" disabled={!rangeAvailable(0.8, 0.9)}>80% to under 90%</SelectItem>
            <SelectItem value="70-80" disabled={!rangeAvailable(0.7, 0.8)}>70% to under 80%</SelectItem>
            <SelectItem value="custom">Custom range</SelectItem>
          </SelectContent>
        </Select>
        {confidence.preset === "custom" ? <div className="grid grid-cols-2 gap-2">
          <label className="text-[11px] text-muted-foreground">Minimum %<Input type="number" min={projectedMinimum === null ? 0 : projectedMinimum * 100} max={100} step="1" value={confidence.minimum === null ? "" : confidence.minimum * 100} onChange={(event) => updateCustomBound("minimum", event.target.value)} className="mt-1 h-8 text-xs" /></label>
          <label className="text-[11px] text-muted-foreground">Maximum %<Input type="number" min={0} max={projectedMaximum === null ? 100 : projectedMaximum * 100} step="1" value={confidence.maximum === null ? "" : confidence.maximum * 100} onChange={(event) => updateCustomBound("maximum", event.target.value)} className="mt-1 h-8 text-xs" /></label>
        </div> : null}
        <p className="text-[11px] leading-4 text-muted-foreground">Minimum is inclusive; maximum is exclusive. Values without a confidence score do not match an active range.{materializedRange ? ` This classifier run contains ${materializedRange}.` : ""}</p>
        {!selected.length && (confidence.minimum !== null || confidence.maximum !== null) ? <p className="text-[11px] text-amber-600">Select at least one value to apply this range.</p> : null}
      </div>
      {values.isPending ? <p className="text-xs text-muted-foreground">Loading values…</p> : values.error ? <p className="text-xs text-destructive">Values could not be loaded.</p> : options.length ? options.map((option) => { const token = facetToken(option.value); const label = option.label ?? labelFor(token); return <label key={token} className="flex min-h-8 cursor-pointer items-center gap-2 overflow-hidden rounded-md px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" checked={selected.includes(token)} onChange={() => onToggle(field.key, token)} className="size-4 shrink-0 accent-primary" /><span className="min-w-0 flex-1 truncate" title={label}>{label}</span><span className="font-mono text-xs tabular-nums text-muted-foreground">{option.count.toLocaleString()}</span></label>; }) : <p className="text-xs text-muted-foreground">No coding values.</p>}
    </div> : null}
  </div>;
}

function BatchFacetSection({ matterId, batchId, searchRequest, definition, selected, negated, custodianNames, onToggle, onToggleNegation }: {
  matterId: string;
  batchId: string;
  searchRequest: MatterSearchRequest;
  definition: MetadataDefinitionRead;
  selected: string[];
  negated: boolean;
  custodianNames: Map<string, string>;
  onToggle: (key: string, value: string) => void;
  onToggleNegation: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [valueQuery, setValueQuery] = useState("");
  const debouncedQuery = useDebouncedValue(valueQuery.trim(), 250);
  const [valueLimit, setValueLimit] = useState(FACET_PAGE_SIZE);
  const enumLabels = useMemo(() => new Map((definition.allowed_values ?? []).map((option) => [option.key, option.label])), [definition.allowed_values]);
  const values = useQuery({
    queryKey: ["review-batch-facet-values", matterId, batchId, definition.key, searchRequest, debouncedQuery, valueLimit],
    queryFn: () => coreApi<MatterFacetValuesResponse>(`/v1/matters/${matterId}/review-batches/${batchId}/facets/${definition.key}/values`, {
      method: "POST",
      body: JSON.stringify({ search: searchRequest, query: debouncedQuery || null, size: valueLimit }),
    }),
    enabled: open,
    placeholderData: (previous) => previous,
  });
  const options = useMemo(() => {
    const available = new Map((values.data?.values ?? []).map((option) => [facetToken(option.value), option]));
    const hasValueCount = Math.max((values.data?.total_count ?? 0) - (values.data?.missing_count ?? 0), 0);
    if (hasValueCount > 0 || selected.includes(HAS_VALUE_FILTER_TOKEN)) {
      available.set(HAS_VALUE_FILTER_TOKEN, { value: HAS_VALUE_FILTER_TOKEN, count: hasValueCount });
    }
    if ((values.data?.missing_count ?? 0) > 0 || selected.includes(NO_VALUE_FILTER_TOKEN)) {
      available.set(NO_VALUE_FILTER_TOKEN, { value: NO_VALUE_FILTER_TOKEN, count: values.data?.missing_count ?? 0 });
    }
    for (const token of selected) {
      if (!available.has(token)) available.set(token, { value: token, count: 0 });
    }
    return [...available.values()];
  }, [selected, values.data?.missing_count, values.data?.total_count, values.data?.values]);
  const labelFor = (token: string) => token === HAS_VALUE_FILTER_TOKEN ? "Has value"
    : token === NO_VALUE_FILTER_TOKEN ? "No value"
    : definition.key === "custodian" ? custodianNames.get(token) ?? token
    : definition.type === "ENUM" ? enumLabels.get(token) ?? token
      : definition.type === "BOOLEAN" ? token === "true" ? "Yes" : "No"
        : definition.key === "file_extension" ? `.${token.replace(/^\./, "")}` : token;

  return <section>
    <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="flex min-h-10 w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold hover:bg-muted/60">
      {open ? <ChevronDown className="size-4 shrink-0 text-muted-foreground" /> : <ChevronRight className="size-4 shrink-0 text-muted-foreground" />}
      <span className="min-w-0 flex-1 truncate">{definition.display_name}</span>{selected.length ? <Badge variant="accent">{selected.length}</Badge> : null}
    </button>
    {open ? <div className="space-y-2 px-3 pb-3">
      {definition.type !== "BOOLEAN" ? <div className="relative"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" /><Input value={valueQuery} onChange={(event) => { setValueQuery(event.target.value); setValueLimit(event.target.value.trim() ? 20 : FACET_PAGE_SIZE); }} className="h-8 pl-8 text-sm" placeholder={`Find ${definition.display_name.toLowerCase()}…`} aria-label={`Find ${definition.display_name} values`} /></div> : null}
      {values.isPending ? <p className="text-xs text-muted-foreground">Loading values…</p>
        : values.error ? <p className="text-xs text-destructive">Values could not be loaded.</p>
          : options.length ? <div><div className="max-h-[min(50dvh,32rem)] space-y-0.5 overflow-y-auto overscroll-contain pr-1">{options.map((option) => {
            const token = facetToken(option.value);
            const label = labelFor(token);
            const checked = selected.includes(token);
            return <label key={token} className="flex min-h-8 cursor-pointer items-center gap-2 overflow-hidden rounded-md px-1.5 py-1 text-sm hover:bg-muted"><input type="checkbox" checked={checked} onChange={() => onToggle(definition.key, token)} className="sr-only" /><span aria-hidden="true" data-selection-mode={checked ? negated ? "excluded" : "included" : "unselected"} className={cn("grid size-4 shrink-0 place-items-center rounded-sm border", checked ? negated ? "border-destructive bg-destructive text-destructive-foreground" : "border-primary bg-primary text-primary-foreground" : "border-input bg-background")}>{checked ? negated ? <X className="size-3" strokeWidth={3} /> : <Check className="size-3" strokeWidth={3} /> : null}</span><span className="min-w-0 flex-1 truncate" title={label}>{label}</span><span className="font-mono text-xs tabular-nums text-muted-foreground">{option.count.toLocaleString()}</span></label>;
          })}</div><div className="flex min-h-8 items-center justify-between">{values.data?.has_more ? <button type="button" className="h-8 px-1.5 text-sm font-medium text-primary underline-offset-4 hover:underline disabled:opacity-50" aria-label={`Show more ${definition.display_name} values`} disabled={values.isFetching} onClick={() => setValueLimit((current) => current + FACET_PAGE_SIZE)}>{values.isFetching ? "Loading…" : "More"}</button> : <span />}<button type="button" className={cn("h-8 px-1.5 text-sm font-medium underline-offset-4 hover:underline", negated ? "text-destructive" : "text-primary")} onClick={onToggleNegation}>{negated ? "Include" : "Negate"}</button></div></div> : <p className="text-xs text-muted-foreground">No matching values.</p>}
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
        return <div key={field.id}><label className="text-xs font-medium text-muted-foreground" htmlFor={`batch-field-${field.id}`}>{definition.display_name}</label>{definition.description ? <p className="mt-0.5 text-xs text-muted-foreground">{definition.description}</p> : null}<div className="mt-1.5"><BatchFieldInput id={`batch-field-${field.id}`} definition={definition} values={drafts[field.metadata_definition_id] ?? []} disabled={disabled || saving} onChange={(values) => setDrafts((current) => ({ ...current, [field.metadata_definition_id]: values }))} /></div>{visible.map((value) => <div key={value.reviewer_coding_id} className="mt-2 rounded-md border bg-muted/35 px-2.5 py-2 text-xs"><span className="font-medium">{value.actor_user.display_name}</span><span className="text-muted-foreground"> · {value.values.map(String).join(", ") || "Not set"}</span></div>)}</div>;
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
