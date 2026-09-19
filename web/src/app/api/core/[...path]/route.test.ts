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
});
