import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterDefinitionPanel } from "@/components/matter-definition-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const definition = {
  id: "definition-1",
  matter_id: "matter-1",
  key: "general_review",
  name: "General Review Guidance",
  description: "Primary reviewer guidance.",
  status: "ACTIVE",
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
    source_skill_run_id: null,
    source_guidance_id: null,
    source_revision_id: null,
    source_content_hash: null,
    created_at: "2026-09-14T12:00:00Z",
  },
};

const conversation = {
  id: "conversation-1",
  tenant_id: "tenant-1",
  client_id: "client-1",
  matter_id: "matter-1",
  matter_definition_id: "definition-1",
  agent_definition_id: "agent-1",
  agent_definition_version_id: "agent-version-1",
  title: "Responsiveness review",
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
  it("switches profiles without leaking an unsaved draft", async () => {
    const privilege = {
      ...definition,
      id: "definition-2",
      key: "privilege_review",
      name: "Privilege Review",
      description: "Review legal advice and work product.",
      revision: {
        ...definition.revision,
        id: "revision-2",
        matter_definition_id: "definition-2",
        content_markdown: "# Privilege guidance\n\nReview legal advice.",
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/guidance") return [definition, privilege] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/revisions") return [definition.revision] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-2/revisions") return [privilege.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-1") return [] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-2") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();
    const editor = await screen.findByRole("textbox", { name: "Matter Definition Markdown" });
    await user.type(editor, "\nUnsaved first-pass change");
    await user.click(screen.getByRole("button", { name: /Privilege Review/ }));

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Matter Definition Markdown" })).toHaveValue(privilege.revision.content_markdown));
    expect(screen.getByRole("textbox", { name: "Matter Definition Markdown" })).not.toHaveValue(expect.stringContaining("Unsaved first-pass change"));
  });

  it("switches the right-side workspace between chat and corpus assessment", async () => {
    const currentDefinition = {
      ...definition,
      current_revision: 2,
      published_revision: 1,
      revision: {
        ...definition.revision,
        id: "revision-2",
        revision: 2,
        content_markdown: "# Current coding guidance\n\nUse the updated responsiveness rule.",
        based_on_revision: 1,
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/guidance") return [currentDefinition] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/revisions") return [currentDefinition.revision, definition.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [{
        id: "agent-1", owner_tenant_id: "tenant-1", scope: "SYSTEM", key: "matter_definition_setup", name: "Matter Definition Setup",
        description: null, current_version: 1, published_version: 1, status: "ACTIVE", created_by_user_id: "user-1",
        created_at: "2026-09-14T11:00:00Z", updated_at: "2026-09-14T11:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-1") return [] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/assessments") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();

    const chatTab = await screen.findByRole("tab", { name: "Chat" });
    const assessmentTab = screen.getByRole("tab", { name: "Assessment" });
    const workspace = screen.getByRole("region", { name: "Matter Definition workspace" });
    expect(chatTab).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByLabelText("Chat controls")).getByRole("combobox", { name: "Chat agent" })).toBeInTheDocument();
    expect(workspace).toHaveClass("matter-definition-workspace");
    expect(screen.getByRole("region", { name: "Matter Definition workspace" }).querySelector("section")).toHaveClass("matter-definition-guidance-frame");
    expect(screen.getByRole("complementary", { name: "Matter Definition chat and assessment" })).toHaveClass("matter-definition-tools-frame");
    const guidanceText = screen.getByRole("textbox", { name: "Matter Definition Markdown" });
    expect(guidanceText).toHaveClass("resize-none", "overflow-y-auto");
    expect(guidanceText).toHaveValue(currentDefinition.revision.content_markdown);
    expect(screen.queryByRole("button", { name: /reviewer guidance/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Revision history")).not.toBeInTheDocument();
    const draftToolbar = screen.getByText("Current draft").parentElement?.parentElement;
    expect(draftToolbar).not.toBeNull();
    expect(within(draftToolbar!).getByRole("button", { name: "Publish current draft" })).toBeInTheDocument();
    expect(within(draftToolbar!).getByRole("button", { name: "Save draft" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Current draft" }));
    const revisionHistory = screen.getByLabelText("Revision history");
    expect(revisionHistory).toBeInTheDocument();
    await user.click(within(revisionHistory).getByRole("button", { name: /Revision 1/ }));
    const revisionDiff = screen.getByRole("region", { name: "Revision 1 to Current draft r2 diff" });
    expect(revisionDiff.querySelector('[data-diff="delete"]')).toHaveTextContent("Review for");
    expect(revisionDiff.querySelector('[data-diff="insert"]')).toHaveTextContent("urrent c");
    expect(screen.queryByRole("textbox", { name: "Matter Definition Markdown" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "View revision" }));
    const historicalGuidance = screen.getByRole("textbox", { name: "Matter Definition Markdown" });
    expect(historicalGuidance).toHaveValue(definition.revision.content_markdown);
    expect(historicalGuidance).toHaveAttribute("readonly");
    await user.click(screen.getByRole("button", { name: "Compare with current" }));
    expect(screen.getByRole("region", { name: "Revision 1 to Current draft r2 diff" })).toBeInTheDocument();

    await user.click(within(revisionHistory).getByRole("button", { name: /Current draft/ }));
    const currentGuidance = screen.getByRole("textbox", { name: "Matter Definition Markdown" });
    expect(currentGuidance).toHaveValue(currentDefinition.revision.content_markdown);
    expect(currentGuidance).not.toHaveAttribute("readonly");

    await user.click(assessmentTab);

    expect(assessmentTab).toHaveAttribute("aria-selected", "true");
    const assessmentPanel = screen.getByRole("tabpanel");
    expect(assessmentPanel).toHaveClass("flex", "min-h-0", "overflow-hidden");
    expect(await within(assessmentPanel).findByLabelText("Assessment controls")).toHaveTextContent("No assessments");
    expect(screen.getByText("No corpus assessments have been run for this Matter Definition.")).toBeInTheDocument();
  });

  it("renames a Matter Definition chat", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/guidance") return [definition] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/revisions") return [definition.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-1") return [conversation] as never;
      if (path === "/v1/agent-conversations/conversation-1/messages") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/runs") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/action-requests") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1" && init?.method === "PATCH") return { ...conversation, title: "Privilege review" } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();

    expect(await screen.findByText(/Responsiveness review/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rename chat" }));
    const input = screen.getByLabelText("Chat name");
    await user.clear(input);
    await user.type(input, "Privilege review");
    await user.click(screen.getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/agent-conversations/conversation-1",
      { method: "PATCH", body: JSON.stringify({ title: "Privilege review" }) },
    ));
    expect(await screen.findByText(/Privilege review/)).toBeInTheDocument();
  });

  it("keeps the automatically selected chat available for another turn", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/guidance") return [definition] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/revisions") return [definition.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [{
        id: "agent-1", owner_tenant_id: "tenant-1", scope: "SYSTEM", key: "matter_definition_setup", name: "Matter Definition Setup",
        description: null, current_version: 1, published_version: 1, status: "ACTIVE", created_by_user_id: "user-1",
        created_at: "2026-09-14T11:00:00Z", updated_at: "2026-09-14T11:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-1") return [{ ...conversation, status: "ACTIVE" }] as never;
      if (path === "/v1/agent-conversations/conversation-1/messages") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/runs") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/action-requests") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/turns" && init?.method === "POST") return {
        turn: {
          id: "turn-2", conversation_id: "conversation-1", sequence: 2, status: "QUEUED", created_by_user_id: "user-1",
          completed_at: null, error_message: null, created_at: "2026-09-14T12:03:00Z", updated_at: "2026-09-14T12:03:00Z",
        },
        message: {
          id: "message-2", conversation_id: "conversation-1", turn_id: "turn-2", sequence: 2, role: "USER",
          content: "Can we continue?", message_data: {}, created_by_user_id: "user-1", agent_run_id: null,
          created_at: "2026-09-14T12:03:00Z",
        },
        run: {
          id: "run-2", conversation_id: "conversation-1", turn_id: "turn-2", sequence: 1, workflow_id: "agent-run:run-2",
          parent_run_id: null, deferred_from_run_id: null, agent_definition_version_id: "agent-version-1", actor_user_id: "user-1",
          model_key: "configured-default", status: "QUEUED", output_text: null, request_count: 0, tool_call_count: 0,
          input_tokens: 0, cached_input_tokens: 0, cache_write_tokens: 0, output_tokens: 0, error_message: null,
          started_at: null, completed_at: null, created_at: "2026-09-14T12:03:00Z",
        },
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    const user = userEvent.setup();
    renderPanel();

    const message = await screen.findByLabelText("Message the Matter Definition chat");
    expect(message).toHaveAttribute("placeholder", "Compare coding fields, improve guidance, or ask the chat to review the current Matter Definition…");
    expect(message).toHaveClass("border-0", "focus-visible:ring-0");
    expect(screen.queryByText("Enter to send · Shift+Enter for a new line")).not.toBeInTheDocument();
    await user.type(message, "Can we continue?");
    await user.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/agent-conversations/conversation-1/turns",
      { method: "POST", body: JSON.stringify({ message: "Can we continue?" }) },
    ));
    expect(coreApi).not.toHaveBeenCalledWith("/v1/agent-conversations//turns", expect.anything());
  });

  it("shows the draft and records approval decisions through the agent workflow", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/guidance") return [definition] as never;
      if (path === "/v1/matters/matter-1/guidance/definition-1/revisions") return [definition.revision] as never;
      if (path === "/v1/matters/matter-1/agents") return [{
        id: "agent-1", owner_tenant_id: "tenant-1", scope: "SYSTEM", key: "matter_definition_setup", name: "Matter Definition Setup",
        description: null, current_version: 1, published_version: 1, status: "ACTIVE", created_by_user_id: "user-1",
        created_at: "2026-09-14T11:00:00Z", updated_at: "2026-09-14T11:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP&guidance_id=definition-1") return [conversation] as never;
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
