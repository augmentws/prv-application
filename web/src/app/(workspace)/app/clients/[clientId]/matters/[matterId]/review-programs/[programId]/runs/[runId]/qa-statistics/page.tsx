import type { Metadata } from "next";

import { ReviewProgramQAPopulationStatisticsPage } from "@/components/review-program-qa-population-statistics-page";

export const metadata: Metadata = { title: "Q&A population statistics" };

export default async function QAPopulationStatisticsPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; programId: string; runId: string }>;
}) {
  const { clientId, matterId, programId, runId } = await params;
  return <ReviewProgramQAPopulationStatisticsPage clientId={clientId} matterId={matterId} programId={programId} runId={runId} />;
}
