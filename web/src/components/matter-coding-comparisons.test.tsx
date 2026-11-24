import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterCodingComparisons } from "@/components/matter-coding-comparisons";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Matter coding comparisons", () => {
  it("presents multi-batch sources and explains artifact-backed storage", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/coding-comparisons/options") return {
        batches: [{ id: "batch-1", name: "Privilege sample", document_count: 5000 }],
        fields: [
          { id: "trec-field", key: "trec_priv_coding", display_name: "TREC privilege coding", type: "BOOLEAN", cardinality: "SINGLE", allowed_values: [] },
          { id: "human-field", key: "privilege", display_name: "Privilege", type: "ENUM", cardinality: "SINGLE", allowed_values: [{ key: "privileged", label: "Privileged" }, { key: "not_privileged", label: "Not privileged" }] },
        ],
        qa_runs: [{ id: "run-1", review_batch_id: "batch-1", label: "Privilege sample · Agent run", run_type: "AGENT", status: "COMPLETED", metadata_definition_ids: ["trec-field"] }],
        reviewer_codings: [{ id: "coding-1", review_batch_id: "batch-1", reviewer_user_id: "user-1", label: "Reviewer One", detail: "reviewer@example.com · Privilege sample", revision: 100, metadata_definition_ids: ["trec-field"] }],
        classifier_scoring_runs: [{ id: "score-1", label: "Privilege classifier", metadata_definition_id: "trec-field", population_document_count: 20000, completed_at: "2026-11-19T00:00:00Z" }],
      } as never;
      if (path === "/v1/matters/matter-1/coding-comparisons") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><MatterCodingComparisons matterId="matter-1" batchesHref="/batches" /></QueryClientProvider>);

    expect(await screen.findByText("Privilege sample")).toBeInTheDocument();
    expect(screen.getByText("Q&A coding")).toBeInTheDocument();
    expect(screen.getByText("Reviewer coding")).toBeInTheDocument();
    expect(screen.getByText("Matter coding")).toBeInTheDocument();
    expect(screen.getByText("Classifier predictions")).toBeInTheDocument();
    expect(screen.getByText(/Document-level matrices are stored as artifacts/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to batches" })).toHaveAttribute("href", "/batches");
  });

  it("presents completed artifacts as comparison statistics before document detail", async () => {
    const completedComparison = {
      id: "comparison-1",
      name: "Privilege validation",
      status: "COMPLETED",
      population_document_count: 100,
      compared_document_count: 90,
      result_artifact_id: "artifact-1",
      created_at: "2026-11-19T00:00:00Z",
      configuration: { sources: [{ source_id: "qa", label: "Q&A coding" }, { source_id: "human", label: "Human coding" }] },
      summary: {
        document_count: 100,
        agreement_counts: { EXACT: 60, DISAGREEMENT: 30, INCOMPLETE: 10 },
        sources: [
          { source_id: "qa", label: "Q&A coding", source_type: "QA_RUN_GROUP", state_counts: { EVALUATED: 95, MISSING: 5 }, value_counts: { PRIVILEGED: 40 }, evaluated_document_count: 95, no_value_document_count: 55 },
          { source_id: "human", label: "Human coding", source_type: "MATTER_FIELD", state_counts: { EVALUATED: 95, MISSING: 5 }, value_counts: { PRIVILEGED: 50 }, evaluated_document_count: 95, no_value_document_count: 45 },
        ],
        pairwise: [{
          left_source_id: "qa", right_source_id: "human", compared_document_count: 90, incomplete_document_count: 10,
          agreement_counts: { EXACT: 60, DISAGREEMENT: 30, INCOMPLETE: 10 }, exact_agreement: 2 / 3, mean_jaccard: 0.7,
          confusion_matrix: [{ left_value: "PRIVILEGED", right_value: "PRIVILEGED", document_count: 35 }],
        }],
        multi_source: {
          compared_document_count: 90, incomplete_document_count: 10,
          class_agreement: [{ value_key: "PRIVILEGED", source_counts: [{ source_id: "qa", document_count: 40 }, { source_id: "human", document_count: 50 }], all_selected_count: 35, any_selected_count: 55, jaccard: 35 / 55 }],
        },
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/coding-comparisons/options") return {
        batches: [], fields: [], qa_runs: [], reviewer_codings: [], classifier_scoring_runs: [],
      } as never;
      if (path === "/v1/matters/matter-1/coding-comparisons") return [completedComparison] as never;
      if (path === "/v1/matters/matter-1/coding-comparisons/comparison-1") return completedComparison as never;
      if (String(path).startsWith("/v1/matters/matter-1/coding-comparisons/comparison-1/documents")) return {
        items: [], total: 0, offset: 0, limit: 100,
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><MatterCodingComparisons matterId="matter-1" batchesHref="/batches" /></QueryClientProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "View statistics" }));

    expect(await screen.findByText("Privilege validation statistics")).toBeInTheDocument();
    expect(window.location.search).toContain("comparison=comparison-1");
    expect(screen.getByText("Fully compared")).toBeInTheDocument();
    expect(screen.getByText("Coding counts")).toBeInTheDocument();
    expect(screen.getByText("Pairwise agreement")).toBeInTheDocument();
    expect(screen.getByText("Confusion matrix")).toBeInTheDocument();
    expect(screen.getByText("Per-value overlap")).toBeInTheDocument();
    expect(screen.getByText("Document matrix")).toBeInTheDocument();
  });
});
