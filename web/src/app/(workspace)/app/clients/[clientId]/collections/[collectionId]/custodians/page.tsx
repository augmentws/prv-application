import type { Metadata } from "next";

import { CustodiansSection } from "@/components/collection/sections/custodians-section";

export const metadata: Metadata = { title: "Custodians" };

export default function CollectionCustodiansPage() {
  return <CustodiansSection />;
}
