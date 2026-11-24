import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateReviewBatchDialog } from "@/components/forms/create-review-batch-dialog";
import type { MatterDefinitionAssessmentRead, MatterSavedSearchRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("CreateReviewBatchDialog", () => {
  it("creates a batch with every document from a saved search", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    const savedSearches = [{
      id: "saved-search-1",
      name: "Insurance correspondence",
      search: { query: "insurance", search_mode: "KEYWORD" },
    }] as unknown as MatterSavedSearchRead[];

    render(
      <CreateReviewBatchDialog
        matterId="matter-1"
        groups={[]}
        batches={[]}
        savedSearches={savedSearches}
        assessments={[]}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await user.type(screen.getByLabelText("Name"), "All matching correspondence");
    await user.click(screen.getByRole("combobox", { name: "Document source" }));
    await user.click(screen.getByRole("option", { name: "All documents from saved search" }));
    await user.click(screen.getByRole("combobox", { name: "Saved search" }));
    await user.click(screen.getByRole("option", { name: "Insurance correspondence" }));
    expect(screen.queryByLabelText("Sample size")).not.toBeInTheDocument();
    expect(screen.getByText(/every matching document is included/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "All matching correspondence",
      selection_type: "SAVED_SEARCH",
      saved_search_id: "saved-search-1",
    })));
    expect(onCreate.mock.calls[0][0]).not.toHaveProperty("sample_size");
  });

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
        matterId="matter-1"
        groups={[]}
        batches={[]}
        savedSearches={savedSearches}
        assessments={[]}
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

  it("previews and creates a fixed-query plus random-sample batch", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    const savedSearches = [
      { id: "saved-fixed", name: "Known privilege", search: { query: "privileged", search_mode: "KEYWORD" } },
      { id: "saved-random", name: "Uncoded population", search: { query: "uncoded", search_mode: "KEYWORD" } },
    ] as unknown as MatterSavedSearchRead[];
    vi.mocked(coreApi).mockResolvedValue({
      fixed_match_count: 1220,
      sample_match_count: 19000,
      overlap_count: 0,
      eligible_sample_count: 19000,
      requested_sample_count: 5000,
      selected_sample_count: 5000,
      family_added_count: 0,
      final_document_count: 6220,
      random_seed: "preview-seed",
      search_index_generation_id: "generation-1",
      preview_hash: "a".repeat(64),
      warnings: [],
    } as never);

    render(
      <CreateReviewBatchDialog
        matterId="matter-1"
        groups={[]}
        batches={[]}
        savedSearches={savedSearches}
        assessments={[]}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await user.type(screen.getByLabelText("Name"), "Privilege validation population");
    await user.click(screen.getByRole("combobox", { name: "Document source" }));
    await user.click(screen.getByRole("option", { name: "Fixed query + random sample" }));
    await user.click(screen.getByRole("combobox", { name: "Fixed population" }));
    await user.click(screen.getByRole("option", { name: "Known privilege" }));
    await user.click(screen.getByRole("combobox", { name: "Random population" }));
    await user.click(screen.getByRole("option", { name: "Uncoded population" }));
    await user.type(screen.getByLabelText("Random sample size"), "5000");
    await user.click(screen.getByRole("button", { name: "Preview population" }));

    await screen.findByText("6,220");
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/composite-preview",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          fixed_saved_search_id: "saved-fixed",
          sample_saved_search_id: "saved-random",
          sample_size: 5000,
          include_complete_families: false,
        }),
      }),
    );
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "Privilege validation population",
      selection_type: "COMPOSITE_SAVED_SEARCH",
      fixed_saved_search_id: "saved-fixed",
      sample_saved_search_id: "saved-random",
      sample_size: 5000,
      random_seed: "preview-seed",
      expected_search_index_generation_id: "generation-1",
      preview_hash: "a".repeat(64),
    })));
  });

  it("samples the full persisted candidate pool from an assessment", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    const assessments = [{
      id: "assessment-1",
      name: "Responsiveness coverage",
      candidate_count: 2750,
      selected_count: 1000,
    }] as unknown as MatterDefinitionAssessmentRead[];

    render(
      <CreateReviewBatchDialog
        matterId="matter-1"
        groups={[]}
        batches={[]}
        savedSearches={[]}
        assessments={assessments}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await user.type(screen.getByLabelText("Name"), "Assessment pool sample");
    await user.click(screen.getByRole("combobox", { name: "Document source" }));
    await user.click(screen.getByRole("option", { name: "Random sample from assessment" }));
    await user.click(screen.getByRole("combobox", { name: "Assessment" }));
    await user.click(screen.getByRole("option", { name: /Responsiveness coverage/ }));
    await user.type(screen.getByLabelText("Sample size"), "125");
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "Assessment pool sample",
      selection_type: "RANDOM_ASSESSMENT",
      assessment_run_id: "assessment-1",
      sample_size: 125,
    })));
  });

  it("can include complete families for an ad hoc sample", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(
      <CreateReviewBatchDialog
        matterId="matter-1"
        groups={[]}
        batches={[]}
        savedSearches={[]}
        assessments={[]}
        onCreate={onCreate}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await user.type(screen.getByLabelText("Name"), "Family sample");
    await user.click(screen.getByRole("combobox", { name: "Document source" }));
    await user.click(screen.getByRole("option", { name: "Random matter sample" }));
    await user.type(screen.getByLabelText("Sample size"), "10");
    await user.click(screen.getByRole("checkbox", { name: /Include complete families/ }));
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "Family sample",
      selection_type: "RANDOM_MATTER",
      sample_size: 10,
      include_complete_families: true,
    })));
  });
});
