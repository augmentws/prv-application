import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClassificationScoringRunResults } from "@/components/classification-scoring-run-results";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ClassificationScoringRunResults", () => {
  it("shows whole-matter outcome and per-class statistics", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models/model-1") return {
        id: "model-1",
        name: "Responsiveness model",
      } as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-1") return {
        id: "scoring-1",
        model_version_id: "version-12345678",
        dataset_scope_type: "MATTER",
        status: "COMPLETED",
        population_document_count: 100,
        scored_document_count: 96,
        abstained_document_count: 2,
        excluded_document_count: 1,
        failed_document_count: 1,
        prediction_artifact_id: "artifact-1",
        created_at: "2026-10-29T10:00:00Z",
      } as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-1/prediction-summary") return {
        scoring_run_id: "scoring-1",
        model_version_id: "version-12345678",
        ordered_class_keys: ["responsive", "not_responsive"],
        minimum_probability: null,
        total_document_count: 100,
        evaluated_document_count: 96,
        predicted_document_count: 94,
        no_prediction_document_count: 2,
        predicted_value_count: 94,
        abstained_document_count: 2,
        class_counts: [
          { class_key: "responsive", label: "Responsive", document_count: 34 },
          { class_key: "not_responsive", label: "Not responsive", document_count: 60 },
        ],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}><ClassificationScoringRunResults clientId="client-1" matterId="matter-1" modelId="model-1" scoringRunId="scoring-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Responsiveness model scoring results" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to classifier" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/classification-models/model-1",
    );
    expect(await screen.findByText("Predictions by class")).toBeInTheDocument();
    expect(screen.getByText(/Responsive: 34 documents/)).toBeInTheDocument();
    expect(screen.getByText(/Not responsive: 60 documents/)).toBeInTheDocument();
    expect(screen.getAllByText("94").length).toBeGreaterThanOrEqual(2);
  });
});
