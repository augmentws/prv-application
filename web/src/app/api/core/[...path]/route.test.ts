import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "@/app/api/core/[...path]/route";

vi.mock("server-only", () => ({}));

const context = {
  params: Promise.resolve({ path: ["v1", "clients", "client-1", "matters"] }),
} as never;

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("core API proxy CSRF bootstrap", () => {
  it("repairs an authenticated legacy session missing its CSRF cookie", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ));
    const request = new NextRequest("http://localhost:3000/api/core/v1/clients/client-1/matters", {
      headers: { cookie: "pv_access=access-token" },
    });

    const response = await GET(request, context);

    expect(response.status).toBe(200);
    expect(response.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_csrf="))).toBe(true);
  });

  it("does not rotate an existing CSRF cookie", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ));
    const request = new NextRequest("http://localhost:3000/api/core/v1/clients/client-1/matters", {
      headers: { cookie: "pv_access=access-token; pv_csrf=existing-token" },
    });

    const response = await GET(request, context);

    expect(response.status).toBe(200);
    expect(response.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_csrf="))).toBe(false);
  });

  it("shares one refresh across concurrent requests with the same rotating refresh token", async () => {
    let refreshCalls = 0;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/v1/auth/refresh")) {
        refreshCalls += 1;
        return new Response(JSON.stringify({
          access_token: "fresh-access-token",
          refresh_token: "fresh-refresh-token",
          token_type: "bearer",
          access_expires_in: 900,
          refresh_expires_in: 1_209_600,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      const authorization = new Headers(init?.headers).get("authorization");
      return authorization === "Bearer fresh-access-token"
        ? new Response(JSON.stringify([]), { status: 200, headers: { "content-type": "application/json" } })
        : new Response(JSON.stringify({ detail: "Token expired" }), { status: 401, headers: { "content-type": "application/json" } });
    }));
    const headers = { cookie: "pv_access=expired-access-token; pv_refresh=shared-refresh-token" };

    const [first, second] = await Promise.all([
      GET(new NextRequest("http://localhost:3000/api/core/v1/clients/client-1/matters", { headers }), context),
      GET(new NextRequest("http://localhost:3000/api/core/v1/clients/client-1/matters", { headers }), context),
    ]);

    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    expect(refreshCalls).toBe(1);
    expect(first.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_access=fresh-access-token"))).toBe(true);
    expect(second.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_access=fresh-access-token"))).toBe(true);
  });
});
