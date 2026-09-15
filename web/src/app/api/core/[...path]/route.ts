import { NextRequest, NextResponse } from "next/server";

import {
  ACCESS_COOKIE,
  clearSessionCookies,
  CSRF_COOKIE,
  isSameOrigin,
  REFRESH_COOKIE,
  requestCore,
  setSessionCookies,
  type TokenPair,
} from "@/lib/server/core-api";

const safeMethods = new Set(["GET", "HEAD", "OPTIONS"]);

async function refreshSession(request: NextRequest) {
  const refreshToken = request.cookies.get(REFRESH_COOKIE)?.value;
  if (!refreshToken) return null;

  const response = await requestCore("/v1/auth/refresh", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ refresh_token: refreshToken }),
  }).catch(() => null);

  if (!response?.ok) return null;
  return (await response.json()) as TokenPair;
}

function validateMutation(request: NextRequest) {
  if (safeMethods.has(request.method)) return true;
  const cookieToken = request.cookies.get(CSRF_COOKIE)?.value;
  return isSameOrigin(request) && Boolean(cookieToken) && cookieToken === request.headers.get("x-csrf-token");
}

async function handler(request: NextRequest, context: RouteContext<"/api/core/[...path]">) {
  const { path } = await context.params;
  const corePath = path.join("/");
  if (!corePath.startsWith("v1/") || corePath === "v1/auth/login" || corePath === "v1/auth/refresh") {
    return NextResponse.json({ message: "Unsupported API route." }, { status: 404 });
  }
  if (!validateMutation(request)) {
    return NextResponse.json({ message: "Request could not be verified." }, { status: 403 });
  }

  const requestBody = safeMethods.has(request.method) ? undefined : await request.arrayBuffer();
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  headers.set("accept", "application/json");

  async function forward(accessToken: string | undefined) {
    const forwardedHeaders = new Headers(headers);
    if (accessToken) forwardedHeaders.set("authorization", `Bearer ${accessToken}`);
    return requestCore(`/${corePath}${request.nextUrl.search}`, {
      method: request.method,
      headers: forwardedHeaders,
      body: requestBody,
    });
  }

  let pair: TokenPair | null = null;
  let coreResponse: Response;
  try {
    coreResponse = await forward(request.cookies.get(ACCESS_COOKIE)?.value);
    if (coreResponse.status === 401) {
      pair = await refreshSession(request);
      if (pair) coreResponse = await forward(pair.access_token);
    }
  } catch {
    return NextResponse.json({ message: "The Priv-View service is unavailable." }, { status: 503 });
  }

  if (coreResponse.status === 401 && !pair) {
    const response = NextResponse.json({ message: "Your session has expired." }, { status: 401 });
    clearSessionCookies(response);
    return response;
  }

  const responseBody = await coreResponse.arrayBuffer();
  const responseHeaders = new Headers();
  for (const name of ["content-type", "content-disposition", "content-length", "etag"]) {
    const value = coreResponse.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  const response = new NextResponse(responseBody.byteLength ? responseBody : null, {
    status: coreResponse.status,
    headers: responseHeaders,
  });
  response.headers.set("cache-control", "no-store");
  if (pair) setSessionCookies(response, pair);
  return response;
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
