import type { Metadata } from "next";

import { FactsSection } from "@/components/matter/sections/facts-section";

export const metadata: Metadata = { title: "Facts" };

export default function MatterFactsPage() {
  return <FactsSection tab="list" />;
}
