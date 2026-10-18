import type { Metadata } from "next";

import { SearchIndexSection } from "@/components/matter/sections/search-index-section";

export const metadata: Metadata = { title: "Search index" };

export default function MatterSearchIndexPage() {
  return <SearchIndexSection />;
}
