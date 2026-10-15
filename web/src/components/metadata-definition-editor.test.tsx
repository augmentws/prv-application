import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MetadataDefinitionEditor } from "@/components/metadata-definition-editor";
import type { MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const definition: MetadataDefinitionRead = {
  id: "definition-1",
  matter_id: "matter-1",
  key: "issue_coding",
  display_name: "Issue coding",
  description: "Matter issues",
  type: "ENUM",
  cardinality: "MULTIPLE",
  allowed_values: [
    { key: "topic_401", label: "Olympics", description: "Olympic matters", active: true },
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
  normalize_to_lowercase: false,
  hierarchy_separator: null,
  reviewable: true,
  ai_assignable: false,
  status: "ACTIVE",
  created_at: "2026-10-07T12:00:00Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MetadataDefinitionEditor", () => {
  it("edits controlled values in the same dialog as field settings", async () => {
    const onChange = vi.fn();
    vi.mocked(coreApi).mockResolvedValue({
      ...definition,
      allowed_values: [
        { key: "topic_401", label: "Olympic Games", description: "Olympic matters", active: true },
        definition.allowed_values![1],
      ],
    } as never);
    const user = userEvent.setup();
    render(<MetadataDefinitionEditor matterId="matter-1" definition={definition} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit Issue coding" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Controlled values" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("Legacy")).toBeDisabled();

    const label = screen.getByDisplayValue("Olympics");
    await user.clear(label);
    await user.type(label, "Olympic Games");
    const valueRow = label.closest("div.rounded-lg");
    expect(valueRow).not.toBeNull();
    await user.click(within(valueRow as HTMLElement).getByRole("button", { name: "Save value" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1/enum-values/topic_401",
      { method: "PATCH", body: JSON.stringify({ label: "Olympic Games", description: "Olympic matters" }) },
    ));
    expect(onChange).toHaveBeenCalled();
  });

  it("adds a controlled value from the combined dialog", async () => {
    const onChange = vi.fn();
    vi.mocked(coreApi).mockResolvedValue({
      ...definition,
      allowed_values: [...definition.allowed_values!, { key: "topic_402", label: "Energy", description: null, active: true }],
    } as never);
    const user = userEvent.setup();
    render(<MetadataDefinitionEditor matterId="matter-1" definition={definition} onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Stable key", { selector: "#new-enum-key" }), "topic_402");
    await user.type(screen.getByLabelText("Display label", { selector: "#new-enum-label" }), "Energy");
    await user.click(screen.getByRole("button", { name: "Add value" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1/enum-values",
      { method: "POST", body: JSON.stringify({ key: "topic_402", label: "Energy", description: null }) },
    ));
  });
});
