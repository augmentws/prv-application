import type { Metadata } from "next";

import { MatterView } from "@/components/views/matter-view";

export const metadata: Metadata = { title: "Matter" };

export default async function MatterPage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]"> & {
  searchParams: Promise<{ tab?: string; metadata?: string; job?: string }>;
}) {
  const { clientId, matterId } = await params;
  const query = await searchParams;
  return <MatterView clientId={clientId} matterId={matterId} requestedTab={query.tab} requestedMetadataTab={query.metadata} selectedJobId={query.job} />;
}
