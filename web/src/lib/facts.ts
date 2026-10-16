import type { FactFilters, MatterFactRead } from "@/generated/models";
import { parseDateOnly } from "@/lib/date-only";

export type FactSort = "DATE_ASC" | "DATE_DESC" | "UPDATED_DESC" | "EVIDENCE_COUNT_DESC";

export const FACT_SORT_LABELS: Record<FactSort, string> = {
  DATE_ASC: "Date, oldest first",
  DATE_DESC: "Date, newest first",
  UPDATED_DESC: "Recently updated",
  EVIDENCE_COUNT_DESC: "Most evidence",
};

/** Facets offered in the Facts list, in display order, with the filter field each one sets. */
export const FACT_FACETS = [
  { key: "status", label: "Status", field: "statuses" },
  { key: "issue_key", label: "Issue", field: "issue_keys" },
  { key: "owner", label: "Owner", field: "owner_entity_ids" },
  { key: "participant", label: "Participant", field: "participant_entity_ids" },
  { key: "date_precision", label: "Date precision", field: "date_precisions" },
  { key: "author_type", label: "Created by", field: "author_types" },
  { key: "key_fact", label: "Key fact", field: "key_fact" },
  { key: "has_contradiction", label: "Contradicted", field: "has_contradiction" },
] as const;

export type FactFacetKey = (typeof FACT_FACETS)[number]["key"];
const BOOLEAN_FACETS = new Set<FactFacetKey>(["key_fact", "has_contradiction"]);

export interface FactListState {
  query: string;
  selected: Partial<Record<FactFacetKey, string[]>>;
  dateFrom: string;
  dateTo: string;
  pendingOnly: boolean;
  /** Facts an entity owns or takes part in, across its merged variants. */
  entityId: string;
  /** Timeline only: also show working facts, marked as unconfirmed. */
  includeWorking: boolean;
  sort: FactSort;
  page: number;
}

const PARAM_PREFIX = "ff_";

