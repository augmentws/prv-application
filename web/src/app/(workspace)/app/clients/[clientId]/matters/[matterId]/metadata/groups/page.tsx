import type { Metadata } from "next";

import { MetadataSection } from "@/components/matter/sections/metadata-section";

export const metadata: Metadata = { title: "Metadata groups" };

export default function MatterMetadataGroupsPage() {
  return <MetadataSection tab="groups" />;
}
