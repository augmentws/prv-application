"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useState, type ReactNode } from "react";

import type { TenantRead, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

interface WorkspaceValue {
  user: UserRead;
  tenant: TenantRead;
  tenants: TenantRead[];
  selectedTenantId: string;
  setSelectedTenantId: (tenantId: string) => void;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [selection, setSelection] = useState<string | null>(null);
  const userQuery = useQuery({
    queryKey: ["session"],
    queryFn: () => coreApi<UserRead>("/v1/auth/me"),
    staleTime: 0,
  });
  const user = userQuery.data;
  const selectedTenantId = selection ?? user?.tenant_id ?? "";

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

  if (userQuery.error || tenantQuery.error) {
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

  if (userQuery.isPending || userQuery.isFetching || !user || tenantQuery.isPending || !tenantQuery.data) {
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
        selectedTenantId,
        setSelectedTenantId: setSelection,
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
