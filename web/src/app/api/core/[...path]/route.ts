import { NextRequest, NextResponse } from "next/server";

import {
  ACCESS_COOKIE,
  clearSessionCookies,
  CSRF_COOKIE,
  isSameOrigin,
  REFRESH_COOKIE,
  refreshCoreSession,
  requestCore,
  setCsrfCookie,
  setSessionCookies,
  type TokenPair,
} from "@/lib/server/core-api";

const safeMethods = new Set(["GET", "HEAD", "OPTIONS"]);

function sessionExpiredResponse() {
  const response = NextResponse.json({ message: "Your session has expired." }, { status: 401 });
  clearSessionCookies(response);
  return response;
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

  // Mutation bodies stream straight to Core instead of being buffered, so a large upload never
  // sits in this process's memory. A streamed body can be read only once, so mutations refresh
  // an expired access token before forwarding and return a 401 rather than retrying.
  const hasBody = !safeMethods.has(request.method);
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const contentLength = request.headers.get("content-length");
  if (hasBody && contentLength) headers.set("content-length", contentLength);
  headers.set("accept", "application/json");

  async function forward(accessToken: string | undefined) {
    const forwardedHeaders = new Headers(headers);
    if (accessToken) forwardedHeaders.set("authorization", `Bearer ${accessToken}`);
    const init: RequestInit & { duplex?: "half" } = {
      method: request.method,
      headers: forwardedHeaders,
      body: hasBody ? request.body : undefined,
    };
    if (hasBody) init.duplex = "half";
    return requestCore(`/${corePath}${request.nextUrl.search}`, init);
  }

  let pair: TokenPair | null = null;
  let coreResponse: Response;
  try {
    let accessToken = request.cookies.get(ACCESS_COOKIE)?.value;
    if (hasBody) {
      if (!accessToken) {
        pair = await refreshCoreSession(request.cookies.get(REFRESH_COOKIE)?.value);
        accessToken = pair?.access_token;
      }
      if (!accessToken) return sessionExpiredResponse();
      coreResponse = await forward(accessToken);
    } else {
      coreResponse = await forward(accessToken);
      if (coreResponse.status === 401) {
        pair = await refreshCoreSession(request.cookies.get(REFRESH_COOKIE)?.value);
        if (pair) coreResponse = await forward(pair.access_token);
      }
    }
  } catch {
    return NextResponse.json({ message: "The Priv-View service is unavailable." }, { status: 503 });
  }

  if (coreResponse.status === 401 && !pair) return sessionExpiredResponse();

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
  if (safeMethods.has(request.method) && coreResponse.ok && !request.cookies.get(CSRF_COOKIE)) {
    setCsrfCookie(response);
  }
  return response;
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
