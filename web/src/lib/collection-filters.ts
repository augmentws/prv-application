import type { MatterDocumentImportCreate } from "@/generated/models";
import { parseDateOnly, utcDayEnd, utcDayStart } from "@/lib/date-only";

/** The Documents facet groups, in the order the filter panel shows them. */
export const COLLECTION_FACET_KEYS = ["custodians", "file_extensions", "record_types", "processing_statuses"] as const;

export type CollectionFacetKey = typeof COLLECTION_FACET_KEYS[number];
export type CollectionFacetFilters = Record<CollectionFacetKey, string[]>;
export interface FileDateRange { from: string; to: string }

/** Everything that decides what Documents shows. It lives in the URL, so Back, reload and shared links restore it. */
export interface CollectionFilters {
  search: string;
  facets: CollectionFacetFilters;
  fileDateRange: FileDateRange;
  offset: number;
  /** The document open in the viewer, if any. */
  itemId: string | null;
}

/** Short page-address names for each facet group. */
const FACET_PARAMS: Record<CollectionFacetKey, string> = {
  custodians: "custodian",
  file_extensions: "ext",
  record_types: "type",
  processing_statuses: "status",
};

/** The Core search API's names for each facet group. */
const FACET_API_PARAMS: Record<CollectionFacetKey, string> = {
  custodians: "custodian_id",
  file_extensions: "extension",
  record_types: "record_type",
  processing_statuses: "processing_status",
};

export function emptyFacetFilters(): CollectionFacetFilters {
  return { custodians: [], file_extensions: [], record_types: [], processing_statuses: [] };
}

export function emptyCollectionFilters(): CollectionFilters {
  return { search: "", facets: emptyFacetFilters(), fileDateRange: { from: "", to: "" }, offset: 0, itemId: null };
}

/** Reads Documents filters from a page query string. Malformed values are dropped rather than sent to the API. */
export function parseCollectionFilters(params: { get(name: string): string | null; getAll(name: string): string[] }): CollectionFilters {
  const facets = emptyFacetFilters();
  for (const key of COLLECTION_FACET_KEYS) {
    facets[key] = [...new Set(params.getAll(FACET_PARAMS[key]).map((value) => value.trim()).filter(Boolean))];
  }
  const offset = Number(params.get("offset") ?? "0");
  return {
    search: (params.get("q") ?? "").trim(),
    facets,
    fileDateRange: { from: dateOnly(params.get("from")), to: dateOnly(params.get("to")) },
    offset: Number.isInteger(offset) && offset > 0 ? offset : 0,
    itemId: params.get("item") || null,
  };
}

/** The page query string for these filters. Defaults are left out, so an unfiltered view is the bare address. */
export function collectionFilterSearch(filters: CollectionFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.search) params.set("q", filters.search);
  for (const key of COLLECTION_FACET_KEYS) {
    for (const value of filters.facets[key]) params.append(FACET_PARAMS[key], value);
  }
  if (filters.fileDateRange.from) params.set("from", filters.fileDateRange.from);
  if (filters.fileDateRange.to) params.set("to", filters.fileDateRange.to);
  if (filters.offset > 0) params.set("offset", String(filters.offset));
  if (filters.itemId) params.set("item", filters.itemId);
  return params;
}

export function hasCollectionFilters(filters: CollectionFilters): boolean {
  return Boolean(filters.search || filters.fileDateRange.from || filters.fileDateRange.to)
    || COLLECTION_FACET_KEYS.some((key) => filters.facets[key].length > 0);
}

/** The Core search and facet API parameters for these filters (paging excluded). */
export function collectionSearchApiParams(filters: CollectionFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.search) params.set("q", filters.search);
  const fileDateFrom = utcDayStart(filters.fileDateRange.from);
  const fileDateTo = utcDayEnd(filters.fileDateRange.to);
  if (fileDateFrom) params.set("file_date_from", fileDateFrom);
  if (fileDateTo) params.set("file_date_to", fileDateTo);
  for (const key of COLLECTION_FACET_KEYS) {
    for (const value of filters.facets[key]) params.append(FACET_API_PARAMS[key], value);
  }
  return params;
}

/** The frozen "Add all matching" selection: exactly the documents the current filters show. */
export function toImportSelection(filters: CollectionFilters): MatterDocumentImportCreate["selection"] {
  return {
    mode: "QUERY",
    q: filters.search || null,
    custodian_ids: filters.facets.custodians,
    file_extensions: filters.facets.file_extensions,
    record_types: filters.facets.record_types as MatterDocumentImportCreate["selection"]["record_types"],
    processing_statuses: filters.facets.processing_statuses as MatterDocumentImportCreate["selection"]["processing_statuses"],
    file_date_from: utcDayStart(filters.fileDateRange.from),
    file_date_to: utcDayEnd(filters.fileDateRange.to),
    item_ids: [],
  };
}

function dateOnly(value: string | null) {
  return value && parseDateOnly(value) ? value : "";
}
