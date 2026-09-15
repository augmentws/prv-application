import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateAgentForm } from "@/components/forms/create-agent-form";

afterEach(cleanup);

describe("CreateAgentForm", () => {
  it("creates a draft with executable tools and keeps planned tools disabled", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <CreateAgentForm
        models={[{
          key: "configured-default",
          name: "Platform default",
          description: "Configured model",
          configured_model: "google:gemini-test",
          available: true,
        }]}
        tools={[
          {
            key: "matter_definition.read",
            name: "Read matter definition",
            description: "Read the current definition.",
            requires_approval: false,
            runtime_available: true,
          },
          {
            key: "matter_metadata.create_definition",
            name: "Create metadata definition",
            description: "Create a field after approval.",
            requires_approval: true,
            runtime_available: false,
          },
        ]}
        onCreate={onCreate}
      />,
    );

    await user.type(screen.getByLabelText("Name"), "Guidance Agent");
    await user.type(screen.getByLabelText("Stable key"), "guidance_agent");
    await user.type(
      screen.getByLabelText("System prompt"),
      "Review the matter guidance and explain every proposed change.",
    );
    await user.click(screen.getByRole("checkbox", { name: /Read matter definition/ }));

    expect(screen.getByRole("checkbox", { name: /Create metadata definition/ })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Create draft agent" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      key: "guidance_agent",
      name: "Guidance Agent",
      initial_version: expect.objectContaining({
        model_key: "configured-default",
        model_policy: { temperature: 0 },
        limits: { max_requests: 30, max_tool_calls: 20 },
        tools: [{ key: "matter_definition.read", configuration: {} }],
      }),
    })));
  });
});
