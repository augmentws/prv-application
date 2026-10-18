import type { Metadata } from "next";

import { CollectionView } from "@/components/views/collection-view";

export const metadata: Metadata = { title: "Custodians" };

export default function CollectionCustodiansPage() {
  return <CollectionView section="custodians" />;
}
