"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { CreateAgentForm } from "@/components/forms/create-agent-form";
import { HelpLink } from "@/components/help-link";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { useWorkspace } from "@/components/workspace-context";
import type { AgentDefinitionCreate, AgentDefinitionCreated, AgentModelRead, AgentToolRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

export function CreateSystemAgentView() {
  const { user } = useWorkspace();
  const router = useRouter();
  const queryClient = useQueryClient();
  const tools = useQuery({ queryKey: ["agent-tools"], queryFn: () => coreApi<AgentToolRead[]>("/v1/agent-tools"), enabled: user.is_superuser });
  const models = useQuery({ queryKey: ["agent-models"], queryFn: () => coreApi<AgentModelRead[]>("/v1/agent-models"), enabled: user.is_superuser });
  const create = useMutation({
    mutationFn: (payload: AgentDefinitionCreate) => coreApi<AgentDefinitionCreated>("/v1/admin/agents", { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: ({ agent }) => {
      void queryClient.invalidateQueries({ queryKey: ["system-agents"] });
      toast.success(`${agent.name} draft was created.`);
      router.push(`/admin/agents/${agent.id}`);
    },
  });

  if (!user.is_superuser) return <QueryError message="Root administrator access is required." />;
  const error = tools.error ?? models.error;
  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex items-center gap-1 text-sm text-muted-foreground"><Link href="/admin/agents" className="hover:text-foreground">Agents</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">New agent</span></nav>
      <PageHeader eyebrow="Root administration" title="Create system agent" description="Published system agents can be bound to compatible workflows implemented by the platform." actions={<HelpLink topic="agents" />} />
      {tools.isPending || models.isPending ? <TableLoading /> : error ? <QueryError message={error.message} /> : (
        <CreateAgentForm models={models.data ?? []} tools={tools.data ?? []} onCreate={(payload) => create.mutateAsync(payload).then(() => undefined)} />
      )}
    </>
  );
}
