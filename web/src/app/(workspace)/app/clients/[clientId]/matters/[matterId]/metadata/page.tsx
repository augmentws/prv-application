import type { Metadata } from "next";

import { MetadataSection } from "@/components/matter/sections/metadata-section";

export const metadata: Metadata = { title: "Metadata fields" };

export default function MatterMetadataPage() {
  return <MetadataSection tab="definitions" />;
}
