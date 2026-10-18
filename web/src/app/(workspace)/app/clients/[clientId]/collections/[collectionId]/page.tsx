import type { Metadata } from "next";

import { CollectionView } from "@/components/views/collection-view";

export const metadata: Metadata = { title: "Collection" };

export default function CollectionDocumentsPage() {
  return <CollectionView section="documents" />;
}
