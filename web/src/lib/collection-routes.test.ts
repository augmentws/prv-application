import { describe, expect, it } from "vitest";

import { collectionHref, collectionSectionFromPath, isCollectionSectionPage } from "@/lib/collection-routes";

const base = "/app/clients/client-1/collections/collection-1";

describe("collectionHref", () => {
  it("builds section paths with optional parameters", () => {
    expect(collectionHref("client-1", "collection-1")).toBe(base);
    expect(collectionHref("client-1", "collection-1", "custodians")).toBe(`${base}/custodians`);
    expect(collectionHref("client-1", "collection-1", "file-dates")).toBe(`${base}/file-dates`);
    expect(collectionHref("client-1", "collection-1", "processing")).toBe(`${base}/processing`);
    expect(collectionHref("client-1", "collection-1", "documents", { from: "2001-01-01", to: "2001-01-31" })).toBe(`${base}?from=2001-01-01&to=2001-01-31`);
    expect(collectionHref("client-1", "collection-1", "custodians", new URLSearchParams())).toBe(`${base}/custodians`);
  });
});

describe("collectionSectionFromPath", () => {
  it.each([
    [base, "documents"],
    [`${base}/`, "documents"],
    [`${base}/custodians`, "custodians"],
    [`${base}/file-dates`, "file-dates"],
    [`${base}/processing`, "processing"],
  ])("reads %s as %s", (pathname, section) => {
    expect(collectionSectionFromPath(pathname)).toBe(section);
  });

  it.each([
    "/app/clients/client-1",
    "/app/clients/client-1/collections",
    `${base}/unknown`,
    `${base}/custodians/extra`,
    "/app/clients/client-1/matters/matter-1",
  ])("ignores %s", (pathname) => {
    expect(collectionSectionFromPath(pathname)).toBeNull();
  });

  it("round-trips every section through its href", () => {
    for (const section of ["documents", "custodians", "file-dates", "processing"] as const) {
      expect(collectionSectionFromPath(collectionHref("client-1", "collection-1", section))).toBe(section);
    }
  });
});

describe("isCollectionSectionPage", () => {
  it("matches only this collection's section pages", () => {
    expect(isCollectionSectionPage("client-1", "collection-1", base)).toBe(true);
    expect(isCollectionSectionPage("client-1", "collection-1", `${base}/processing/`)).toBe(true);
    expect(isCollectionSectionPage("client-1", "collection-2", base)).toBe(false);
    expect(isCollectionSectionPage("client-1", "collection-1", `${base}/unknown`)).toBe(false);
  });
});
