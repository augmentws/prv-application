"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import type { CustodianRead, MatterDocumentImportCreate, MatterDocumentImportRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterJobsHref } from "@/lib/matter-routes";

/** The client's matters, the destinations for "Add to matter". Same key as the client page and matter switcher. */
export function useClientMatters(clientId: string) {
  return useQuery({
    queryKey: ["matters", clientId],
    queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`),
  });
}

/** The client's custodians, used to name custodian IDs in tables and facets. */
export function useClientCustodians(clientId: string) {
  return useQuery({
    queryKey: ["custodians", clientId],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/clients/${clientId}/custodians`),
  });
}

/** Starts a document import into a matter and opens that matter's Document imports, where the job runs. */
export function useStartCollectionImport(clientId: string) {
  const router = useRouter();
  return async (matterId: string, payload: MatterDocumentImportCreate) => {
    const job = await coreApi<MatterDocumentImportRead>(`/v1/matters/${matterId}/document-imports`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    router.push(matterJobsHref(clientId, matterId, "DOCUMENT_IMPORTS", { job: job.id }));
  };
}
