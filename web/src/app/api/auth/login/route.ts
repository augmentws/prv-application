import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";

import {
  readCoreError,
  requestCore,
  setCsrfCookie,
  setSessionCookies,
  type TokenPair,
  isSameOrigin,
} from "@/lib/server/core-api";

const loginSchema = z.object({
  email: z.email(),
  password: z.string().min(12).max(1024),
}).strict();

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return NextResponse.json({ message: "Request origin could not be verified." }, { status: 403 });
  }

  const parsed = loginSchema.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json({ message: "Enter a valid email and password." }, { status: 400 });
  }

  let coreResponse: Response;
  try {
    coreResponse = await requestCore("/v1/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(parsed.data),
    });
  } catch {
    return NextResponse.json({ message: "The Priv-View service is unavailable." }, { status: 503 });
  }

  if (!coreResponse.ok) {
    return NextResponse.json({ message: await readCoreError(coreResponse) }, { status: coreResponse.status });
  }

  const pair = (await coreResponse.json()) as TokenPair;
  const response = NextResponse.json({ authenticated: true });
  setSessionCookies(response, pair);
  setCsrfCookie(response);
  return response;
}
