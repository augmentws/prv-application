import { z } from "zod";

import type { AgentDefinitionVersionRead, AgentVersionCreate } from "@/generated/models";

function jsonObject(value: string, context: z.RefinementCtx, path: string) {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      context.addIssue({ code: "custom", path: [path], message: "Enter a JSON object." });
    }
  } catch {
    context.addIssue({ code: "custom", path: [path], message: "Enter valid JSON." });
  }
}

const agentVersionFormShape = {
  system_prompt: z.string().trim().min(20, "Describe the agent's responsibilities and boundaries.").max(100_000),
  model_key: z.string().min(1, "Select a model."),
  temperature: z.number().min(0).max(2),
  max_requests: z.number().int().min(1).max(500),
  max_tool_calls: z.number().int().min(0).max(500),
  output_schema: z.string().trim().min(2),
  tools: z.array(z.string()),
};

export const agentVersionFormSchema = z.object(agentVersionFormShape).superRefine((values, context) => jsonObject(values.output_schema, context, "output_schema"));

export type AgentVersionFormValues = z.infer<typeof agentVersionFormSchema>;

export const agentDefinitionFormSchema = z.object({
  key: z.string().trim().min(1).max(100).regex(/^[a-z][a-z0-9_]*$/, "Use lowercase letters, numbers, and underscores."),
  name: z.string().trim().min(2, "Enter an agent name.").max(200),
  description: z.string().trim().max(4000).optional(),
  ...agentVersionFormShape,
}).superRefine((values, context) => jsonObject(values.output_schema, context, "output_schema"));

export type AgentDefinitionFormValues = z.infer<typeof agentDefinitionFormSchema>;

export const defaultAgentVersionValues: AgentVersionFormValues = {
  system_prompt: "",
  model_key: "configured-default",
  temperature: 0,
  max_requests: 30,
  max_tool_calls: 20,
  output_schema: JSON.stringify({ type: "string" }, null, 2),
  tools: [],
};

function numberSetting(source: Record<string, unknown>, key: string, fallback: number) {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function valuesFromVersion(version: AgentDefinitionVersionRead): AgentVersionFormValues {
  return {
    system_prompt: version.system_prompt,
    model_key: version.model_key,
    temperature: numberSetting(version.model_policy, "temperature", 0),
    max_requests: numberSetting(version.limits, "max_requests", 30),
    max_tool_calls: numberSetting(version.limits, "max_tool_calls", 20),
    output_schema: JSON.stringify(version.output_schema, null, 2),
    tools: version.tools.map((tool) => tool.key),
  };
}

export function versionPayload(values: AgentVersionFormValues): AgentVersionCreate {
  return {
    system_prompt: values.system_prompt,
    model_key: values.model_key,
    model_policy: { temperature: values.temperature },
    output_schema: JSON.parse(values.output_schema) as Record<string, unknown>,
    limits: {
      max_requests: values.max_requests,
      max_tool_calls: values.max_tool_calls,
    },
    tools: values.tools.map((key) => ({ key, configuration: {} })),
  };
}
