import type { Metadata } from "next";

import { MatterDefinitionPanel } from "@/components/matter-definition-panel";

export const metadata: Metadata = { title: "Matter definition" };

export default async function MatterDefinitionPage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]/definition"> & {
  searchParams: Promise<{
    guidance?: string | string[];
    assessment?: string | string[];
    compare_from?: string | string[];
    compare_to?: string | string[];
  }>;
}) {
  const { matterId } = await params;
  const query = await searchParams;
  const guidanceId = Array.isArray(query.guidance) ? query.guidance[0] : query.guidance;
  const assessmentId = Array.isArray(query.assessment) ? query.assessment[0] : query.assessment;
  const compareFromRevisionId = Array.isArray(query.compare_from) ? query.compare_from[0] : query.compare_from;
  const compareToRevisionId = Array.isArray(query.compare_to) ? query.compare_to[0] : query.compare_to;
  return (
    <MatterDefinitionPanel
      matterId={matterId}
      initialGuidanceId={guidanceId}
      initialAssessmentId={assessmentId}
      initialCompareFromRevisionId={compareFromRevisionId}
      initialCompareToRevisionId={compareToRevisionId}
    />
  );
}
