import type { Metadata } from "next";

import { BatchesSection } from "@/components/matter/sections/batches-section";

export const metadata: Metadata = { title: "Batches" };

export default function MatterBatchesPage() {
  return <BatchesSection />;
}
