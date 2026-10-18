import type { Metadata } from "next";

import { AnalysisTasksPanel } from "@/components/analysis-tasks-panel";

export const metadata: Metadata = { title: "Analysis tasks" };

export default async function MatterAnalysisPage({ params }: PageProps<"/app/clients/[clientId]/matters/[matterId]/analysis">) {
  const { clientId, matterId } = await params;
  return <AnalysisTasksPanel clientId={clientId} matterId={matterId} />;
}
