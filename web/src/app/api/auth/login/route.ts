import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";

import {
  appUrl,
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

async function readLoginSubmission(request: NextRequest) {
  const contentType = request.headers.get("content-type") ?? "";
  const isFormSubmission =
    contentType.startsWith("application/x-www-form-urlencoded") ||
    contentType.startsWith("multipart/form-data");
  if (isFormSubmission) {
    const form = await request.formData().catch(() => null);
    return {
      isFormSubmission,
      payload: form
        ? { email: form.get("email"), password: form.get("password") }
        : null,
    };
  }
  return {
    isFormSubmission,
    payload: await request.json().catch(() => null),
  };
}

function errorResponse(
  request: NextRequest,
  isFormSubmission: boolean,
  message: string,
  status: number,
) {
  if (!isFormSubmission) return NextResponse.json({ message }, { status });
  const target = appUrl(request, "/login");
  target.searchParams.set("error", "login_failed");
  return NextResponse.redirect(target, 303);
}

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return NextResponse.json({ message: "Request origin could not be verified." }, { status: 403 });
  }

  const submission = await readLoginSubmission(request);
  const parsed = loginSchema.safeParse(submission.payload);
  if (!parsed.success) {
    return errorResponse(
      request,
      submission.isFormSubmission,
      "Enter a valid email and password.",
      400,
    );
  }

  let coreResponse: Response;
  try {
    coreResponse = await requestCore("/v1/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(parsed.data),
    });
  } catch {
    return errorResponse(
      request,
      submission.isFormSubmission,
      "The Priv-View service is unavailable.",
      503,
    );
  }

  if (!coreResponse.ok) {
    return errorResponse(
      request,
      submission.isFormSubmission,
      await readCoreError(coreResponse),
      coreResponse.status,
    );
  }

  const pair = (await coreResponse.json()) as TokenPair;
  const response = submission.isFormSubmission
    ? NextResponse.redirect(appUrl(request, "/app"), 303)
    : NextResponse.json({ authenticated: true });
  setSessionCookies(response, pair);
  setCsrfCookie(response);
  return response;
}
