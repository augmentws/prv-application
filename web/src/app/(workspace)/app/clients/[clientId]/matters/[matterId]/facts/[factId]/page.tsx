import type { Metadata } from "next";

import { FactDetail } from "@/components/facts/fact-detail";

export const metadata: Metadata = { title: "Fact" };

export default async function FactPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; factId: string }>;
}) {
  const { clientId, matterId, factId } = await params;
  return <FactDetail clientId={clientId} matterId={matterId} factId={factId} />;
}
