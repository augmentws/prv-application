import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateReviewBatchDialog } from "@/components/forms/create-review-batch-dialog";
import type { MatterSavedSearchRead } from "@/generated/models";

afterEach(cleanup);

describe("CreateReviewBatchDialog", () => {
  it("creates a deterministic sample from a saved search", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    const savedSearches = [{
      id: "saved-search-1",
      name: "Insurance correspondence",
      search: { query: "insurance", search_mode: "KEYWORD" },
    }] as unknown as MatterSavedSearchRead[];

    render(
      <CreateReviewBatchDialog
        groups={[]}
        batches={[]}
        savedSearches={savedSearches}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await user.type(screen.getByLabelText("Name"), "Sampled correspondence");
    await user.click(screen.getByRole("combobox", { name: "Document source" }));
    await user.click(screen.getByRole("option", { name: "Random sample from saved search" }));
    await user.click(screen.getByRole("combobox", { name: "Saved search" }));
    await user.click(screen.getByRole("option", { name: "Insurance correspondence" }));
    await user.type(screen.getByLabelText("Sample size"), "25");
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "Sampled correspondence",
      selection_type: "RANDOM_SAVED_SEARCH",
      saved_search_id: "saved-search-1",
      sample_size: 25,
    })));
  });
});
