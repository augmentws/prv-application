"use client";

import { useEffect } from "react";

interface ToolDefinition {
  name: string;
  title: string;
  description: string;
  inputSchema: Record<string, unknown>;
  annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
  execute: (input: unknown) => unknown | Promise<unknown>;
}

declare global {
  interface Document {
    modelContext?: {
      registerTool(tool: ToolDefinition, options?: { signal?: AbortSignal }): void | Promise<void>;
    };
  }
}

export function useWebMcpTool(tool: ToolDefinition) {
  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(context.registerTool(tool, { signal: lifecycle.signal })).catch(() => undefined);
    return () => lifecycle.abort();
  }, [tool]);
}
