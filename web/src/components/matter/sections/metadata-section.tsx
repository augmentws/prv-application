"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useRouter } from "next/navigation";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateMetadataDialog } from "@/components/forms/create-metadata-dialog";
import { type CreateMetadataGroupValues } from "@/components/forms/create-metadata-group-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { subtabClass } from "@/components/matter/sections/subtabs";
import { MetadataDefinitionEditor } from "@/components/metadata-definition-editor";
import { MetadataGroupsPanel } from "@/components/metadata-groups-panel";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import type { MetadataDefinitionCreate, MetadataDefinitionRead, MetadataGroupRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref } from "@/lib/matter-routes";

export type MetadataTab = "definitions" | "groups";

export function MetadataSection({ tab }: { tab: MetadataTab }) {
  const { clientId, matterId } = useMatter();
  const router = useRouter();
  const queryClient = useQueryClient();
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const groups = useQuery({ queryKey: ["metadata-groups", matterId], queryFn: () => coreApi<MetadataGroupRead[]>(`/v1/matters/${matterId}/metadata-groups`), enabled: tab === "groups" });
  const mutation = useMutation({
    mutationFn: (values: MetadataDefinitionCreate) => coreApi<MetadataDefinitionRead>(`/v1/matters/${matterId}/metadata-definitions`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (definition) => { void queryClient.invalidateQueries({ queryKey: ["metadata-definitions", matterId] }); toast.success(`${definition.display_name} was created.`); },
  });
  const createDefinition = useCallback((values: MetadataDefinitionCreate) => mutation.mutateAsync(values).then(() => undefined), [mutation]);
  const groupMutation = useMutation({
    mutationFn: (values: CreateMetadataGroupValues) => coreApi<MetadataGroupRead>(`/v1/matters/${matterId}/metadata-groups`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (group) => { void queryClient.invalidateQueries({ queryKey: ["metadata-groups", matterId] }); toast.success(`${group.display_name} was created.`); },
  });
  const visibilityMutation = useMutation({
    mutationFn: ({ group, surface, visible }: { group: MetadataGroupRead; surface: "TABLE" | "DOCUMENT"; visible: boolean }) => coreApi<MetadataGroupRead>(`/v1/matters/${matterId}/metadata-groups/${group.id}/visibility`, { method: "PUT", body: JSON.stringify({ surface, visible }) }),
    onSuccess: (updated) => queryClient.setQueryData<MetadataGroupRead[]>(["metadata-groups", matterId], (current) => current?.map((group) => group.id === updated.id ? updated : group)),
    onError: (error) => toast.error(error instanceof Error ? error.message : "Visibility could not be changed."),
  });
  const createGroup = useCallback((values: CreateMetadataGroupValues) => groupMutation.mutateAsync(values).then(() => undefined), [groupMutation]);
  const changeVisibility = useCallback((group: MetadataGroupRead, surface: "TABLE" | "DOCUMENT", visible: boolean) => visibilityMutation.mutateAsync({ group, surface, visible }).then(() => undefined), [visibilityMutation]);
  const updateDefinitionCache = useCallback((updated: MetadataDefinitionRead) => {
    queryClient.setQueryData<MetadataDefinitionRead[]>(["metadata-definitions", matterId], (current) => current?.map((definition) => definition.id === updated.id ? updated : definition));
  }, [matterId, queryClient]);
  const columns = useMemo<ColumnDef<MetadataDefinitionRead>[]>(() => [
    { accessorKey: "display_name", header: "Field", cell: ({ row }) => <div className="cursor-help" title={row.original.description ?? "No description has been provided for this field."}><p className="font-semibold">{row.original.display_name}</p><code className="text-xs text-muted-foreground">{row.original.key}</code></div> },
    { accessorKey: "type", header: "Type", cell: ({ row }) => <Badge>{row.original.type.toLowerCase().replace("_", " ")}</Badge> },
    { id: "allowed_values", header: "Enum values", size: 220, cell: ({ row }) => row.original.type === "ENUM" ? <span className="text-muted-foreground">{(row.original.allowed_values ?? []).filter((value) => value.active !== false).length} active · {(row.original.allowed_values ?? []).filter((value) => value.active === false).length} inactive</span> : <span className="text-muted-foreground">—</span> },
    { accessorKey: "value_source", header: "Source", cell: ({ row }) => <Badge variant="outline">{row.original.value_source.toLowerCase()}</Badge> },
    { accessorKey: "cardinality", header: "Values", cell: ({ row }) => <span className="text-muted-foreground">{row.original.cardinality.toLowerCase()}</span> },
    { id: "behavior", header: "Behavior", cell: ({ row }) => <div className="flex flex-wrap gap-1">{row.original.searchable ? <Badge variant="outline">search</Badge> : null}{row.original.facetable ? <Badge variant="outline">filter</Badge> : null}{row.original.normalize_to_lowercase ? <Badge variant="outline">lowercase</Badge> : null}{row.original.hierarchy_separator ? <Badge variant="outline">hierarchy <code>{row.original.hierarchy_separator}</code></Badge> : null}{row.original.ai_assignable ? <Badge variant="accent">agent</Badge> : null}</div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { id: "actions", header: "", size: 100, cell: ({ row }) => row.original.value_source === "ASSERTED" ? <div className="flex justify-end"><MetadataDefinitionEditor matterId={matterId} definition={row.original} onChange={updateDefinitionCache} /></div> : null },
  ], [matterId, updateDefinitionCache]);

  const openTab = (next: MetadataTab) => router.push(matterHref(clientId, matterId, next === "groups" ? "metadata-groups" : "metadata"));

  return <>
    <div className="mb-5 flex items-end justify-between gap-3 border-b">
      <div className="flex" role="tablist" aria-label="Metadata sections">
        <button type="button" role="tab" aria-selected={tab === "definitions"} onClick={() => openTab("definitions")} className={subtabClass(tab === "definitions")}>Definitions</button>
        <button type="button" role="tab" aria-selected={tab === "groups"} onClick={() => openTab("groups")} className={subtabClass(tab === "groups")}>Groups</button>
      </div>
      {tab === "definitions" ? <div className="pb-2"><CreateMetadataDialog onCreate={createDefinition} /></div> : null}
    </div>
    {tab === "definitions"
      ? definitions.isPending ? <TableLoading /> : definitions.error ? <QueryError message={definitions.error.message} /> : <DataTable columns={columns} data={definitions.data} emptyMessage="No metadata fields yet. Add the first field definition for this matter." />
      : definitions.isPending || groups.isPending ? <TableLoading /> : definitions.error || groups.error ? <QueryError message={definitions.error?.message ?? groups.error?.message} /> : <MetadataGroupsPanel definitions={definitions.data} groups={groups.data} onCreate={createGroup} onVisibilityChange={changeVisibility} />}
  </>;
}
