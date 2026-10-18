import type { Metadata } from "next";

import { ProcessingSection } from "@/components/collection/sections/processing-section";

export const metadata: Metadata = { title: "Text processing" };

export default function CollectionProcessingPage() {
  return <ProcessingSection />;
}
