import type { Metadata } from "next";

import { CollectionView } from "@/components/views/collection-view";

export const metadata: Metadata = { title: "File dates" };

export default function CollectionFileDatesPage() {
  return <CollectionView section="file-dates" />;
}
