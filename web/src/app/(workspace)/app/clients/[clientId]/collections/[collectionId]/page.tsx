import type { Metadata } from "next";

import { CollectionView } from "@/components/views/collection-view";

export const metadata: Metadata = { title: "Collection" };

export default async function CollectionPage({
  params,
}: {
  params: Promise<{ clientId: string; collectionId: string }>;
}) {
  const { clientId, collectionId } = await params;
  return <CollectionView clientId={clientId} collectionId={collectionId} />;
}
