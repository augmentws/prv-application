import type { Metadata } from "next";

import { EntitiesSection } from "@/components/matter/sections/entities-section";

export const metadata: Metadata = { title: "Entities" };

export default function MatterEntitiesPage() {
  return <EntitiesSection tab="directory" />;
}
