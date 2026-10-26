import type { Metadata } from "next";

import { MatterDefinitionPanel } from "@/components/matter-definition-panel";

export const metadata: Metadata = { title: "Matter definition" };

export default async function MatterDefinitionPage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]/definition"> & {
  searchParams: Promise<{ guidance?: string | string[]; assessment?: string | string[] }>;
}) {
  const { matterId } = await params;
  const query = await searchParams;
  const guidanceId = Array.isArray(query.guidance) ? query.guidance[0] : query.guidance;
  const assessmentId = Array.isArray(query.assessment) ? query.assessment[0] : query.assessment;
  return (
    <MatterDefinitionPanel
      matterId={matterId}
      initialGuidanceId={guidanceId}
      initialAssessmentId={assessmentId}
    />
  );
}
