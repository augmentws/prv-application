import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterDefinitionPanel } from "@/components/matter-definition-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const definition = {
  id: "definition-1",
  matter_id: "matter-1",
  current_revision: 1,
  published_revision: null,
  created_by_user_id: "user-1",
  created_at: "2026-09-14T12:00:00Z",
  updated_at: "2026-09-14T12:00:00Z",
  revision: {
    id: "revision-1",
    matter_definition_id: "definition-1",
    revision: 1,
    content_markdown: "# Coding guidance\n\nReview for responsiveness.",
    source_kind: "PASTE",
    source_artifact_id: null,
    source_filename: null,
    based_on_revision: null,
    created_by_user_id: "user-1",
    agent_run_id: null,
    created_at: "2026-09-14T12:00:00Z",
  },
};

const conversation = {
  id: "conversation-1",
  tenant_id: "tenant-1",
  client_id: "client-1",
  matter_id: "matter-1",
  agent_definition_id: "agent-1",
  agent_definition_version_id: "agent-version-1",
  workflow_type: "MATTER_DEFINITION_SETUP",
  status: "WAITING_APPROVAL",
  initiated_by_user_id: "user-1",
  created_at: "2026-09-14T12:01:00Z",
  updated_at: "2026-09-14T12:02:00Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><MatterDefinitionPanel matterId="matter-1" /></QueryClientProvider>);
}

describe("MatterDefinitionPanel", () => {
  it("shows the draft and records approval decisions through the agent workflow", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition") return definition as never;
      if (path === "/v1/matters/matter-1/definition/revisions") return [definition.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [{
        id: "agent-1", owner_tenant_id: "tenant-1", scope: "SYSTEM", key: "matter_definition_setup", name: "Matter Definition Setup",
        description: null, current_version: 1, published_version: 1, status: "ACTIVE", created_by_user_id: "user-1",
        created_at: "2026-09-14T11:00:00Z", updated_at: "2026-09-14T11:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations") return [conversation] as never;
      if (path === "/v1/agent-conversations/conversation-1/messages") return [{
        id: "message-1", conversation_id: "conversation-1", turn_id: "turn-1", sequence: 1, role: "ASSISTANT",
        content: "I found one wording improvement.", message_data: {}, created_by_user_id: null, agent_run_id: "run-1",
        created_at: "2026-09-14T12:02:00Z",
      }] as never;
      if (path === "/v1/agent-conversations/conversation-1/runs") return [{
        id: "run-1", conversation_id: "conversation-1", turn_id: "turn-1", sequence: 1, workflow_id: "workflow-1",
        parent_run_id: null, deferred_from_run_id: null, agent_definition_version_id: "agent-version-1", actor_user_id: "user-1",
        model_key: "configured-default", status: "WAITING_APPROVAL", output_text: null, request_count: 1, tool_call_count: 1,
        input_tokens: 50, output_tokens: 20, error_message: null, started_at: "2026-09-14T12:01:00Z",
        completed_at: "2026-09-14T12:02:00Z", created_at: "2026-09-14T12:01:00Z",
      }] as never;
      if (path === "/v1/agent-conversations/conversation-1/action-requests") return [{
        id: "action-1", conversation_id: "conversation-1", turn_id: "turn-1", agent_run_id: "run-1", tool_call_id: "tool-1",
        tool_key: "matter_definition.apply_draft_edit", arguments: {
          content_markdown: "# Improved coding guidance", based_on_revision: 1, reason: "Clarify the decision rule.",
        }, summary: "Apply improved guidance", status: "PENDING", requested_at: "2026-09-14T12:02:00Z", executed_at: null,
      }] as never;
      if (path === "/v1/agent-action-requests/action-1/decision" && init?.method === "POST") return {
        action_request: { id: "action-1", status: "APPROVED" },
        decision: { decision: "APPROVE" },
        resumed_run: null,
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByDisplayValue(/Review for responsiveness/)).toBeInTheDocument();
    expect(await screen.findByText("Apply improved guidance")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve change" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/agent-action-requests/action-1/decision",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ decision: "APPROVE", reason: null }) }),
    ));
  });
});
