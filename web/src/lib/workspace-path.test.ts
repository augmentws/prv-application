import { describe, expect, it } from "vitest";

import { parseClientPath, parseMatterPath } from "@/lib/workspace-path";

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
