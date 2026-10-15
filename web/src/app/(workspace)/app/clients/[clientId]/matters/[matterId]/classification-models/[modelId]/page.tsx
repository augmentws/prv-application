import type { Metadata } from "next";

import { ClassificationModelDetail } from "@/components/classification-model-detail";

export const metadata: Metadata = { title: "Classification model" };

export default async function ClassificationModelPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; modelId: string }>;
}) {
  const { clientId, matterId, modelId } = await params;
  return <ClassificationModelDetail clientId={clientId} matterId={matterId} modelId={modelId} />;
}
