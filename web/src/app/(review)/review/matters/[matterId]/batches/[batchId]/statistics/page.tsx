import type { Metadata } from "next";

import { BatchStatisticsPage } from "@/components/batch-statistics-page";

export const metadata: Metadata = { title: "Batch Statistics" };

export default async function StatisticsPage({
  params,
}: {
  params: Promise<{ matterId: string; batchId: string }>;
}) {
  const { matterId, batchId } = await params;
  return <BatchStatisticsPage matterId={matterId} batchId={batchId} />;
}
