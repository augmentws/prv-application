import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { POST } from "@/app/api/auth/login/route";

vi.mock("server-only", () => ({}));

const tokenPair = {
  access_token: "access-token",
  refresh_token: "refresh-token",
  token_type: "bearer",
  access_expires_in: 900,
  refresh_expires_in: 1_209_600,
};

afterEach(() => {
  delete process.env.APP_ORIGIN;
  vi.unstubAllGlobals();
});

describe("login route", () => {
  it("accepts the configured public origin behind a proxy", async () => {
    process.env.APP_ORIGIN = "https://priv.augment.ws";
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(tokenPair), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://internal-next:3000/api/auth/login", {
      method: "POST",
      headers: {
        origin: "https://priv.augment.ws",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        email: "admin@example.com",
        password: "long-enough-password",
      }),
    });

    const response = await POST(request);

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ authenticated: true });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("authenticates a native form fallback without putting credentials in the URL", async () => {
    process.env.APP_ORIGIN = "https://priv.augment.ws";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(tokenPair), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const body = new URLSearchParams({
      email: "admin@example.com",
      password: "long-enough-password",
    });
    const request = new NextRequest("http://internal-next:3000/api/auth/login", {
      method: "POST",
      headers: {
        origin: "https://priv.augment.ws",
        "content-type": "application/x-www-form-urlencoded",
      },
      body,
    });

    const response = await POST(request);

    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe("https://priv.augment.ws/app");
    expect(response.headers.get("location")).not.toContain("admin%40example.com");
    expect(response.headers.get("location")).not.toContain("long-enough-password");
    expect(response.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_access="))).toBe(true);
    expect(response.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_refresh="))).toBe(true);
    expect(response.headers.getSetCookie().some((cookie) => cookie.startsWith("pv_csrf="))).toBe(true);
  });

  it("rejects an untrusted origin", async () => {
    process.env.APP_ORIGIN = "https://priv.augment.ws";
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const request = new NextRequest("http://internal-next:3000/api/auth/login", {
      method: "POST",
      headers: {
        origin: "https://attacker.example",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        email: "admin@example.com",
        password: "long-enough-password",
      }),
    });

    const response = await POST(request);

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
