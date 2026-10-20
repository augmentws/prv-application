import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BatchStatisticsPage } from "@/components/batch-statistics-page";
import type { ReviewBatchCodingComparisonRead, ReviewBatchComparisonOptionsRead, ReviewBatchRunPromotionRead, ReviewBatchStatisticsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BatchStatisticsPage", () => {
  it("compares human and analysis coding and exposes document exports", async () => {
    const options: ReviewBatchComparisonOptionsRead = {
      review_batch_id: "batch-1",
      fields: [{ id: "field-1", key: "responsiveness", display_name: "Responsiveness", type: "ENUM", cardinality: "SINGLE", allowed_values: [] }],
      sources: [
        { id: "human-1", source_type: "REVIEWER", run_type: null, purpose: null, status: null, label: "Ada Reviewer", detail: "ada@example.com", metadata_definition_ids: ["field-1"], created_at: "2026-10-08T12:00:00Z" },
        { id: "jev-1", source_type: "RUN", run_type: "WORKFLOW", purpose: "REVIEW", status: "COMPLETED", label: "Matter assessment", detail: "Published v2", metadata_definition_ids: ["field-1"], created_at: "2026-10-08T13:00:00Z" },
        { id: "jev-failed", source_type: "RUN", run_type: "WORKFLOW", purpose: "REVIEW", status: "FAILED", label: "Failed assessment", detail: "Published v1", metadata_definition_ids: ["field-1"], created_at: "2026-10-08T11:00:00Z" },
      ],
    };
    const comparison: ReviewBatchCodingComparisonRead = {
      review_batch_id: "batch-1",
      metadata_definition_id: "field-1",
      field_display_name: "Responsiveness",
      cardinality: "SINGLE",
      document_count: 2,
      filtered_document_count: 2,
      offset: 0,
      limit: 50,
      sources: options.sources.slice(0, 2),
      source_summaries: [
        { source_id: "human-1", evaluated_document_count: 2, no_value_document_count: 0, skipped_document_count: 0, failed_document_count: 0, incomplete_document_count: 0, not_evaluated_document_count: 0, value_counts: [{ value_key: "responsive", display_label: "Responsive", document_count: 2 }], value_counts_truncated: false },
        { source_id: "jev-1", evaluated_document_count: 2, no_value_document_count: 0, skipped_document_count: 0, failed_document_count: 0, incomplete_document_count: 0, not_evaluated_document_count: 0, value_counts: [{ value_key: "responsive", display_label: "Responsive", document_count: 1 }, { value_key: "not_responsive", display_label: "Not responsive", document_count: 1 }], value_counts_truncated: false },
      ],
      multi_source_agreement: { compared_document_count: 2, incomplete_document_count: 0, class_agreement: [{ value_key: "responsive", display_label: "Responsive", source_counts: [{ source_id: "human-1", document_count: 2 }, { source_id: "jev-1", document_count: 1 }], all_selected_count: 1, any_selected_count: 2, jaccard: 0.5 }] },
      pairwise_agreements: [{ left_source_id: "human-1", right_source_id: "jev-1", compared_document_count: 2, exact_match_count: 1, partial_match_count: 0, disagreement_count: 1, incomplete_document_count: 0, exact_agreement: 0.5, mean_jaccard: 0.5, confusion_matrix: [{ left_value: "responsive", right_value: "responsive", document_count: 1 }, { left_value: "responsive", right_value: "not_responsive", document_count: 1 }], class_agreement: [] }],
      documents: [{ matter_document_id: "document-1", collection_item_id: "item-1", sequence_number: 1, agreement_status: "EXACT", sources: [{ source_id: "human-1", state: "EVALUATED", value_keys: ["responsive"], display_values: ["Responsive"], top_confidence: null }, { source_id: "jev-1", state: "EVALUATED", value_keys: ["responsive"], display_values: ["Responsive"], top_confidence: 0.94 }] }],
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/review-batches/batch-1") return { id: "batch-1", name: "QC batch", document_count: 2 } as never;
      if (path.endsWith("/comparison-options")) return options as never;
      if (path.endsWith("/statistics")) return { review_batch_id: "batch-1", document_count: 2, can_promote: false, reviewers: [] } as never;
      if (path.endsWith("/coding-comparison/review-batches/preview")) return { source_batch_id: "batch-1", metadata_definition_id: "field-1", field_display_name: "Responsiveness", candidate_count: 1, sources: [] } as never;
      if (path.endsWith("/coding-comparison/review-batches") && init?.method === "POST") return { id: "batch-disagreements", name: "Responsiveness disagreements", status: "QUEUED", document_count: 0, selection_definition: { candidate_count: 1 } } as never;
      return comparison as never;
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><BatchStatisticsPage matterId="matter-1" batchId="batch-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "QC batch" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to batch" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(await screen.findByText("Coding counts")).toBeInTheDocument();
    expect(screen.queryByText("Failed assessment")).not.toBeInTheDocument();
    expect(screen.getByText("Pairwise agreement")).toBeInTheDocument();
    const overlap = screen.getByText("Per-value overlap").closest("section");
    expect(overlap).not.toBeNull();
    expect(within(overlap!).getByRole("columnheader", { name: "Ada Reviewer" })).toBeInTheDocument();
    expect(within(overlap!).getByRole("columnheader", { name: "Matter assessment" })).toBeInTheDocument();
    expect(screen.getAllByLabelText("About Jaccard similarity")).toHaveLength(2);
    expect(screen.getAllByRole("tooltip")[0]).toHaveTextContent("intersection ÷ union");
    expect(screen.getByText("Document matrix")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "CSV" })).toHaveAttribute("href", expect.stringContaining("source_id=human-1"));
    expect(screen.getByRole("link", { name: "Parquet" })).toHaveAttribute("href", expect.stringContaining("format=parquet"));

    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Document agreement filter"));
    await user.click(screen.getByRole("option", { name: "Disagreements" }));
    await user.click(await screen.findByRole("button", { name: "Create review batch" }));
    expect(await screen.findByRole("heading", { name: "Create disagreement review batch" })).toBeInTheDocument();
    expect(await screen.findByText("documents will be included.", { exact: false })).toHaveTextContent("1 documents will be included.");
    expect(screen.getByLabelText("Batch name")).toHaveValue("Responsiveness disagreements");
    await user.click(screen.getByRole("button", { name: "Create batch" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/batch-1/coding-comparison/review-batches",
      {
        method: "POST",
        body: JSON.stringify({ metadata_definition_id: "field-1", source_ids: ["human-1", "jev-1"], name: "Responsiveness disagreements" }),
      },
    ));
    expect(await screen.findByRole("link", { name: "Open review batch" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-disagreements");
  });

  it("shows per-reviewer field counts and confirms matter promotion", async () => {
    const statistics: ReviewBatchStatisticsRead = {
      review_batch_id: "batch-1",
      document_count: 2500,
      can_promote: true,
      reviewers: [{
        reviewer_coding_id: "coding-1",
        reviewer: { id: "user-1", display_name: "Ada Reviewer", email: "ada@example.com" },
        completed_document_count: 2477,
        skipped_document_count: 23,
        incomplete_document_count: 0,
        created_at: "2026-10-08T12:00:00Z",
        updated_at: "2026-10-08T13:00:00Z",
        promotion: null,
        field_counts: [{
          coding_group_id: "group-1",
          coding_group_name: "Review coding",
          metadata_definition_id: "definition-1",
          field_key: "responsiveness",
          field_display_name: "Responsiveness",
          document_count: 2400,
        }, {
          coding_group_id: "group-1",
          coding_group_name: "Review coding",
          metadata_definition_id: "definition-2",
          field_key: "issue_coding",
          field_display_name: "Issue Coding",
          document_count: 2100,
        }],
      }],
    };
    const queuedPromotion = {
      review_batch_run_id: null,
      reviewer_coding_id: "coding-1",
      status: "QUEUED",
    } as ReviewBatchRunPromotionRead;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/review-batches/batch-1") return { id: "batch-1", name: "First-level review", document_count: 2500 } as never;
      if (path.endsWith("/comparison-options")) return { review_batch_id: "batch-1", sources: [], fields: [] } as never;
      return (init?.method === "POST" ? queuedPromotion : statistics) as never;
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(
      <QueryClientProvider client={queryClient}>
        <BatchStatisticsPage matterId="matter-1" batchId="batch-1" />
      </QueryClientProvider>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Human progress & promotion" }));
    expect(await screen.findByText("Ada Reviewer")).toBeInTheDocument();
    expect(screen.getByText("Responsiveness")).toBeInTheDocument();
    expect(screen.getByText("2,400 coded")).toBeInTheDocument();
    expect(screen.getByText("Issue Coding")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Promote coding" }));
    expect(screen.getByText(/Promote Ada Reviewer.s coding\?/)).toBeInTheDocument();
    expect(screen.getByText(/field left blank clears its current matter value/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Confirm promotion" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/batch-1/reviewer-coding/coding-1/promotion",
      { method: "POST" },
    ));
  });
});
