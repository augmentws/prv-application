import type { Metadata } from "next";

import { ClassificationScoringRunResults } from "@/components/classification-scoring-run-results";

export const metadata: Metadata = { title: "Classifier scoring results" };

export default async function ClassificationScoringRunResultsPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; modelId: string; scoringRunId: string }>;
}) {
  const { clientId, matterId, modelId, scoringRunId } = await params;
  return <ClassificationScoringRunResults clientId={clientId} matterId={matterId} modelId={modelId} scoringRunId={scoringRunId} />;
}
