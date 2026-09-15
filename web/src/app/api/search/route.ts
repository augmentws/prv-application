import { createFromSource } from "fumadocs-core/search/server";
import { cookies } from "next/headers";

import { docsSource } from "@/lib/docs-source";
import { ACCESS_COOKIE, REFRESH_COOKIE } from "@/lib/server/core-api";

const search = createFromSource(docsSource);

export async function GET(request: Request) {
  const cookieStore = await cookies();
  const hasSession = cookieStore.has(ACCESS_COOKIE) || cookieStore.has(REFRESH_COOKIE);

  if (!hasSession) {
    return Response.json({ detail: "Authentication required." }, { status: 401 });
  }

  return search.GET(request);
}
