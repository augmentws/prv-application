"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";

import { matterNavHref, matterNavTarget, visibleMatterNav } from "@/components/matter/matter-sections";
import { ResourceSidebarNav } from "@/components/resource-nav/resource-sidebar-nav";
import type { ClientRead, EffectivePermissionsRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref, matterSectionFromPath } from "@/lib/matter-routes";

/** The matter's own menu, shown in the app sidebar in place of the workspace links while a matter is open. */
export function MatterSidebarNav({ clientId, matterId, collapsed, onNavigate }: {
  clientId: string;
  matterId: string;
  collapsed: boolean;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  // Same keys and requests as the matter pages, so these responses are shared with them.
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const matters = useQuery({ queryKey: ["matters", clientId], queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const section = matterSectionFromPath(pathname) ?? "overview";
  const activeTarget = matterNavTarget(section);

  function switchMatter(nextMatterId: string) {
    onNavigate();
    // Detail pages map to their section, so switching never points at another matter's record.
    router.push(matterHref(clientId, nextMatterId, activeTarget));
  }

  return (
    <ResourceSidebarNav
      label="Matter"
      pluralLabel="matters"
      client={{ id: clientId, name: client.data?.name }}
      current={{ id: matterId, name: matter.data?.name }}
      siblings={matters}
      groups={visibleMatterNav(permissionKeys)}
      activeTarget={activeTarget}
      hrefFor={(target) => matterNavHref(clientId, matterId, target)}
      onSwitch={switchMatter}
      permissionsError={permissions.error}
      collapsed={collapsed}
      onNavigate={onNavigate}
    />
  );
}
