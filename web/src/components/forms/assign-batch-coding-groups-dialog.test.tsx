import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssignBatchCodingGroupsDialog } from "@/components/forms/assign-batch-coding-groups-dialog";
import type { MetadataGroupRead, ReviewBatchRead } from "@/generated/models";

afterEach(cleanup);

describe("AssignBatchCodingGroupsDialog", () => {
  it("assigns selected groups to an existing batch", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const batch = {
      id: "batch-1",
      name: "Coverage review",
      coding_groups: [],
    } as unknown as ReviewBatchRead;
    const groups = [{
      id: "group-1",
      display_name: "Review decisions",
      definition_ids: ["definition-1", "definition-2"],
      scope: "MATTER",
      status: "ACTIVE",
    }] as unknown as MetadataGroupRead[];

    render(<AssignBatchCodingGroupsDialog batch={batch} groups={groups} onSave={onSave} />);
    await userEvent.click(screen.getByRole("button", { name: /groups/i }));
    await userEvent.click(screen.getByText("Review decisions"));
    await userEvent.click(screen.getByRole("button", { name: "Save groups" }));

    expect(onSave).toHaveBeenCalledWith("batch-1", ["group-1"]);
  });

  it("removes an assigned group and permits an empty configuration", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const batch = {
      id: "batch-1",
      name: "Coverage review",
      coding_groups: [{
        id: "batch-group-1",
        source_metadata_group_id: "group-1",
        display_name: "Review decisions",
        fields: [],
      }],
    } as unknown as ReviewBatchRead;
    const groups = [{
      id: "group-1",
      display_name: "Review decisions",
      definition_ids: ["definition-1"],
      scope: "MATTER",
      status: "ACTIVE",
    }] as unknown as MetadataGroupRead[];

    render(<AssignBatchCodingGroupsDialog batch={batch} groups={groups} onSave={onSave} />);
    await userEvent.click(screen.getByRole("button", { name: /groups/i }));
    await userEvent.click(screen.getByText("Review decisions"));
    await userEvent.click(screen.getByRole("button", { name: "Save groups" }));

    expect(onSave).toHaveBeenCalledWith("batch-1", []);
  });
});
