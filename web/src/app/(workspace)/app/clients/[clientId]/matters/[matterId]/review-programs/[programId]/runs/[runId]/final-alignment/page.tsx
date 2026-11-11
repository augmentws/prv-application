import type { Metadata } from "next";

import { ReviewProgramFinalAlignmentPage } from "@/components/review-program-final-alignment-page";

export const metadata: Metadata = { title: "Classifier and decision-model comparison" };

export default async function FinalAlignmentPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; programId: string; runId: string }>;
}) {
  const { clientId, matterId, programId, runId } = await params;
  return <ReviewProgramFinalAlignmentPage clientId={clientId} matterId={matterId} programId={programId} runId={runId} />;
}
