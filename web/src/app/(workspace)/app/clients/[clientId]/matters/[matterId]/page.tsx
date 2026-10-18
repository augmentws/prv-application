import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { OverviewSection } from "@/components/matter/sections/overview-section";
import { legacyMatterTabHref } from "@/lib/matter-routes";

export const metadata: Metadata = { title: "Matter" };

export default async function MatterPage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]"> & {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { clientId, matterId } = await params;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    for (const item of Array.isArray(value) ? value : value === undefined ? [] : [value]) query.append(key, item);
  }
  // Sections used to be `?tab=` values; keep old links and bookmarks working.
  const legacyHref = legacyMatterTabHref(clientId, matterId, query);
  if (legacyHref) redirect(legacyHref);
  return <OverviewSection />;
}
