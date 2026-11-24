import type { Metadata } from "next";

import { MatterCodingComparisons } from "@/components/matter-coding-comparisons";

export const metadata: Metadata = { title: "Compare coding" };

export default async function CodingComparisonsPage({ params }: { params: Promise<{ clientId: string; matterId: string }> }) {
  const { clientId, matterId } = await params;
  return <MatterCodingComparisons matterId={matterId} batchesHref={`/app/clients/${clientId}/matters/${matterId}/batches`} />;
}
