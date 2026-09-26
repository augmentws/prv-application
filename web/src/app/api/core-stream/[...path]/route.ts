import { NextRequest, NextResponse } from "next/server";

import {
  ACCESS_COOKIE,
  clearSessionCookies,
  REFRESH_COOKIE,
  requestCore,
  setSessionCookies,
  type TokenPair,
} from "@/lib/server/core-api";

async function refreshSession(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken) return null;
  const response = await requestCore("/v1/auth/refresh", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  }).catch(() => null);
  if (!response?.ok) return null;
  return response.json() as Promise<TokenPair>;
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const corePath = path.join("/");
  if (!/^v1\/matters\/[^/]+\/agent-events$/.test(corePath)) {
    return NextResponse.json({ message: "Unsupported streaming route." }, { status: 404 });
  }

  const abortController = new AbortController();
  request.signal.addEventListener("abort", () => abortController.abort(), { once: true });
  const headers = new Headers({ accept: "text/event-stream" });
  const lastEventId = request.headers.get("last-event-id");
  if (lastEventId) headers.set("last-event-id", lastEventId);

  async function forward(accessToken: string | undefined) {
    const forwarded = new Headers(headers);
    if (accessToken) forwarded.set("authorization", `Bearer ${accessToken}`);
    return requestCore(`/${corePath}${request.nextUrl.search}`, {
      method: "GET",
      headers: forwarded,
      signal: abortController.signal,
    });
  }

  let pair: TokenPair | null = null;
  let coreResponse: Response;
  try {
    coreResponse = await forward(request.cookies.get(ACCESS_COOKIE)?.value);
    if (coreResponse.status === 401) {
      await coreResponse.body?.cancel();
      pair = await refreshSession(request);
      if (pair) coreResponse = await forward(pair.access_token);
    }
  } catch {
    return NextResponse.json({ message: "The Priv-View service is unavailable." }, { status: 503 });
  }

  if (coreResponse.status === 401 && !pair) {
    await coreResponse.body?.cancel();
    const response = NextResponse.json({ message: "Your session has expired." }, { status: 401 });
    clearSessionCookies(response);
    return response;
  }

  const responseHeaders = new Headers();
  for (const name of ["content-type", "cache-control", "x-accel-buffering"]) {
    const value = coreResponse.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  responseHeaders.delete("content-length");
  responseHeaders.delete("connection");
  responseHeaders.delete("keep-alive");
  responseHeaders.delete("transfer-encoding");
  const response = new NextResponse(coreResponse.body, {
    status: coreResponse.status,
    headers: responseHeaders,
  });
  if (pair) setSessionCookies(response, pair);
  return response;
}

export const dynamic = "force-dynamic";
