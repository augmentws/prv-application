export type MatterSection =
  | "overview"
  | "metadata"
  | "metadata-groups"
  | "definition"
  | "facts"
  | "facts-timeline"
  | "entities"
  | "entities-proposed"
  | "analysis"
  | "review-programs"
  | "batches"
  | "classification"
  | "jobs"
  | "usage"
  | "search-index";

const SECTION_PATHS: Record<MatterSection, string> = {
  overview: "",
  metadata: "/metadata",
  "metadata-groups": "/metadata/groups",
  definition: "/definition",
  facts: "/facts",
  "facts-timeline": "/facts/timeline",
  entities: "/entities",
  "entities-proposed": "/entities/proposed",
  analysis: "/analysis",
  "review-programs": "/review-programs",
  batches: "/batches",
  classification: "/classification",
  jobs: "/jobs",
  usage: "/usage",
  "search-index": "/search-index",
};

/** The one place matter section URLs are built. */
export function matterHref(clientId: string, matterId: string, section: MatterSection = "overview", params?: URLSearchParams | Record<string, string>) {
  const query = params ? new URLSearchParams(params).toString() : "";
  return `/app/clients/${clientId}/matters/${matterId}${SECTION_PATHS[section]}${query ? `?${query}` : ""}`;
}

export type MatterJobType = "BULK_TAGGING" | "DOCUMENT_EMBEDDINGS" | "TOPIC_CLUSTERING" | "DOCUMENT_IMPORTS" | "QUESTION_ANSWERING" | "CLASSIFICATION_ANALYSIS" | "SEARCH_EXPORTS";

/** Every job type, in menu order, with its route slug. */
export const MATTER_JOB_TYPES: { type: MatterJobType; slug: string; label: string }[] = [
  { type: "BULK_TAGGING", slug: "bulk-tagging", label: "Bulk tagging" },
  { type: "DOCUMENT_EMBEDDINGS", slug: "embeddings", label: "Document embeddings" },
  { type: "TOPIC_CLUSTERING", slug: "topic-clustering", label: "Topic clustering" },
  { type: "DOCUMENT_IMPORTS", slug: "imports", label: "Document imports" },
  { type: "QUESTION_ANSWERING", slug: "question-answering", label: "Question answering" },
  { type: "CLASSIFICATION_ANALYSIS", slug: "classification-analysis", label: "Classification analysis" },
  { type: "SEARCH_EXPORTS", slug: "search-exports", label: "Search exports" },
];

/** The job type a route slug names, or null for an unknown slug. */
export function jobTypeFromSlug(slug: string): MatterJobType | null {
  return MATTER_JOB_TYPES.find((item) => item.slug === slug)?.type ?? null;
}

/** One job type's page; `job` selects a job to show in detail. */
export function matterJobsHref(clientId: string, matterId: string, jobType: MatterJobType, params?: Record<string, string>) {
  const slug = MATTER_JOB_TYPES.find((item) => item.type === jobType)!.slug;
  const query = params ? new URLSearchParams(params).toString() : "";
  return `${matterHref(clientId, matterId, "jobs")}/${slug}${query ? `?${query}` : ""}`;
}

/**
 * Where an older Jobs URL now lives: `job_type` picks the page, and a `job` without a type was a
 * document import. Other parameters are kept.
 */
export function legacyMatterJobsHref(clientId: string, matterId: string, query: URLSearchParams): string {
  const requested = query.get("job_type");
  const jobType = MATTER_JOB_TYPES.find((item) => item.type === requested)?.type ?? "DOCUMENT_IMPORTS";
  const rest = new URLSearchParams(query);
  rest.delete("job_type");
  return matterJobsHref(clientId, matterId, jobType, Object.fromEntries(rest));
}

/** True on a section's own page (including a job type's page), false on detail pages under it. */
export function isMatterSectionPage(clientId: string, matterId: string, pathname: string): boolean {
  const path = pathname.replace(/\/$/, "");
  const section = matterSectionFromPath(path);
  if (!section) return false;
  if (path === matterHref(clientId, matterId, section)) return true;
  const jobsBase = `${matterHref(clientId, matterId, "jobs")}/`;
  return section === "jobs" && path.startsWith(jobsBase) && jobTypeFromSlug(path.slice(jobsBase.length)) !== null;
}

export function searchAndReviewHref(matterId: string) {
  return `/review/matters/${matterId}`;
}

/** Query keys that selected a section before sections were routes. */
const LEGACY_KEYS = ["tab", "metadata", "entities", "facts"] as const;

function legacySection(query: URLSearchParams): MatterSection {
  const tab = query.get("tab");
  switch (tab) {
    case "metadata":
      return query.get("metadata") === "groups" ? "metadata-groups" : "metadata";
    case "groups":
      return "metadata-groups";
    case "facts":
      return query.get("facts") === "timeline" ? "facts-timeline" : "facts";
    case "entities":
      return query.get("entities") === "proposed" ? "entities-proposed" : "entities";
    case "search":
      return "search-index";
    case "definition":
    case "analysis":
    case "review-programs":
    case "batches":
    case "classification":
    case "jobs":
    case "usage":
      return tab;
    case null:
      // Without a tab the old view defaulted to Metadata, so `?metadata=groups` alone opened Groups.
      return query.get("metadata") === "groups" ? "metadata-groups" : "overview";
    default:
      return "overview";
  }
}

/**
 * Where an old `?tab=` matter link now lives, or null when the query has no legacy keys.
 * Every other parameter (fact and entity filters, the selected job) is kept.
 */
export function legacyMatterTabHref(clientId: string, matterId: string, query: URLSearchParams): string | null {
  if (!LEGACY_KEYS.some((key) => query.has(key))) return null;
  const section = legacySection(query);
  const rest = new URLSearchParams(query);
  for (const key of LEGACY_KEYS) rest.delete(key);
  if (section === "jobs") return legacyMatterJobsHref(clientId, matterId, rest);
  return matterHref(clientId, matterId, section, rest);
}

/** The section a matter path belongs to, including detail pages under a section. */
export function matterSectionFromPath(pathname: string): MatterSection | null {
  const match = /^\/app\/clients\/[^/]+\/matters\/[^/]+(\/[^?#]*)?$/.exec(pathname);
  if (!match) return null;
  const [first = "", second = ""] = (match[1] ?? "").split("/").filter(Boolean);
  switch (first) {
    case "":
      return "overview";
    case "metadata":
      return second === "groups" ? "metadata-groups" : "metadata";
    case "facts":
      return second === "timeline" ? "facts-timeline" : "facts";
    case "entities":
      return second === "proposed" ? "entities-proposed" : "entities";
    case "classification":
    case "classification-models":
      return "classification";
    case "jobs":
    case "topic-jobs":
      return "jobs";
    case "definition":
    case "analysis":
    case "review-programs":
    case "batches":
    case "usage":
    case "search-index":
      return first;
    default:
      return null;
  }
}
