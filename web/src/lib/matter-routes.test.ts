import { describe, expect, it } from "vitest";

import { isMatterSectionPage, jobTypeFromSlug, legacyMatterJobsHref, legacyMatterTabHref, MATTER_JOB_TYPES, matterHref, matterJobsHref, matterSectionFromPath } from "@/lib/matter-routes";

const base = "/app/clients/client-1/matters/matter-1";

describe("matterHref", () => {
  it("builds section paths with optional parameters", () => {
    expect(matterHref("client-1", "matter-1")).toBe(base);
    expect(matterHref("client-1", "matter-1", "facts-timeline")).toBe(`${base}/facts/timeline`);
    expect(matterHref("client-1", "matter-1", "jobs", { job_type: "SEARCH_EXPORTS" })).toBe(`${base}/jobs?job_type=SEARCH_EXPORTS`);
    expect(matterHref("client-1", "matter-1", "batches", new URLSearchParams())).toBe(`${base}/batches`);
  });
});

describe("legacyMatterTabHref", () => {
  it.each([
    ["tab=overview", base],
    ["tab=metadata", `${base}/metadata`],
    ["tab=metadata&metadata=groups", `${base}/metadata/groups`],
    ["metadata=groups", `${base}/metadata/groups`],
    ["tab=groups", `${base}/metadata/groups`],
    ["tab=definition", `${base}/definition`],
    ["tab=facts", `${base}/facts`],
    ["tab=facts&facts=timeline", `${base}/facts/timeline`],
    ["tab=entities", `${base}/entities`],
    ["tab=entities&entities=proposed", `${base}/entities/proposed`],
    ["tab=analysis", `${base}/analysis`],
    ["tab=batches", `${base}/batches`],
    ["tab=classification", `${base}/classification`],
    ["tab=usage", `${base}/usage`],
    ["tab=search", `${base}/search-index`],
    ["tab=jobs", `${base}/jobs/imports`],
    ["tab=jobs&job_type=CLASSIFICATION_ANALYSIS&job=run-1", `${base}/jobs/classification-analysis?job=run-1`],
    ["tab=jobs&job=import-1", `${base}/jobs/imports?job=import-1`],
    ["tab=unknown", base],
  ])("redirects ?%s", (query, expected) => {
    expect(legacyMatterTabHref("client-1", "matter-1", new URLSearchParams(query))).toBe(expected);
  });

  it("keeps fact and entity filters", () => {
    expect(legacyMatterTabHref("client-1", "matter-1", new URLSearchParams("tab=facts&facts=timeline&ff_issue_key=pricing&fentity=e-1"))).toBe(`${base}/facts/timeline?ff_issue_key=pricing&fentity=e-1`);
    expect(legacyMatterTabHref("client-1", "matter-1", new URLSearchParams("tab=entities&entities=proposed&eq=acme"))).toBe(`${base}/entities/proposed?eq=acme`);
  });

  it("leaves current URLs alone", () => {
    expect(legacyMatterTabHref("client-1", "matter-1", new URLSearchParams())).toBeNull();
    expect(legacyMatterTabHref("client-1", "matter-1", new URLSearchParams("job=import-1"))).toBeNull();
  });
});

describe("matterSectionFromPath", () => {
  it.each([
    [base, "overview"],
    [`${base}/metadata`, "metadata"],
    [`${base}/metadata/groups`, "metadata-groups"],
    [`${base}/facts`, "facts"],
    [`${base}/facts/timeline`, "facts-timeline"],
    [`${base}/facts/fact-1`, "facts"],
    [`${base}/entities/proposed`, "entities-proposed"],
    [`${base}/entities/entity-1`, "entities"],
    [`${base}/classification-models/model-1`, "classification"],
    [`${base}/topic-jobs/job-1`, "jobs"],
    [`${base}/search-index`, "search-index"],
  ])("maps %s to %s", (pathname, section) => {
    expect(matterSectionFromPath(pathname)).toBe(section);
  });

  it("ignores paths outside a matter", () => {
    expect(matterSectionFromPath("/app/clients/client-1")).toBeNull();
    expect(matterSectionFromPath("/review/matters/matter-1")).toBeNull();
  });
});

describe("job routes", () => {
  it("round-trips every job type through its slug", () => {
    for (const { type, slug } of MATTER_JOB_TYPES) {
      expect(jobTypeFromSlug(slug)).toBe(type);
      expect(matterJobsHref("client-1", "matter-1", type)).toBe(`${base}/jobs/${slug}`);
    }
    expect(jobTypeFromSlug("unknown")).toBeNull();
    expect(new Set(MATTER_JOB_TYPES.map((item) => item.slug)).size).toBe(MATTER_JOB_TYPES.length);
  });

  it("selects a job with a query parameter", () => {
    expect(matterJobsHref("client-1", "matter-1", "CLASSIFICATION_ANALYSIS", { job: "run-1" })).toBe(`${base}/jobs/classification-analysis?job=run-1`);
  });

  it.each([
    ["job_type=SEARCH_EXPORTS", `${base}/jobs/search-exports`],
    ["job_type=TOPIC_CLUSTERING&job=topic-1", `${base}/jobs/topic-clustering?job=topic-1`],
    ["job=import-1", `${base}/jobs/imports?job=import-1`],
    ["job_type=UNKNOWN", `${base}/jobs/imports`],
    ["", `${base}/jobs/imports`],
  ])("redirects /jobs?%s", (query, expected) => {
    expect(legacyMatterJobsHref("client-1", "matter-1", new URLSearchParams(query))).toBe(expected);
  });

  it.each([
    [base, true],
    [`${base}/facts`, true],
    [`${base}/facts/`, true],
    [`${base}/jobs/imports`, true],
    [`${base}/jobs`, true],
    [`${base}/jobs/unknown`, false],
    [`${base}/facts/fact-1`, false],
    [`${base}/topic-jobs/job-1`, false],
    ["/app/clients/client-1/matters/matter-2/facts", false],
  ])("treats %s as a section page: %s", (pathname, expected) => {
    expect(isMatterSectionPage("client-1", "matter-1", pathname)).toBe(expected);
  });
});
