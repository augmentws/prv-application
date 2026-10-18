"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { createContext, useContext, type ReactNode } from "react";

import { QueryError, TableLoading } from "@/components/query-state";
import type { ClientRead, EffectivePermissionsRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

interface MatterValue {
  clientId: string;
  matterId: string;
  client: ClientRead;
  matter: MatterRead;
  /** The current user's matter permissions; empty until loaded. UI visibility only: the API enforces every permission. */
  permissionKeys: ReadonlySet<string>;
  permissions: UseQueryResult<EffectivePermissionsRead>;
}

const MatterContext = createContext<MatterValue | null>(null);

/** Loads the client, matter, and the user's matter permissions once for every matter page. */
export function MatterProvider({ clientId, matterId, children }: { clientId: string; matterId: string; children: ReactNode }) {
  // Same keys and requests as the sidebar's matter menu, so both share one response.
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });

  if (client.isPending || matter.isPending) return <TableLoading />;
  if (client.error || matter.error) return <QueryError message={client.error?.message ?? matter.error?.message} />;

  return (
    <MatterContext.Provider value={{ clientId, matterId, client: client.data, matter: matter.data, permissionKeys: new Set(permissions.data?.permission_keys ?? []), permissions }}>
      {children}
    </MatterContext.Provider>
  );
}

export function useMatter() {
  const context = useContext(MatterContext);
  if (!context) throw new Error("useMatter must be used inside MatterProvider");
  return context;
}
