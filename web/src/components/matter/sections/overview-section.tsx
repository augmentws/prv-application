"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, FileText, type LucideIcon, Search, Sparkles, UsersRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { toast } from "sonner";

import { CloneMatterDialog, type CloneMatterValues } from "@/components/forms/clone-matter-dialog";
import { SaveMatterTemplateDialog, type SaveMatterTemplateValues } from "@/components/forms/save-matter-template-dialog";
import { useMatter } from "@/components/matter/matter-context";
import { QueryError } from "@/components/query-state";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { MatterOverviewCounts, MatterRead, MatterTemplateRead, MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref } from "@/lib/matter-routes";

export function OverviewSection() {
  const { clientId, matterId, matter } = useMatter();
  const router = useRouter();
  const queryClient = useQueryClient();
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const overviewCounts = useQuery({
    queryKey: ["matter-overview-counts", matterId],
    queryFn: () => coreApi<MatterOverviewCounts>(`/v1/matters/${matterId}/overview-counts`),
  });
  const templateMutation = useMutation({
    mutationFn: (values: SaveMatterTemplateValues) => coreApi<MatterTemplateRead>(`/v1/matters/${matterId}/templates`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (template) => { void queryClient.invalidateQueries({ queryKey: ["matter-templates", clientId] }); toast.success(`${template.name} is ready to use.`); },
  });
  const cloneMutation = useMutation({
    mutationFn: (values: CloneMatterValues) => coreApi<MatterRead>(`/v1/clients/${clientId}/matters`, { method: "POST", body: JSON.stringify({ name: values.name, clone_from_matter_id: matterId }) }),
    onSuccess: (created) => { void queryClient.invalidateQueries({ queryKey: ["matters", clientId] }); toast.success(`${created.name} was created.`); router.push(matterHref(clientId, created.id)); },
  });
  const saveTemplate = useCallback((values: SaveMatterTemplateValues) => templateMutation.mutateAsync(values).then(() => undefined), [templateMutation]);
  const cloneMatter = useCallback((values: CloneMatterValues) => cloneMutation.mutateAsync(values).then(() => undefined), [cloneMutation]);

  return <>
    <div className="mb-5 flex items-center justify-end gap-2 border-b pb-2" aria-label="Overview actions">
      <CloneMatterDialog sourceName={matter.name} onClone={cloneMatter} />
      <SaveMatterTemplateDialog onCreate={saveTemplate} />
    </div>
    {overviewCounts.error ? <QueryError message={overviewCounts.error.message} /> : <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
      <Summary icon={FileText} label="Documents" value={overviewCounts.data?.document_count ?? "—"} />
      <Summary icon={UsersRound} label="Custodians" value={overviewCounts.data?.custodian_count ?? "—"} />
      <Summary icon={Database} label="Metadata fields" value={definitions.data?.length ?? 0} />
      <Summary icon={Search} label="Searchable fields" value={definitions.data?.filter((item) => item.searchable).length ?? 0} />
      <Summary icon={Sparkles} label="Agent assignable" value={definitions.data?.filter((item) => item.ai_assignable).length ?? 0} />
    </div>}
  </>;
}

function Summary({ icon: Icon, label, value }: { icon: LucideIcon; label: string; value: number | string }) {
  return <Card><CardHeader><span className="mb-2 grid size-9 place-items-center rounded-lg bg-secondary text-primary"><Icon className="size-4" /></span><CardTitle className="text-sm text-muted-foreground">{label}</CardTitle></CardHeader><CardContent><p className="text-3xl font-semibold tabular-nums">{value}</p></CardContent></Card>;
}
