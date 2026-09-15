import { NextRequest, NextResponse } from "next/server";

import {
  ACCESS_COOKIE,
  clearSessionCookies,
  CSRF_COOKIE,
  isSameOrigin,
  requestCore,
} from "@/lib/server/core-api";

export async function POST(request: NextRequest) {
  const cookieToken = request.cookies.get(CSRF_COOKIE)?.value;
  const headerToken = request.headers.get("x-csrf-token");
  if (!isSameOrigin(request) || !cookieToken || cookieToken !== headerToken) {
    return NextResponse.json({ message: "Request could not be verified." }, { status: 403 });
  }

  const accessToken = request.cookies.get(ACCESS_COOKIE)?.value;
  if (accessToken) {
    await requestCore("/v1/auth/logout", {
      method: "POST",
      headers: { authorization: `Bearer ${accessToken}` },
    }).catch(() => null);
  }

  const response = new NextResponse(null, { status: 204 });
  clearSessionCookies(response);
  return response;
}
