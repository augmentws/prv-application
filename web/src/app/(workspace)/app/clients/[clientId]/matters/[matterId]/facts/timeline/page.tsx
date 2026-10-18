import type { Metadata } from "next";

import { FactsSection } from "@/components/matter/sections/facts-section";

export const metadata: Metadata = { title: "Facts timeline" };

export default function MatterFactsTimelinePage() {
  return <FactsSection tab="timeline" />;
}
