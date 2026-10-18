import type { Metadata } from "next";

import { FileDatesSection } from "@/components/collection/sections/file-dates-section";

export const metadata: Metadata = { title: "File dates" };

export default function CollectionFileDatesPage() {
  return <FileDatesSection />;
}
