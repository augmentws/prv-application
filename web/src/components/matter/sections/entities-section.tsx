"use client";

import { useRouter } from "next/navigation";

import { EntitiesPanel, type EntitiesTab } from "@/components/entities/entities-panel";
import { useMatter } from "@/components/matter/matter-context";
import { subtabClass } from "@/components/matter/sections/subtabs";
import { QueryError, TableLoading } from "@/components/query-state";
import { matterHref } from "@/lib/matter-routes";

export function EntitiesSection({ tab }: { tab: EntitiesTab }) {
  const { clientId, matterId, permissionKeys, permissions } = useMatter();
  const router = useRouter();

  if (permissions.isPending) return <TableLoading />;
  if (permissions.error) return <QueryError message={permissions.error.message} />;
  // UI visibility only; the API enforces entity permissions on every request.
  if (!permissionKeys.has("entity.view")) return <QueryError message="You do not have permission to view entities in this matter." />;

  const openTab = (next: EntitiesTab) => router.push(matterHref(clientId, matterId, next === "proposed" ? "entities-proposed" : "entities"));

  return <>
    <div className="mb-5 flex border-b" role="tablist" aria-label="Entity sections">
      <button type="button" role="tab" aria-selected={tab === "directory"} onClick={() => openTab("directory")} className={subtabClass(tab === "directory")}>Directory</button>
      <button type="button" role="tab" aria-selected={tab === "proposed"} onClick={() => openTab("proposed")} className={subtabClass(tab === "proposed")}>Proposed</button>
    </div>
    <EntitiesPanel key={tab} clientId={clientId} matterId={matterId} tab={tab} canManage={permissionKeys.has("entity.manage")} />
  </>;
}
