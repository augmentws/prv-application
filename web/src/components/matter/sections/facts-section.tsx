"use client";

import { useRouter, useSearchParams } from "next/navigation";

import { FactsListPanel } from "@/components/facts/facts-list-panel";
import { FactsTimelinePanel } from "@/components/facts/facts-timeline-panel";
import { useMatter } from "@/components/matter/matter-context";
import { subtabClass } from "@/components/matter/sections/subtabs";
import { QueryError, TableLoading } from "@/components/query-state";
import { type FactsTab, factsFilterParams } from "@/lib/facts";
import { matterHref } from "@/lib/matter-routes";

export function FactsSection({ tab }: { tab: FactsTab }) {
  const { clientId, matterId, permissionKeys, permissions } = useMatter();
  const router = useRouter();
  const searchParams = useSearchParams();

  if (permissions.isPending) return <TableLoading />;
  if (permissions.error) return <QueryError message={permissions.error.message} />;
  // UI visibility only; the API enforces fact permissions on every request.
  if (!permissionKeys.has("fact.view")) return <QueryError message="You do not have permission to view facts in this matter." />;
  const canExport = permissionKeys.has("fact.export");

  // The List and Timeline share their filters, so switching keeps them.
  const openTab = (next: FactsTab) => router.push(matterHref(clientId, matterId, next === "timeline" ? "facts-timeline" : "facts", factsFilterParams(new URLSearchParams(searchParams.toString()))));

  return <>
    <div className="mb-5 flex border-b" role="tablist" aria-label="Fact sections">
      <button type="button" role="tab" aria-selected={tab === "list"} onClick={() => openTab("list")} className={subtabClass(tab === "list")}>List</button>
      <button type="button" role="tab" aria-selected={tab === "timeline"} onClick={() => openTab("timeline")} className={subtabClass(tab === "timeline")}>Timeline</button>
    </div>
    {tab === "timeline"
      ? <FactsTimelinePanel clientId={clientId} matterId={matterId} canExport={canExport} />
      : <FactsListPanel clientId={clientId} matterId={matterId} canReview={permissionKeys.has("fact.review")} canExport={canExport} />}
  </>;
}
