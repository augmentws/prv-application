import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/core/[...path]/route";

vi.mock("server-only", () => ({}));

const context = {
  params: Promise.resolve({ path: ["v1", "matters", "matter-1", "uploads"] }),
} as never;

const origin = "http://localhost:3000";
const jsonHeaders = { "content-type": "application/json" };

function uploadRequest(cookie: string, body = "payload") {
  return new NextRequest(`${origin}/api/core/v1/matters/matter-1/uploads`, {
    method: "POST",
    body,
    headers: {
      ...jsonHeaders,
      origin,
      "x-csrf-token": "csrf-token",
      cookie: `${cookie}; pv_csrf=csrf-token`,
    },
    // Node's fetch requires duplex when a request body is a stream.
    duplex: "half",
  } as ConstructorParameters<typeof NextRequest>[1]);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("core API proxy mutations", () => {
  it("streams the request body to Core without buffering it", async () => {
    const seen: { body: unknown; duplex: unknown }[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit & { duplex?: string }) => {
      seen.push({ body: init?.body, duplex: init?.duplex });
      return new Response(JSON.stringify({ ok: true }), { status: 201, headers: jsonHeaders });
    }));

    const response = await POST(uploadRequest("pv_access=access-token"), context);

    expect(response.status).toBe(201);
    expect(seen).toHaveLength(1);
    expect(seen[0].body).toBeInstanceOf(ReadableStream);
    expect(seen[0].duplex).toBe("half");
    expect(await new Response(seen[0].body as ReadableStream).text()).toBe("payload");
  });

  it("refreshes a missing access token before forwarding, and forwards the body once", async () => {
    let refreshCalls = 0;
    const bodies: string[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith("/v1/auth/refresh")) {
        refreshCalls += 1;
        return new Response(JSON.stringify({
          access_token: "fresh-access-token",
          refresh_token: "fresh-refresh-token",
          token_type: "bearer",
          access_expires_in: 900,
          refresh_expires_in: 1_209_600,
        }), { status: 200, headers: jsonHeaders });
      }
      bodies.push(await new Response(init?.body as ReadableStream).text());
      const authorized = new Headers(init?.headers).get("authorization") === "Bearer fresh-access-token";
      return authorized
        ? new Response(JSON.stringify({ ok: true }), { status: 201, headers: jsonHeaders })
        : new Response(JSON.stringify({ detail: "Token expired" }), { status: 401, headers: jsonHeaders });
    }));

    const response = await POST(uploadRequest("pv_refresh=refresh-token"), context);

    expect(response.status).toBe(201);
    expect(refreshCalls).toBe(1);
    expect(bodies).toEqual(["payload"]);
  });

  it("returns 401 for a mutation rejected by Core instead of retrying the streamed body", async () => {
    let refreshCalls = 0;
    const forwards: number[] = [];
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith("/v1/auth/refresh")) {
        refreshCalls += 1;
        return new Response(JSON.stringify({ detail: "no" }), { status: 401, headers: jsonHeaders });
      }
      forwards.push(1);
      return new Response(JSON.stringify({ detail: "Token expired" }), { status: 401, headers: jsonHeaders });
    }));

    const response = await POST(uploadRequest("pv_access=stale-access-token"), context);

    expect(response.status).toBe(401);
    expect(refreshCalls).toBe(0);
    expect(forwards).toHaveLength(1);
  });

  it("does not contact Core when no session can be refreshed", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const response = await POST(uploadRequest("pv_refresh=revoked-refresh-token"), context);

    expect(response.status).toBe(401);
    expect(fetchMock.mock.calls.some(([input]) => !String(input).endsWith("/v1/auth/refresh"))).toBe(false);
  });
});
