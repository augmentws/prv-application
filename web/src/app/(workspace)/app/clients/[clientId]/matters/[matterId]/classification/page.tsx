import type { Metadata } from "next";

import { ClassificationSection } from "@/components/matter/sections/classification-section";

export const metadata: Metadata = { title: "Classifiers" };

export default function MatterClassificationPage() {
  return <ClassificationSection />;
}
