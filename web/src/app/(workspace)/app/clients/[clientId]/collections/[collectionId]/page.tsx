import type { Metadata } from "next";

import { DocumentsSection } from "@/components/collection/sections/documents-section";

export const metadata: Metadata = { title: "Collection" };

export default function CollectionDocumentsPage() {
  return <DocumentsSection />;
}
