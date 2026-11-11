import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ReviewProgramQAPopulationStatisticsPage } from "@/components/review-program-qa-population-statistics-page";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const qaSources = [
  { id: "profile-1", source_type: "RUN", run_type: "WORKFLOW", purpose: null, status: "COMPLETED", preparation_status: "READY", classification_scoring_run_id: null, label: "jev-latest", detail: "JEV", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
  { id: "profile-2", source_type: "RUN", run_type: "WORKFLOW", purpose: null, status: "COMPLETED", preparation_status: "READY", classification_scoring_run_id: null, label: "gpt-6-luna", detail: "OpenAI Decisions", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
] as const;

function response(includeClassifier = false, includeHuman = false) {
  const optional = [
    { id: "classifier-1", source_type: "RUN", run_type: "CLASSIFIER", purpose: "CANDIDATE", status: "COMPLETED", preparation_status: "READY", classification_scoring_run_id: null, label: "Insurance classifier", detail: "Whole-matter classifier result", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
    { id: "human-1", source_type: "REVIEWER", run_type: null, purpose: null, status: null, preparation_status: "READY", classification_scoring_run_id: null, label: "Human reviewer decisions", detail: "Recorded disagreement-review adjudications", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
  ];
  const sources = [...qaSources, ...(includeClassifier ? [optional[0]] : []), ...(includeHuman ? [optional[1]] : [])];
  return {
    run_id: "run-1", task_version_id: "task-version-1", document_count: 5200,
    both_profiles_document_count: 5000, one_profile_document_count: 200,
    fields: [{ id: "field-1", key: "insurance_topics", display_name: "Insurance topics", type: "ENUM", cardinality: "MULTIPLE", allowed_values: [{ key: "reform", label: "Reform" }] }],
    selected_metadata_definition_id: "field-1",
    available_sources: [...qaSources, ...optional],
    sources,
    source_summaries: sources.map((source, index) => ({ source_id: source.id, evaluated_document_count: index < 2 ? 5100 : 4000, no_value_document_count: 2000, skipped_document_count: 0, failed_document_count: 0, incomplete_document_count: 0, not_evaluated_document_count: index < 2 ? 100 : 1200, value_counts: [{ value_key: "reform", display_label: "Reform", document_count: 3000 }], value_counts_truncated: false })),
    pairwise_agreements: sources.flatMap((left, leftIndex) => sources.slice(leftIndex + 1).map((right) => ({ left_source_id: left.id, right_source_id: right.id, compared_document_count: 4000, exact_match_count: 3600, partial_match_count: 250, disagreement_count: 150, incomplete_document_count: 1200, exact_agreement: 0.9, mean_jaccard: 0.94, confusion_matrix: [], class_agreement: [{ value_key: "reform", display_label: "Reform", both_selected_count: 2800, left_only_count: 200, right_only_count: 100, both_not_selected_count: 900, jaccard: 0.903 }] }))),
    multi_source_agreement: { compared_document_count: 4000, incomplete_document_count: 1200, class_agreement: [{ value_key: "reform", display_label: "Reform", source_counts: sources.map((source) => ({ source_id: source.id, document_count: 3000 })), all_selected_count: 2800, any_selected_count: 3100, jaccard: 0.903 }] },
  };
}

describe("ReviewProgramQAPopulationStatisticsPage", () => {
  it("renders the population analysis and adds optional classifier and human sources", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => response(path.includes("include_classifier=true"), path.includes("include_human=true")) as never);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><ReviewProgramQAPopulationStatisticsPage clientId="client-1" matterId="matter-1" programId="program-1" runId="run-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Q&A reviewed population statistics" })).toBeInTheDocument();
    expect(await screen.findByText("5,200")).toBeInTheDocument();
    expect(screen.getByText("Coding counts")).toBeInTheDocument();
    expect(screen.getByText("Pairwise agreement")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Review Program" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/review-programs?program=program-1&run=run-1");

    await userEvent.click(screen.getByRole("checkbox", { name: /Insurance classifier/ }));
    await waitFor(() => expect(coreApi).toHaveBeenLastCalledWith(expect.stringContaining("include_classifier=true")));
    expect(await screen.findAllByText("Insurance classifier")).not.toHaveLength(0);

    await userEvent.click(screen.getByRole("checkbox", { name: /Human reviewer decisions/ }));
    await waitFor(() => expect(coreApi).toHaveBeenLastCalledWith(expect.stringContaining("include_human=true")));
    expect(await screen.findAllByText("Human reviewer decisions")).not.toHaveLength(0);
  });
});
