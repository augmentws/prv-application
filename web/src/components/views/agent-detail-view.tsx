"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, LoaderCircle, Save } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { AgentVersionFields } from "@/components/agent-version-fields";
import { HelpLink } from "@/components/help-link";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { useWorkspace } from "@/components/workspace-context";
import type {
  AgentDefinitionCreated,
  AgentDefinitionRead,
  AgentDefinitionUpdate,
  AgentDefinitionVersionRead,
  AgentModelRead,
  AgentToolRead,
  AgentVersionCreate,
} from "@/generated/models";
import { agentVersionFormSchema, valuesFromVersion, versionPayload, type AgentVersionFormValues } from "@/lib/agent-forms";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const propertiesSchema = z.object({
  name: z.string().trim().min(2).max(200),
  description: z.string().trim().max(4000),
  status: z.enum(["ACTIVE", "SUSPENDED", "ARCHIVED"]),
});
type PropertiesValues = z.infer<typeof propertiesSchema>;

function AgentPropertiesForm({ agent, onSave }: { agent: AgentDefinitionRead; onSave: (payload: AgentDefinitionUpdate) => Promise<void> }) {
  const { register, control, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<PropertiesValues>({
    resolver: zodResolver(propertiesSchema),
    defaultValues: { name: agent.name, description: agent.description ?? "", status: agent.status },
  });
  async function submit(values: PropertiesValues) {
    try {
      await onSave({ name: values.name, description: values.description || null, status: values.status });
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Agent settings could not be saved." });
    }
  }
  return (
    <form className="space-y-4" onSubmit={handleSubmit(submit)}>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2"><Label htmlFor="agent-detail-name">Name</Label><Input id="agent-detail-name" {...register("name")} /></div>
        <div className="space-y-2"><Label>Lifecycle status</Label><Controller control={control} name="status" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ACTIVE">Active</SelectItem><SelectItem value="SUSPENDED">Suspended</SelectItem><SelectItem value="ARCHIVED">Archived</SelectItem></SelectContent></Select>} /></div>
      </div>
      <div className="space-y-2"><Label htmlFor="agent-detail-description">Description</Label><Textarea id="agent-detail-description" {...register("description")} /></div>
      {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
      <div className="flex justify-end"><Button type="submit" variant="outline" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : <Save />}Save settings</Button></div>
    </form>
  );
}

function NewAgentVersionForm({
  current,
  models,
  tools,
  onCreate,
}: {
  current: AgentDefinitionVersionRead;
  models: AgentModelRead[];
  tools: AgentToolRead[];
  onCreate: (payload: AgentVersionCreate) => Promise<void>;
}) {
  const { register, control, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<AgentVersionFormValues>({
    resolver: zodResolver(agentVersionFormSchema),
    defaultValues: valuesFromVersion(current),
  });
  async function submit(values: AgentVersionFormValues) {
    try {
      await onCreate(versionPayload(values));
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Agent version could not be created." });
    }
  }
  return (
    <form className="space-y-6" onSubmit={handleSubmit(submit)}>
      <AgentVersionFields register={register} control={control} errors={errors} models={models} tools={tools} />
      {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
      <div className="flex justify-end"><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create new draft version</Button></div>
    </form>
  );
}

function PublishVersionDialog({ version, publishing, onPublish }: { version: number; publishing: boolean; onPublish: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  async function publish() {
    try {
      await onPublish();
      setOpen(false);
    } catch {
      // The mutation reports the API error while the dialog remains open for retry.
    }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button disabled={publishing}><CheckCircle2 />Publish v{version}</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Publish agent version {version}?</DialogTitle><DialogDescription>This makes the immutable prompt and tool assignments available to new matter conversations. Existing conversations remain pinned to their original version.</DialogDescription></DialogHeader>
        <DialogFooter><Button variant="outline" type="button" onClick={() => setOpen(false)}>Cancel</Button><Button type="button" disabled={publishing} onClick={() => void publish()}>{publishing ? <LoaderCircle className="animate-spin" /> : null}Publish version</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function AgentDetailView({ agentId }: { agentId: string }) {
  const { user } = useWorkspace();
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ["agent", agentId], queryFn: () => coreApi<AgentDefinitionCreated>(`/v1/agents/${agentId}`), enabled: user.is_superuser });
  const versions = useQuery({ queryKey: ["agent-versions", agentId], queryFn: () => coreApi<AgentDefinitionVersionRead[]>(`/v1/agents/${agentId}/versions`), enabled: user.is_superuser });
  const tools = useQuery({ queryKey: ["agent-tools"], queryFn: () => coreApi<AgentToolRead[]>("/v1/agent-tools"), enabled: user.is_superuser });
  const models = useQuery({ queryKey: ["agent-models"], queryFn: () => coreApi<AgentModelRead[]>("/v1/agent-models"), enabled: user.is_superuser });
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["agent", agentId] }),
      queryClient.invalidateQueries({ queryKey: ["agent-versions", agentId] }),
      queryClient.invalidateQueries({ queryKey: ["system-agents"] }),
    ]);
  };
  const update = useMutation({
    mutationFn: (payload: AgentDefinitionUpdate) => coreApi<AgentDefinitionRead>(`/v1/agents/${agentId}`, { method: "PATCH", body: JSON.stringify(payload) }),
    onSuccess: async () => { await refresh(); toast.success("Agent settings were saved."); },
  });
  const createVersion = useMutation({
    mutationFn: (payload: AgentVersionCreate) => coreApi<AgentDefinitionVersionRead>(`/v1/agents/${agentId}/versions`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: async (version) => { await refresh(); toast.success(`Version ${version.version} draft was created.`); },
  });
  const publish = useMutation({
    mutationFn: (version: number) => coreApi<AgentDefinitionVersionRead>(`/v1/agents/${agentId}/versions/${version}/publish`, { method: "POST" }),
    onSuccess: async (version) => { await refresh(); toast.success(`Version ${version.version} was published.`); },
    onError: (error) => toast.error(error.message),
  });

  if (!user.is_superuser) return <QueryError message="Root administrator access is required." />;
  const error = detail.error ?? versions.error ?? tools.error ?? models.error;
  if (detail.isPending || versions.isPending || tools.isPending || models.isPending) return <TableLoading />;
  if (error || !detail.data) return <QueryError message={error?.message ?? "Agent could not be loaded."} />;
  const { agent, version: current } = detail.data;

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex items-center gap-1 text-sm text-muted-foreground"><Link href="/admin/agents" className="hover:text-foreground">Agents</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{agent.name}</span></nav>
      <PageHeader eyebrow="System agent" title={agent.name} description={agent.description ?? "No description provided."} actions={<><HelpLink topic="agents" />{current.status === "DRAFT" && agent.status === "ACTIVE" ? <PublishVersionDialog version={current.version} publishing={publish.isPending} onPublish={() => publish.mutateAsync(current.version).then(() => undefined)} /> : null}</>} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="space-y-6">
          <Card><CardHeader><CardTitle>Agent settings</CardTitle><CardDescription>The key <code>{agent.key}</code> and system scope are permanent.</CardDescription></CardHeader><CardContent><AgentPropertiesForm key={agent.updated_at} agent={agent} onSave={(payload) => update.mutateAsync(payload).then(() => undefined)} /></CardContent></Card>

          <Card>
            <CardHeader><CardTitle>Create version {agent.current_version + 1}</CardTitle><CardDescription>Each save creates a new immutable draft copied from the current version. Publishing is a separate action.</CardDescription></CardHeader>
            <CardContent>{agent.status === "ACTIVE" ? <NewAgentVersionForm key={current.id} current={current} models={models.data ?? []} tools={tools.data ?? []} onCreate={(payload) => createVersion.mutateAsync(payload).then(() => undefined)} /> : <p className="rounded-lg border bg-muted/30 p-4 text-sm text-muted-foreground">Reactivate this agent before creating another version.</p>}</CardContent>
          </Card>
        </div>

        <aside className="space-y-6">
          <Card><CardHeader><CardTitle>Current state</CardTitle></CardHeader><CardContent className="space-y-3 text-sm"><div className="flex items-center justify-between"><span className="text-muted-foreground">Status</span><StatusBadge status={agent.status} /></div><div className="flex items-center justify-between"><span className="text-muted-foreground">Current</span><span className="font-semibold">v{agent.current_version}</span></div><div className="flex items-center justify-between"><span className="text-muted-foreground">Published</span><span className="font-semibold">{agent.published_version ? `v${agent.published_version}` : "None"}</span></div><div className="flex items-center justify-between"><span className="text-muted-foreground">Model</span><span className="font-mono text-xs">{current.model_key}</span></div></CardContent></Card>

          <Card><CardHeader><CardTitle>Version history</CardTitle><CardDescription>Conversations remain pinned to the version they started with.</CardDescription></CardHeader><CardContent className="space-y-3">{(versions.data ?? []).map((item) => <div key={item.id} className="rounded-lg border p-3"><div className="flex items-center justify-between gap-2"><span className="font-semibold">Version {item.version}</span><StatusBadge status={item.status} /></div><p className="mt-2 text-xs text-muted-foreground">{formatDate(item.created_at)}</p><div className="mt-2 flex flex-wrap gap-1.5">{item.tools.map((tool) => <Badge key={tool.key} variant="outline">{tool.key.split(".").at(-1)?.replaceAll("_", " ")}</Badge>)}</div></div>)}</CardContent></Card>
        </aside>
      </div>
    </>
  );
}
