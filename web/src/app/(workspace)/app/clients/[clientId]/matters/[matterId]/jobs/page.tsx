import { redirect } from "next/navigation";

import { legacyMatterJobsHref } from "@/lib/matter-routes";

/** Jobs has one page per job type; older `?job_type=` links and the bare section land on theirs. */
export default async function MatterJobsPage({
  params,
  searchParams,
}: PageProps<"/app/clients/[clientId]/matters/[matterId]/jobs"> & {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { clientId, matterId } = await params;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    for (const item of Array.isArray(value) ? value : value === undefined ? [] : [value]) query.append(key, item);
  }
  redirect(legacyMatterJobsHref(clientId, matterId, query));
}
