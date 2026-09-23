import type { Metadata } from "next";

import { BatchReviewWorkspace } from "@/components/batch-review-workspace";
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
  const batchId = Array.isArray(query.batch) ? query.batch[0] : query.batch;
  const initialDocumentId = Array.isArray(query.document) ? query.document[0] : query.document;
  const initialParagraphReference = Array.isArray(query.paragraph) ? query.paragraph[0] : query.paragraph;
  const rawPage = Array.isArray(query.page) ? query.page[0] : query.page;
  const page = Number.parseInt(rawPage ?? "1", 10);
  const initialPage = Number.isFinite(page) && page > 0 ? page : 1;
  if (batchId) {
    return <BatchReviewWorkspace matterId={matterId} batchId={batchId} initialDocumentId={initialDocumentId} initialParagraphReference={initialParagraphReference} initialPage={initialPage} />;
  }
  const initialFilters: Record<string, string[]> = {};

  for (const [key, value] of Object.entries(query)) {
    if (!key.startsWith("f_") || value === undefined) continue;
    initialFilters[key.slice(2)] = Array.isArray(value) ? value : [value];
  }

  const rawMode = (Array.isArray(query.mode) ? query.mode[0] : query.mode)?.toUpperCase();
  const searchMode: MatterSearchRequestSearchMode = rawMode === "SEMANTIC" || rawMode === "HYBRID"
    ? rawMode
    : "KEYWORD";
  const rawSimilarity = Array.isArray(query.similarity) ? query.similarity[0] : query.similarity;
  const parsedSimilarity = rawSimilarity === undefined ? null : Number(rawSimilarity);
  const minimumSimilarity = searchMode === "SEMANTIC"
    && parsedSimilarity !== null
    && Number.isFinite(parsedSimilarity)
    && parsedSimilarity >= 0
    && parsedSimilarity <= 1
    ? parsedSimilarity
    : null;

  return (
    <ReviewWorkspace
      matterId={matterId}
      initialQuery={(Array.isArray(query.q) ? query.q[0] : query.q) ?? ""}
      initialFilters={initialFilters}
      initialDocumentId={initialDocumentId}
      initialPage={initialPage}
      initialSearchMode={searchMode}
      initialMinimumSimilarity={minimumSimilarity}
    />
  );
}
