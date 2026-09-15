"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LoaderCircle } from "lucide-react";
import { useForm } from "react-hook-form";

import { AgentVersionFields } from "@/components/agent-version-fields";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { AgentDefinitionCreate, AgentModelRead, AgentToolRead } from "@/generated/models";
import {
  agentDefinitionFormSchema,
  defaultAgentVersionValues,
  versionPayload,
  type AgentDefinitionFormValues,
} from "@/lib/agent-forms";

export function CreateAgentForm({
  models,
  tools,
  onCreate,
}: {
  models: AgentModelRead[];
  tools: AgentToolRead[];
  onCreate: (payload: AgentDefinitionCreate) => Promise<void>;
}) {
  const { register, control, handleSubmit, setError, formState: { errors, isSubmitting } } = useForm<AgentDefinitionFormValues>({
    resolver: zodResolver(agentDefinitionFormSchema),
    defaultValues: { key: "", name: "", description: "", ...defaultAgentVersionValues },
  });

  async function submit(values: AgentDefinitionFormValues) {
    try {
      await onCreate({
        key: values.key,
        name: values.name,
        description: values.description || null,
        initial_version: versionPayload(values),
      });
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Agent could not be created." });
    }
  }

  return (
    <form className="space-y-6" onSubmit={handleSubmit(submit)}>
      <Card>
        <CardHeader><CardTitle>Identity</CardTitle><CardDescription>The stable key cannot be changed after creation.</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="agent-name">Name</Label><Input id="agent-name" autoFocus placeholder="Matter Definition Setup" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="agent-key">Stable key</Label><Input id="agent-key" placeholder="matter_definition_setup" {...register("key")} />{errors.key ? <p className="text-sm text-destructive">{errors.key.message}</p> : null}</div>
          </div>
          <div className="space-y-2"><Label htmlFor="agent-description">Description</Label><Textarea id="agent-description" placeholder="Explain where this agent is used and what it accomplishes." {...register("description")} /></div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Initial version</CardTitle><CardDescription>Creation saves version 1 as a draft. A root administrator must publish it before matters can use it.</CardDescription></CardHeader>
        <CardContent><AgentVersionFields register={register} control={control} errors={errors} models={models} tools={tools} /></CardContent>
      </Card>

      {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
      <div className="flex justify-end"><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create draft agent</Button></div>
    </form>
  );
}
