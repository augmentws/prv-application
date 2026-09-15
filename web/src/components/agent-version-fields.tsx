import type { Control, FieldErrors, FieldPath, UseFormRegister } from "react-hook-form";
import { Controller } from "react-hook-form";

import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { AgentModelRead, AgentToolRead } from "@/generated/models";
import type { AgentVersionFormValues } from "@/lib/agent-forms";

export function AgentVersionFields<TValues extends AgentVersionFormValues>({
  register,
  control,
  errors,
  models,
  tools,
}: {
  register: UseFormRegister<TValues>;
  control: Control<TValues>;
  errors: FieldErrors<TValues>;
  models: AgentModelRead[];
  tools: AgentToolRead[];
}) {
  const field = (name: keyof AgentVersionFormValues) => name as FieldPath<TValues>;
  const versionErrors = errors as FieldErrors<AgentVersionFormValues>;
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Label htmlFor="agent-system-prompt">System prompt</Label>
        <Textarea
          id="agent-system-prompt"
          className="min-h-64 font-mono text-sm leading-6"
          placeholder="Define the agent's responsibilities, process, constraints, and when it must ask for approval."
          {...register(field("system_prompt"))}
        />
        {versionErrors.system_prompt ? <p className="text-sm text-destructive">{versionErrors.system_prompt.message}</p> : null}
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <div className="space-y-2 sm:col-span-2">
          <Label>Model policy</Label>
          <Controller
            control={control}
            name={field("model_key")}
            render={({ field }) => (
              <Select value={typeof field.value === "string" ? field.value : ""} onValueChange={field.onChange}>
                <SelectTrigger><SelectValue placeholder="Select a model" /></SelectTrigger>
                <SelectContent>
                  {models.map((model) => (
                    <SelectItem key={model.key} value={model.key}>
                      {model.name}{model.configured_model ? ` · ${model.configured_model}` : " · not configured"}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          />
          <p className="text-xs leading-5 text-muted-foreground">The platform controls provider credentials and the concrete model name.</p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="agent-temperature">Temperature</Label>
          <Input id="agent-temperature" type="number" min="0" max="2" step="0.1" {...register(field("temperature"), { valueAsNumber: true })} />
          {versionErrors.temperature ? <p className="text-sm text-destructive">{versionErrors.temperature.message}</p> : null}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="agent-max-requests">Maximum model requests</Label>
          <Input id="agent-max-requests" type="number" min="1" max="500" {...register(field("max_requests"), { valueAsNumber: true })} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="agent-max-tool-calls">Maximum tool calls</Label>
          <Input id="agent-max-tool-calls" type="number" min="0" max="500" {...register(field("max_tool_calls"), { valueAsNumber: true })} />
        </div>
      </div>

      <fieldset className="space-y-3 rounded-xl border bg-muted/20 p-4">
        <legend className="px-1 text-sm font-semibold">Assigned tools</legend>
        <p className="text-xs leading-5 text-muted-foreground">Only code-owned tools can be assigned. State-changing tools always require a separate user approval.</p>
        <div className="grid gap-3 lg:grid-cols-2">
          {tools.map((tool) => (
            <label key={tool.key} className="flex items-start gap-3 rounded-lg border bg-card p-3 text-sm has-[:disabled]:opacity-55">
              <input
                type="checkbox"
                value={tool.key}
                disabled={!tool.runtime_available}
                className="mt-0.5 size-4 rounded border-input accent-primary"
                {...register(field("tools"))}
              />
              <span className="min-w-0 flex-1">
                <span className="flex flex-wrap items-center gap-2 font-semibold">
                  {tool.name}
                  {tool.requires_approval ? <Badge variant="accent">approval</Badge> : null}
                  {!tool.runtime_available ? <Badge variant="outline">planned</Badge> : null}
                </span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">{tool.description}</span>
                <code className="mt-1 block break-all text-[11px] text-muted-foreground">{tool.key}</code>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

      <div className="space-y-2">
        <Label htmlFor="agent-output-schema">Output schema</Label>
        <Textarea id="agent-output-schema" className="min-h-28 font-mono text-xs leading-5" {...register(field("output_schema"))} />
        <p className="text-xs leading-5 text-muted-foreground">JSON Schema describing the agent’s final output. Use <code>{'{ "type": "string" }'}</code> for conversational text.</p>
        {versionErrors.output_schema ? <p className="text-sm text-destructive">{versionErrors.output_schema.message}</p> : null}
      </div>
    </div>
  );
}
