"use client";

import { useCollection } from "@/components/collection/collection-context";
import { CollectionProcessingPanel } from "@/components/collection-processing-panel";

export function ProcessingSection() {
  const { collectionId } = useCollection();
  return <CollectionProcessingPanel collectionId={collectionId} />;
}
