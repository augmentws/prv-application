"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";

import { collectionNavHref, visibleCollectionNav } from "@/components/collection/collection-sections";
import { ResourceSidebarNav } from "@/components/resource-nav/resource-sidebar-nav";
import type { ClientRead, CollectionRead, EffectivePermissionsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { collectionHref, collectionSectionFromPath } from "@/lib/collection-routes";

/** The collection's own menu, shown in the app sidebar in place of the workspace links while a collection is open. */
export function CollectionSidebarNav({ clientId, collectionId, collapsed, onNavigate }: {
  clientId: string;
  collectionId: string;
  collapsed: boolean;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  // Same keys and requests as the collection pages and the client page, so these responses are shared.
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const collection = useQuery({ queryKey: ["collection", collectionId], queryFn: () => coreApi<CollectionRead>(`/v1/collections/${collectionId}`) });
  const tenantId = client.data?.tenant_id;
  const collections = useQuery({
    queryKey: ["collections", tenantId, clientId],
    queryFn: () => coreApi<CollectionRead[]>(`/v1/tenants/${tenantId}/clients/${clientId}/collections`),
    enabled: Boolean(tenantId),
  });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "COLLECTION", collectionId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=COLLECTION&resource_id=${collectionId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const activeTarget = collectionSectionFromPath(pathname.replace(/\/$/, "")) ?? "documents";

  function switchCollection(nextCollectionId: string) {
    onNavigate();
    // Keep the section; Documents filters are left behind because their IDs belong to this collection.
    router.push(collectionHref(clientId, nextCollectionId, activeTarget));
  }

  return (
    <ResourceSidebarNav
      label="Collection"
      pluralLabel="collections"
      client={{ id: clientId, name: client.data?.name }}
      current={{ id: collectionId, name: collection.data?.name }}
      siblings={{
        data: collections.data,
        // The list waits for the client's tenant; until then it is still loading.
        isPending: !client.error && collections.isPending,
        error: client.error ?? collections.error,
      }}
      groups={visibleCollectionNav(permissionKeys)}
      activeTarget={activeTarget}
      hrefFor={(target) => collectionNavHref(clientId, collectionId, target)}
      onSwitch={switchCollection}
      permissionsError={permissions.error}
      collapsed={collapsed}
      onNavigate={onNavigate}
    />
  );
}
