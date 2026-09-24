import type { Metadata } from "next";

import { TopicReviewPage } from "@/components/topic-review-page";

export const metadata: Metadata = { title: "Review topics" };

export default async function TopicReviewRoute({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; jobId: string }>;
}) {
  const { clientId, matterId, jobId } = await params;
  return <TopicReviewPage clientId={clientId} matterId={matterId} jobId={jobId} />;
}
