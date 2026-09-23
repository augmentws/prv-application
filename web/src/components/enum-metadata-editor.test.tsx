import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EnumMetadataEditor } from "@/components/enum-metadata-editor";
import type { MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const definition: MetadataDefinitionRead = {
  id: "definition-1",
  matter_id: "matter-1",
  key: "responsiveness",
  display_name: "Responsiveness",
  description: "Review decision",
  type: "ENUM",
  cardinality: "SINGLE",
  allowed_values: [
    { key: "responsive", label: "Responsive", description: "Within scope", active: true },
    { key: "legacy", label: "Legacy", description: null, active: false },
  ],
  value_source: "ASSERTED",
  reference_target: null,
  template_key: null,
  template_version: null,
  assertion_policy: "IMMEDIATE",
  resolution_policy: "EXPLICIT_ONLY",
  searchable: true,
  facetable: true,
  reviewable: true,
  ai_assignable: true,
  status: "ACTIVE",
  created_at: "2026-09-20T12:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("EnumMetadataEditor", () => {
  it("shows active and inactive values and updates an active value", async () => {
    const onChange = vi.fn();
    vi.mocked(coreApi).mockResolvedValue({
      ...definition,
      allowed_values: [
        { key: "responsive", label: "Relevant", description: "Within scope", active: true },
        definition.allowed_values![1],
      ],
    } as never);
    const user = userEvent.setup();
    render(<EnumMetadataEditor matterId="matter-1" definition={definition} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Manage values" }));
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByText("Inactive")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Legacy")).toBeDisabled();

    const label = screen.getByDisplayValue("Responsive");
    await user.clear(label);
    await user.type(label, "Relevant");
    const section = label.closest("section");
    expect(section).not.toBeNull();
    await user.click(within(section!).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1/enum-values/responsive",
      { method: "PATCH", body: JSON.stringify({ label: "Relevant", description: "Within scope" }) },
    ));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ allowed_values: expect.arrayContaining([expect.objectContaining({ key: "responsive", label: "Relevant" })]) }));
  });

  it("adds a new stable enum value", async () => {
    const onChange = vi.fn();
    vi.mocked(coreApi).mockResolvedValue({
      ...definition,
      allowed_values: [...definition.allowed_values!, { key: "needs_follow_up", label: "Needs follow-up", description: null, active: true }],
    } as never);
    const user = userEvent.setup();
    render(<EnumMetadataEditor matterId="matter-1" definition={definition} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Manage values" }));
    await user.type(screen.getByLabelText("Stable key"), "needs_follow_up");
    const displayLabels = screen.getAllByLabelText("Display label");
    await user.type(displayLabels[displayLabels.length - 1], "Needs follow-up");
    await user.click(screen.getByRole("button", { name: "Add value" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1/enum-values",
      { method: "POST", body: JSON.stringify({ key: "needs_follow_up", label: "Needs follow-up", description: null }) },
    ));
    expect(onChange).toHaveBeenCalled();
  });
});
