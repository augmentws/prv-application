"use client";

import { useQuery } from "@tanstack/react-query";
import { Braces, Network, Wrench } from "lucide-react";
import { useState } from "react";

import { PageHeader } from "@/components/page-header";
import { HelpLink } from "@/components/help-link";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useWorkspace } from "@/components/workspace-context";
import type { SkillDefinitionCreated, SkillDefinitionRead, SkillDefinitionVersionRead, WorkflowSkillBindingRead, WorkflowSpecRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

export function SystemSkillsView() {
  const { user } = useWorkspace();
  const [selectedId, setSelectedId] = useState("");
  const skills = useQuery({ queryKey: ["system-skills"], queryFn: () => coreApi<SkillDefinitionRead[]>("/v1/admin/skills"), enabled: user.is_superuser });
  const workflows = useQuery({ queryKey: ["workflow-specs"], queryFn: () => coreApi<WorkflowSpecRead[]>("/v1/workflow-specs"), enabled: user.is_superuser });
  const bindings = useQuery({ queryKey: ["system-workflow-bindings"], queryFn: () => coreApi<WorkflowSkillBindingRead[]>("/v1/admin/workflow-skill-bindings"), enabled: user.is_superuser });
  const effectiveId = selectedId || skills.data?.[0]?.id || "";
  const detail = useQuery({ queryKey: ["skill", effectiveId], queryFn: () => coreApi<SkillDefinitionCreated>(`/v1/skills/${effectiveId}`), enabled: Boolean(effectiveId) });
  const versions = useQuery({ queryKey: ["skill-versions", effectiveId], queryFn: () => coreApi<SkillDefinitionVersionRead[]>(`/v1/skills/${effectiveId}/versions`), enabled: Boolean(effectiveId) });

  if (!user.is_superuser) return <QueryError message="Root administrator access is required." />;
  const error = skills.error ?? workflows.error ?? bindings.error ?? detail.error ?? versions.error;
  return <>
    <PageHeader eyebrow="Root administration" title="Skills & workflow bindings" description="Inspect managed skill contracts and the code-owned workflow roles that may invoke them. Binding changes affect future runs only." actions={<HelpLink topic="skills" />} />
    {skills.isPending || workflows.isPending || bindings.isPending ? <TableLoading /> : error ? <QueryError message={error.message} /> : <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(24rem,0.8fr)]">
      <section className="space-y-4">
        <Card className="p-5"><div className="mb-4 flex items-center gap-2"><Wrench className="size-4 text-primary" /><h2 className="font-semibold">Managed skills</h2></div>{skills.data?.length ? <Select value={effectiveId} onValueChange={setSelectedId}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{skills.data.map((skill) => <SelectItem key={skill.id} value={skill.id}>{skill.name} · {skill.key}</SelectItem>)}</SelectContent></Select> : <p className="text-sm text-muted-foreground">No system skills are registered.</p>}</Card>
        {detail.data ? <Card className="overflow-hidden"><div className="border-b p-5"><div className="flex flex-wrap items-center justify-between gap-2"><div><h2 className="font-semibold">{detail.data.skill.name}</h2><p className="font-mono text-xs text-muted-foreground">{detail.data.skill.key}</p></div><StatusBadge status={detail.data.skill.status} /></div><p className="mt-2 text-sm text-muted-foreground">{detail.data.skill.description}</p></div><div className="grid gap-4 p-5 sm:grid-cols-2"><Contract label="Input schema" value={detail.data.version.input_schema_key} body={detail.data.version.input_schema} /><Contract label="Output schema" value={detail.data.version.output_schema_key} body={detail.data.version.output_schema} /><Contract label="Model policy" value={detail.data.version.model_key} body={detail.data.version.model_policy} /><Contract label="Cache policy" value={`v${detail.data.version.version}`} body={detail.data.version.cache_policy} /></div><div className="border-t p-5"><h3 className="text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Version history</h3><div className="mt-2 flex flex-wrap gap-2">{versions.data?.map((version) => <Badge key={version.id} variant={version.status === "PUBLISHED" ? "active" : "outline"}>v{version.version} · {version.status.toLowerCase()} · {version.evaluation_fixtures.length} evals</Badge>)}</div></div></Card> : null}
      </section>
      <section className="space-y-4"><Card className="p-5"><div className="mb-4 flex items-center gap-2"><Network className="size-4 text-primary" /><h2 className="font-semibold">Code-owned workflows</h2></div><div className="space-y-4">{workflows.data?.map((workflow) => <div key={workflow.key} className="rounded-lg border p-3"><div className="flex items-center justify-between gap-2"><p className="font-semibold">{workflow.name}</p><Badge variant="outline">code v{workflow.code_version}</Badge></div><p className="mt-1 text-xs text-muted-foreground">{workflow.description}</p><div className="mt-3 space-y-2">{workflow.roles.map((role) => { const binding = bindings.data?.find((item) => item.workflow_key === workflow.key && item.role_key === role.key); const skill = skills.data?.find((item) => item.id === binding?.skill_definition_id); return <div key={role.key} className="rounded-md bg-muted/35 p-2 text-xs"><div className="flex items-center justify-between gap-2"><strong>{role.key.replaceAll("_", " ")}</strong>{binding ? <StatusBadge status={binding.status} /> : <Badge variant="outline">unbound</Badge>}</div><p className="mt-1 text-muted-foreground">{skill?.name ?? "No skill"}{skill && binding ? ` · pinned version ${skill.published_version ?? "—"}` : ""}</p></div>; })}</div></div>)}</div></Card></section>
    </div>}
  </>;
}

function Contract({ label, value, body }: { label: string; value: string; body: Record<string, unknown> }) {
  return <div className="min-w-0 rounded-lg border"><div className="flex items-center gap-2 border-b px-3 py-2"><Braces className="size-3.5 text-primary" /><div><p className="text-xs font-semibold">{label}</p><p className="break-all font-mono text-[10px] text-muted-foreground">{value}</p></div></div><pre className="max-h-48 overflow-auto whitespace-pre-wrap p-3 text-[10px] leading-4">{JSON.stringify(body, null, 2)}</pre></div>;
}
