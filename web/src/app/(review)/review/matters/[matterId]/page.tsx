import type { Metadata } from "next";

import { ReviewWorkspace } from "@/components/review-workspace";
import type { MatterSearchRequestSearchMode } from "@/generated/models";

export const metadata: Metadata = { title: "Search & Review" };

export default async function ReviewPage({
  params,
  searchParams,
}: {
  params: Promise<{ matterId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { matterId } = await params;
  const query = await searchParams;
  const initialFilters: Record<string, string[]> = {};

  for (const [key, value] of Object.entries(query)) {
    if (!key.startsWith("f_") || value === undefined) continue;
    initialFilters[key.slice(2)] = Array.isArray(value) ? value : [value];
  }

  const rawPage = Array.isArray(query.page) ? query.page[0] : query.page;
  const page = Number.parseInt(rawPage ?? "1", 10);
  const rawMode = (Array.isArray(query.mode) ? query.mode[0] : query.mode)?.toUpperCase();
  const searchMode: MatterSearchRequestSearchMode = rawMode === "SEMANTIC" || rawMode === "HYBRID"
    ? rawMode
    : "KEYWORD";

  return (
    <ReviewWorkspace
      matterId={matterId}
      initialQuery={(Array.isArray(query.q) ? query.q[0] : query.q) ?? ""}
      initialFilters={initialFilters}
      initialDocumentId={Array.isArray(query.document) ? query.document[0] : query.document}
      initialPage={Number.isFinite(page) && page > 0 ? page : 1}
      initialSearchMode={searchMode}
    />
  );
}
