"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname } from "next/navigation";
import { createContext, useContext, useState, type ReactNode } from "react";

import type { ClientRead, EffectivePermissionsRead, TenantRead, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { parseClientPath } from "@/lib/workspace-path";

interface WorkspaceValue {
  user: UserRead;
  tenant: TenantRead;
  tenants: TenantRead[];
  permissionKeys: string[];
  selectedTenantId: string;
  setSelectedTenantId: (tenantId: string) => void;
  /** True on client and matter routes, where the client's tenant scopes the page and cannot be switched. */
  tenantFromRoute: boolean;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const routeClientId = parseClientPath(pathname)?.clientId;
  const [selection, setSelection] = useState<string | null>(null);
  const [syncedRouteTenantId, setSyncedRouteTenantId] = useState<string | null>(null);
  const userQuery = useQuery({
    queryKey: ["session"],
    queryFn: () => coreApi<UserRead>("/v1/auth/me"),
    staleTime: 0,
  });
  const user = userQuery.data;
  // Same key and request as the client and matter views, so the page reuses this response.
  const routeClientQuery = useQuery({
    queryKey: ["client", routeClientId],
    queryFn: () => coreApi<ClientRead>(`/v1/clients/${routeClientId}`),
    enabled: Boolean(user && routeClientId),
  });
  const routeTenantId = routeClientId ? routeClientQuery.data?.tenant_id : undefined;
  // Keep the selection on the route's tenant so leaving the client (for example to Clients) stays in that tenant.
  if (routeTenantId && routeTenantId !== syncedRouteTenantId) {
    setSyncedRouteTenantId(routeTenantId);
    setSelection(routeTenantId);
  } else if (!routeClientId && syncedRouteTenantId) {
    setSyncedRouteTenantId(null);
  }
  const selectedTenantId = routeTenantId ?? selection ?? user?.tenant_id ?? "";
  // A failed client lookup is reported by the page itself; the shell then keeps the current selection.
  const routeTenantPending = Boolean(routeClientId) && routeClientQuery.isPending && !routeClientQuery.error;

  const tenantQuery = useQuery({
    queryKey: ["tenant", selectedTenantId],
    queryFn: () => coreApi<TenantRead>(`/v1/tenants/${selectedTenantId}`),
    enabled: Boolean(selectedTenantId),
  });
  const tenantsQuery = useQuery({
    queryKey: ["tenants"],
    queryFn: () => coreApi<TenantRead[]>("/v1/tenants"),
    enabled: Boolean(user?.is_superuser),
  });
  const permissionsQuery = useQuery({
    queryKey: ["effective-permissions", "TENANT", selectedTenantId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=TENANT&resource_id=${selectedTenantId}`),
    enabled: Boolean(selectedTenantId),
  });

  if (userQuery.error || tenantQuery.error || permissionsQuery.error) {
    return (
      <main id="main-content" className="grid min-h-screen place-items-center p-6">
        <div className="max-w-md rounded-xl border bg-card p-6 text-center">
          <h1 className="text-xl font-semibold">Workspace unavailable</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">Refresh the page or sign in again.</p>
          <a href="/login" className="mt-5 inline-flex font-semibold text-primary underline-offset-4 hover:underline">Return to sign in</a>
        </div>
      </main>
    );
  }

  if (userQuery.isPending || userQuery.isFetching || !user || routeTenantPending || tenantQuery.isPending || !tenantQuery.data || permissionsQuery.isPending) {
    return (
      <div className="grid min-h-screen place-items-center bg-background">
        <div className="flex items-center gap-3 text-sm font-medium text-muted-foreground">
          <span className="size-2 animate-pulse rounded-full bg-accent" />
          Opening workspace…
        </div>
      </div>
    );
  }

  return (
    <WorkspaceContext.Provider
      value={{
        user,
        tenant: tenantQuery.data,
        tenants: user.is_superuser ? (tenantsQuery.data ?? [tenantQuery.data]) : [tenantQuery.data],
        permissionKeys: permissionsQuery.data?.permission_keys ?? [],
        selectedTenantId,
        setSelectedTenantId: setSelection,
        tenantFromRoute: Boolean(routeTenantId),
      }}
    >
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace() {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return context;
}