export function readFactListState(params: URLSearchParams): FactListState {
  const selected: FactListState["selected"] = {};
  for (const facet of FACT_FACETS) {
    const raw = params.get(`${PARAM_PREFIX}${facet.key}`);
    if (raw) selected[facet.key] = raw.split(",").filter(Boolean);
  }
  const sort = params.get("fsort");
  const page = Number.parseInt(params.get("fpage") ?? "1", 10);
  return {
    query: params.get("fq") ?? "",
    selected,
    dateFrom: parseDateOnly(params.get("ffrom") ?? "") ? (params.get("ffrom") as string) : "",
    dateTo: parseDateOnly(params.get("fto") ?? "") ? (params.get("fto") as string) : "",
    pendingOnly: params.get("fpending") === "1",
    entityId: params.get("fentity") ?? "",
    includeWorking: params.get("fworking") === "1",
    sort: sort && sort in FACT_SORT_LABELS ? (sort as FactSort) : "DATE_ASC",
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

/** Write list state into URL params, keeping unrelated params (such as the matter tab) unchanged. */
export function writeFactListState(params: URLSearchParams, state: FactListState): URLSearchParams {
  const next = new URLSearchParams(params);
  for (const key of [...next.keys()]) {
    if (key.startsWith(PARAM_PREFIX) || ["fq", "ffrom", "fto", "fpending", "fentity", "fworking", "fsort", "fpage"].includes(key)) next.delete(key);
  }
  if (state.query.trim()) next.set("fq", state.query.trim());
  for (const facet of FACT_FACETS) {
    const values = state.selected[facet.key] ?? [];
    if (values.length) next.set(`${PARAM_PREFIX}${facet.key}`, values.join(","));
  }
  if (state.dateFrom) next.set("ffrom", state.dateFrom);
  if (state.dateTo) next.set("fto", state.dateTo);
  if (state.pendingOnly) next.set("fpending", "1");
  if (state.entityId) next.set("fentity", state.entityId);
  if (state.includeWorking) next.set("fworking", "1");
  if (state.sort !== "DATE_ASC") next.set("fsort", state.sort);
  if (state.page > 1) next.set("fpage", String(state.page));
  return next;
}

function booleanSelection(values: string[] | undefined): boolean | null {
  const unique = new Set(values ?? []);
  if (unique.size !== 1) return null;
  return unique.has("true");
}

/** The API filter for the list state; a facet's own key can be left out for self-excluding facet counts. */
export function factFilters(state: FactListState, { without }: { without?: FactFacetKey } = {}): FactFilters {
  const filters: FactFilters = {};
  if (state.query.trim()) filters.query = state.query.trim();
  for (const facet of FACT_FACETS) {
    if (facet.key === without) continue;
    const values = state.selected[facet.key];
    if (!values?.length) continue;
    if (BOOLEAN_FACETS.has(facet.key)) {
      const value = booleanSelection(values);
      if (value !== null) (filters as Record<string, unknown>)[facet.field] = value;
    } else {
      (filters as Record<string, unknown>)[facet.field] = values;
    }
  }
  if (state.dateFrom) filters.event_date_from = state.dateFrom;
  if (state.dateTo) filters.event_date_to = state.dateTo;
  if (state.pendingOnly) filters.has_pending_suggestion = true;
  if (state.entityId) filters.entity_id = state.entityId;
  return filters;
}

/** The timeline's filter: the list filters without the status facet, limited to confirmed (and optionally working) facts. */
export function timelineFilters(state: FactListState): FactFilters {
  return { ...factFilters(state, { without: "status" }), statuses: state.includeWorking ? ["CONFIRMED", "WORKING"] : ["CONFIRMED"] };
}

export function hasFactFilters(state: FactListState) {
  return Boolean(
    state.query.trim() ||
      state.dateFrom ||
      state.dateTo ||
      state.pendingOnly ||
      state.entityId ||
      Object.values(state.selected).some((values) => values?.length),
  );
}

const QUARTER_MONTHS: Record<number, string> = { 0: "Q1", 3: "Q2", 6: "Q3", 9: "Q4" };

function formatPart(value: string, options: Intl.DateTimeFormatOptions) {
  const date = parseDateOnly(value);
  return date ? new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(date) : value;
}

/** A fact's event date at its stated precision: "Mar 3, 2021", "Mar 2021", "Q1 2021", "2021", a range, or "Undated". */
export function formatFactDate(fact: Pick<MatterFactRead, "date_precision" | "event_date_start" | "event_date_end">) {
  const start = fact.event_date_start;
  if (!start || fact.date_precision === "UNKNOWN") return "Undated";
  switch (fact.date_precision) {
    case "YEAR":
      return start.slice(0, 4);
    case "QUARTER": {
      const date = parseDateOnly(start);
      return date ? `${QUARTER_MONTHS[date.getUTCMonth()] ?? ""} ${date.getUTCFullYear()}`.trim() : start;
    }
    case "MONTH":
      return formatPart(start, { month: "short", year: "numeric" });
    case "RANGE":
      return fact.event_date_end
        ? `${formatPart(start, { month: "short", day: "numeric", year: "numeric" })} – ${formatPart(fact.event_date_end, { month: "short", day: "numeric", year: "numeric" })}`
        : formatPart(start, { month: "short", day: "numeric", year: "numeric" });
    default:
      return formatPart(start, { month: "short", day: "numeric", year: "numeric" });
  }
}

export const FACT_STATUS_LABELS: Record<string, string> = {
  WORKING: "Working",
  CONFIRMED: "Confirmed",
  REJECTED: "Rejected",
  MERGED: "Merged",
};

export const FACT_STATUS_CLASSES: Record<string, string> = {
  WORKING: "border border-pending/40 bg-pending/10 text-pending",
  CONFIRMED: "border border-confirmed/40 bg-confirmed/10 text-confirmed",
  REJECTED: "border border-rejected/40 bg-rejected/10 text-rejected",
  MERGED: "border border-border bg-muted text-muted-foreground",
};

export function factDetailHref(clientId: string, matterId: string, factId: string) {
  return `/app/clients/${clientId}/matters/${matterId}/facts/${factId}`;
}

export function entityDetailHref(clientId: string, matterId: string, entityId: string) {
  return `/app/clients/${clientId}/matters/${matterId}/entities/${entityId}`;
}

/** The Facts list filtered to facts an entity owns or takes part in. */
export function entityFactsHref(clientId: string, matterId: string, entityId: string) {
  return `/app/clients/${clientId}/matters/${matterId}?${new URLSearchParams({ tab: "facts", fentity: entityId })}`;
}

export type FactsTab = "list" | "timeline";

/** Switch between the Facts sub-tabs, keeping the shared filters. */
export function factsTabParams(params: URLSearchParams, tab: FactsTab): URLSearchParams {
  const next = new URLSearchParams(params);
  next.set("tab", "facts");
  next.delete("fpage");
  if (tab === "timeline") next.set("facts", "timeline");
  else next.delete("facts");
  return next;
}

export interface TimelineSection<T> {
  key: string;
  label: string;
  facts: T[];
}

export interface TimelineYear<T> {
  year: string;
  sections: TimelineSection<T>[];
}

type TimelineFact = Pick<MatterFactRead, "date_precision" | "event_date_start" | "event_date_end">;

function timelineSection(fact: TimelineFact): { year: string; key: string; label: string } | null {
  const start = fact.event_date_start ? parseDateOnly(fact.event_date_start) : null;
  if (!start || fact.date_precision === "UNKNOWN") return null;
  const year = String(start.getUTCFullYear()).padStart(4, "0");
  if (fact.date_precision === "YEAR") return { year, key: `${year}`, label: `${year}, month unknown` };
  if (fact.date_precision === "QUARTER") {
    const quarter = QUARTER_MONTHS[start.getUTCMonth()] ?? "";
    return { year, key: `${year}-${quarter}`, label: `${quarter} ${year}` };
  }
  const month = String(start.getUTCMonth() + 1).padStart(2, "0");
  return { year, key: `${year}-${month}`, label: new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" }).format(start) };
}

/**
 * Group date-ordered facts by year, then by month (or quarter, or year only, as precise as each fact's date), keeping
 * their order. Undated facts are returned separately.
 */
export function groupTimeline<T extends TimelineFact>(facts: T[]): { years: TimelineYear<T>[]; undated: T[] } {
  const years: TimelineYear<T>[] = [];
  const undated: T[] = [];
  for (const fact of facts) {
    const section = timelineSection(fact);
    if (!section) {
      undated.push(fact);
      continue;
    }
    let year = years.at(-1);
    if (year?.year !== section.year) {
      year = { year: section.year, sections: [] };
      years.push(year);
    }
    let current = year.sections.at(-1);
    if (current?.label !== section.label) {
      // A section can recur within a year (a quarter-precision fact between two days of the same month); keys stay unique.
      current = { key: `${section.key}:${year.sections.length}`, label: section.label, facts: [] };
      year.sections.push(current);
    }
    current.facts.push(fact);
  }
  return { years, undated };
}

/**
 * Starting text for a new fact-development task. Each heading becomes a section the issue catalog can cite, so the
 * defaults are complete instructions a reviewer can keep, edit, or delete.
 */
export const FACT_GUIDANCE_TEMPLATE = `# Fact guidance

## What counts as a fact
Record one dated event, decision, statement, or action per fact, in a single self-contained sentence that names who did what.
Prefer facts that show what people knew, decided, or did about an issue, and when.

## Dates
Date each fact by when the event happened, not when the document was sent. Use the document date only when the text gives no other date.
When the text gives only a month, quarter, or year, record that precision rather than guessing a day.

## Owners
The owner is the person or organization responsible for the fact in the record, such as the person who made the decision or statement.
List other people and organizations involved as participants. The reviewing attorney is never an owner.

## Exclude
Scheduling and logistics, signatures and disclaimers, forwarded newsletters, and text quoted from earlier messages that is already covered by its original document.
`;

export const FACT_GUIDANCE_HELP =
  "How facts should be built in this matter: what counts as a fact, how to date it, who owns it, and what to leave out. Generate reads it together with the Review Guidance to write the issue catalog, and the agent follows that catalog for every document. Each heading can be cited as a source, so keep one topic per heading.";
