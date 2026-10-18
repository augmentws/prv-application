import type { Metadata } from "next";

import { MatterDefinitionPanel } from "@/components/matter-definition-panel";

export const metadata: Metadata = { title: "Matter definition" };

export default async function MatterDefinitionPage({ params }: PageProps<"/app/clients/[clientId]/matters/[matterId]/definition">) {
  const { matterId } = await params;
  return <MatterDefinitionPanel matterId={matterId} />;
}
