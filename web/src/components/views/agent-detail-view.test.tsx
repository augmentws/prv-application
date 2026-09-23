import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AgentDetailView } from "@/components/views/agent-detail-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));
vi.mock("@/components/workspace-context", () => ({
  useWorkspace: () => ({ user: { is_superuser: true } }),
}));

const agent = {
  id: "agent-1",
  key: "test_agent",
  name: "Test Agent",
  description: "Tests version selection.",
  status: "ACTIVE",
  current_version: 2,
  published_version: 1,
  updated_at: "2026-09-22T12:00:00Z",
};

const currentVersion = {
  id: "version-2",
  version: 2,
  status: "DRAFT",
  system_prompt: "Current version prompt with enough detail.",
  model_key: "configured-default",
  model_policy: { temperature: 0 },
  limits: { max_requests: 20, max_tool_calls: 10 },
  output_schema: { type: "string" },
  tools: [],
  created_at: "2026-09-22T12:00:00Z",
};

const historicalVersion = {
  ...currentVersion,
  id: "version-1",
  version: 1,
  status: "PUBLISHED",
  system_prompt: "Historical version prompt with enough detail.",
  created_at: "2026-09-21T12:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentDetailView version draft source", () => {
  it("loads a historical version into the draft and lets Current reset it", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/agents/agent-1") return { agent, version: currentVersion } as never;
      if (path === "/v1/agents/agent-1/versions") return [currentVersion, historicalVersion] as never;
      if (path === "/v1/agent-tools") return [] as never;
      if (path === "/v1/agent-models") return [{ key: "configured-default", name: "Platform default", configured_model: "test-model" }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={client}><AgentDetailView agentId="agent-1" /></QueryClientProvider>);

    const prompt = await screen.findByLabelText("System prompt");
    expect(prompt).toHaveValue(currentVersion.system_prompt);
    expect(screen.getByText("Draft source · v2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use version 1 as draft source" }));
    expect(screen.getByLabelText("System prompt")).toHaveValue(historicalVersion.system_prompt);
    expect(screen.getByText("Draft source · v1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use current version 2 as draft source" }));
    expect(screen.getByLabelText("System prompt")).toHaveValue(currentVersion.system_prompt);
    expect(screen.getByText("Draft source · v2")).toBeInTheDocument();
  });
});
