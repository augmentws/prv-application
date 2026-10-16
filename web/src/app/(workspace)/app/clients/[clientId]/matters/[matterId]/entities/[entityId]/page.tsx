import type { Metadata } from "next";

import { EntityDetail } from "@/components/entities/entity-detail";

export const metadata: Metadata = { title: "Entity" };

export default async function EntityPage({
  params,
}: {
  params: Promise<{ clientId: string; matterId: string; entityId: string }>;
}) {
  const { clientId, matterId, entityId } = await params;
  return <EntityDetail clientId={clientId} matterId={matterId} entityId={entityId} />;
}
