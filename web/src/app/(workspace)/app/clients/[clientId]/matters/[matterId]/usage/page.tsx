import type { Metadata } from "next";

import { UsageSection } from "@/components/matter/sections/usage-section";

export const metadata: Metadata = { title: "Token usage" };

export default function MatterUsagePage() {
  return <UsageSection />;
}
