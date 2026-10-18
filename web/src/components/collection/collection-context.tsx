"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import { QueryError, TableLoading } from "@/components/query-state";
import type { ClientRead, CollectionRead, EffectivePermissionsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

interface CollectionValue {
  clientId: string;
  collectionId: string;
  client: ClientRead;
  collection: CollectionRead;
  /** The current user's collection permissions; empty until loaded. UI visibility only: the API enforces every permission. */
  permissionKeys: ReadonlySet<string>;
  permissions: UseQueryResult<EffectivePermissionsRead>;
}

const CollectionContext = createContext<CollectionValue | null>(null);

/** Loads the client, collection, and the user's collection permissions once for every collection page. */
export function CollectionProvider({ clientId, collectionId, children }: { clientId: string; collectionId: string; children: ReactNode }) {
  // Same keys and requests as the sidebar's collection menu, so both share one response.
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const collection = useQuery({ queryKey: ["collection", collectionId], queryFn: () => coreApi<CollectionRead>(`/v1/collections/${collectionId}`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "COLLECTION", collectionId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=COLLECTION&resource_id=${collectionId}`),
  });

  if (client.isPending || collection.isPending) return <TableLoading />;
  if (client.error || collection.error) return <QueryError message={client.error?.message ?? collection.error?.message} />;
  if (collection.data.client_id !== clientId) return <QueryError message="This collection does not belong to the selected client." />;

  return (
    <CollectionContext.Provider value={{ clientId, collectionId, client: client.data, collection: collection.data, permissionKeys: new Set(permissions.data?.permission_keys ?? []), permissions }}>
      {children}
    </CollectionContext.Provider>
  );
}

export function useCollection() {
  const context = useContext(CollectionContext);
  if (!context) throw new Error("useCollection must be used inside CollectionProvider");
  return context;
}
