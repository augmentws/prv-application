import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReviewProgramFinalAlignmentPage } from "@/components/review-program-final-alignment-page";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

describe("ReviewProgramFinalAlignmentPage", () => {
  it("shows aggregate, issue, and paged document comparisons with named Q&A models", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs/run-1") return {
        id: "run-1", program_id: "program-1", status: "COMPLETED", current_stage: "COMPLETION", current_stage_ordinal: 18,
        created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null, actions: [], stage_attempts: [],
        progress_summary: { final_alignment: {
          minimum_confidence: 0.85, population_document_count: 15791,
          alignment_counts: { EXACT_ALL: 1924, QA_PAIR_EXACT: 11180, PARTIAL: 2613, DISAGREEMENT: 71, INCOMPLETE: 3 },
          issues: [{ issue_key: "issue_tag:issue_1_market_disruption", classifier: 7318, profile_a: 3632, profile_b: 3799, qa_pair: 3500, all_three: 3465 }],
        } },
      } as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs/run-1/final-alignment?offset=0&limit=50") return {
        manifest_id: "manifest-1", artifact_id: "artifact-1", manifest_content_hash: "a".repeat(64),
        population_scope: "CLASSIFIER_POSITIVE_AT_OR_ABOVE_COVERAGE_THRESHOLD", minimum_confidence: 0.85,
        profile_a_name: "jev-latest", profile_b_name: "gpt-6-luna", total: 51, offset: 0, limit: 50,
        items: [{ matter_document_id: "document-1", collection_item_id: "item-1", scope_ordinal: 1, filename: "memo.pdf", input_status: "READY", classifier_codings: [{ key: "issue_1", label: "Issue 1 — Market Disruption" }], profile_a_codings: [{ key: "issue_1", label: "Issue 1 — Market Disruption" }], profile_b_codings: [], human_codings: [], alignment_status: "PARTIAL", source_status: "CURRENT", confidence_band: "0.85-0.90", training_overlap: false, label_authority: "UNLABELED", family_id: null, record_type: "PDF" }],
      } as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs/run-1/final-alignment?offset=50&limit=50") return {
        manifest_id: "manifest-1", artifact_id: "artifact-1", manifest_content_hash: "a".repeat(64),
        population_scope: "CLASSIFIER_POSITIVE_AT_OR_ABOVE_COVERAGE_THRESHOLD", minimum_confidence: 0.85,
        profile_a_name: "jev-latest", profile_b_name: "gpt-6-luna", total: 51, offset: 50, limit: 50, items: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><ReviewProgramFinalAlignmentPage clientId="client-1" matterId="matter-1" programId="program-1" runId="run-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Classifier and decision-model comparison" })).toBeInTheDocument();
    expect(await screen.findByText(/jev-latest and gpt-6-luna/)).toBeInTheDocument();
    expect(screen.getByLabelText("Alignment distribution chart")).toBeInTheDocument();
    expect(screen.getByText(/70\.8%/)).toBeInTheDocument();
    expect(screen.getByText("Issue 1 Market Disruption")).toBeInTheDocument();
    expect(screen.getByText("memo.pdf")).toBeInTheDocument();
    expect(screen.getAllByText("Issue 1 — Market Disruption")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Back to Review Program" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/review-programs?program=program-1&run=run-1");
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
  });
});
