import { describe, expect, it } from "vitest";

import { parseClientPath, parseCollectionPath, parseMatterPath, parseResourcePath } from "@/lib/workspace-path";

describe("parseClientPath", () => {
  it.each([
    ["/app/clients/client-1", "client-1"],
    ["/app/clients/client-1/", "client-1"],
    ["/app/clients/client-1/collections/collection-1", "client-1"],
    ["/app/clients/client-1/matters/matter-1/facts/fact-1", "client-1"],
  ])("reads the client from %s", (pathname, clientId) => {
    expect(parseClientPath(pathname)).toEqual({ clientId });
  });

  it.each(["/app/clients", "/app/clients/", "/app", "/review/matters/matter-1", "/admin/tenants"])("ignores %s", (pathname) => {
    expect(parseClientPath(pathname)).toBeNull();
  });
});

describe("parseMatterPath", () => {
  it.each([
    ["/app/clients/client-1/matters/matter-1", { clientId: "client-1", matterId: "matter-1" }],
    ["/app/clients/client-1/matters/matter-1/jobs/imports", { clientId: "client-1", matterId: "matter-1" }],
  ])("reads the matter from %s", (pathname, expected) => {
    expect(parseMatterPath(pathname)).toEqual(expected);
  });

  it.each(["/app/clients/client-1", "/app/clients/client-1/matters", "/app/clients/client-1/collections/c-1", "/review/matters/matter-1"])("ignores %s", (pathname) => {
    expect(parseMatterPath(pathname)).toBeNull();
  });
});

describe("parseCollectionPath", () => {
  it.each([
    ["/app/clients/client-1/collections/collection-1", { clientId: "client-1", collectionId: "collection-1" }],
    ["/app/clients/client-1/collections/collection-1/file-dates", { clientId: "client-1", collectionId: "collection-1" }],
  ])("reads the collection from %s", (pathname, expected) => {
    expect(parseCollectionPath(pathname)).toEqual(expected);
  });

  it.each(["/app/clients/client-1", "/app/clients/client-1/collections", "/app/clients/client-1/matters/m-1"])("ignores %s", (pathname) => {
    expect(parseCollectionPath(pathname)).toBeNull();
  });
});

describe("parseResourcePath", () => {
  it.each([
    ["/app/clients/client-1/matters/matter-1/facts", { kind: "matter", clientId: "client-1", matterId: "matter-1" }],
    ["/app/clients/client-1/collections/collection-1/custodians", { kind: "collection", clientId: "client-1", collectionId: "collection-1" }],
    ["/app/clients/client-1", null],
    ["/review/matters/matter-1", null],
  ])("reads %s", (pathname, expected) => {
    expect(parseResourcePath(pathname)).toEqual(expected);
  });
});
