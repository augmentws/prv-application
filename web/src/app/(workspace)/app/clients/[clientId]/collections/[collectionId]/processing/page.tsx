import type { Metadata } from "next";

import { CollectionView } from "@/components/views/collection-view";

export const metadata: Metadata = { title: "Text processing" };

export default function CollectionProcessingPage() {
  return <CollectionView section="processing" />;
}
