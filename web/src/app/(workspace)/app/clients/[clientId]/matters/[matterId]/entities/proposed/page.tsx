import type { Metadata } from "next";

import { EntitiesSection } from "@/components/matter/sections/entities-section";

export const metadata: Metadata = { title: "Proposed entities" };

export default function MatterEntitiesProposedPage() {
  return <EntitiesSection tab="proposed" />;
}
