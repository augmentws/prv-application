import "server-only";

import type { NextResponse } from "next/server";

export const ACCESS_COOKIE = "pv_access";
export const REFRESH_COOKIE = "pv_refresh";
export const CSRF_COOKIE = "pv_csrf";

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  access_expires_in: number;
  refresh_expires_in: number;
}

const CORE_API_URL = (process.env.CORE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export function coreUrl(path: string) {
  return `${CORE_API_URL}/${path.replace(/^\//, "")}`;
}

export function isSameOrigin(request: Request) {
  const origin = request.headers.get("origin");
  return origin !== null && origin === new URL(request.url).origin;
}

export async function requestCore(path: string, init: RequestInit = {}) {
  return fetch(coreUrl(path), { ...init, cache: "no-store" });
}

export async function readCoreError(response: Response) {
  const payload = (await response.json().catch(() => null)) as
    | { error?: { message?: string }; detail?: string }
    | null;
  return payload?.error?.message ?? payload?.detail ?? "The service could not complete the request.";
}

export function setSessionCookies(response: NextResponse, pair: TokenPair) {
  const secure = process.env.NODE_ENV === "production";
  response.cookies.set(ACCESS_COOKIE, pair.access_token, {
    httpOnly: true,
    secure,
    sameSite: "lax",
    path: "/",
    maxAge: pair.access_expires_in,
  });
  response.cookies.set(REFRESH_COOKIE, pair.refresh_token, {
    httpOnly: true,
    secure,
    sameSite: "strict",
    path: "/",
    maxAge: pair.refresh_expires_in,
  });
}

export function setCsrfCookie(response: NextResponse, token = crypto.randomUUID()) {
  response.cookies.set(CSRF_COOKIE, token, {
    httpOnly: false,
    secure: process.env.NODE_ENV === "production",
    sameSite: "strict",
    path: "/",
    maxAge: 60 * 60 * 24 * 14,
  });
}

export function clearSessionCookies(response: NextResponse) {
  for (const name of [ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE]) {
    response.cookies.set(name, "", { httpOnly: name !== CSRF_COOKIE, path: "/", maxAge: 0 });
  }
}
