import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClassificationPanel } from "@/components/classification-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ClassificationPanel", () => {
  it("shows governed backend state and loads a bounded prediction sample", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models") return [{
        id: "model-1",
        matter_id: "matter-1",
        name: "Responsiveness classifier",
        status: "ACTIVE",
        current_configuration_id: "configuration-1",
        active_model_version_id: "version-1",
        target_field: { display_name: "Responsiveness", stable_key: "responsiveness", metadata_type: "ENUM", cardinality: "SINGLE", active_value_keys: ["not_responsive", "responsive"] },
        current_configuration: { revision: 1 },
      }] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/dataset-snapshots") return [{
        id: "snapshot-1",
        status: "READY_FOR_TRAINING",
        population_count: 100,
        labeled_count: 80,
        training_eligible_count: 80,
        failure_summary: null,
      }] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/builds") return [] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/versions") return [{
        id: "version-1",
        version_number: 1,
        status: "APPROVED",
        metric_summary: { macro_f1: 0.91 },
      }] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs") return [{
        id: "score-1",
        status: "COMPLETED",
        scored_document_count: 98,
        abstained_document_count: 2,
        excluded_document_count: 0,
        failed_document_count: 0,
      }] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/score-1/predictions?offset=0&limit=25") return {
        scoring_run_id: "score-1",
        model_version_id: "version-1",
        ordered_class_keys: ["not_responsive", "responsive"],
        total: 1,
        offset: 0,
        limit: 25,
        items: [{
          matter_document_id: "document-12345678",
          collection_item_id: "item-1",
          scope_ordinal: 1,
          source_vector_content_hash: "a".repeat(64),
          result_status: "SUCCESS",
          failure_reason: null,
          probabilities: [0.1, 0.9],
          predicted_class_keys: ["responsive"],
          abstained: false,
          probability_margin: 0.8,
          uncertainty: 0.1,
        }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><ClassificationPanel matterId="matter-1" definitions={[]} reviewBatches={[]} embeddingJobs={[]} /></QueryClientProvider>);

    expect(await screen.findByText("Responsiveness classifier · Responsiveness")).toBeInTheDocument();
    expect(screen.getByText("Active version: version-")).toBeInTheDocument();
    expect(await screen.findByText("98 scored · 2 abstained · 0 excluded · 0 failed")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Inspect predictions" }));

    expect(await screen.findByText("Prediction sample")).toBeInTheDocument();
    expect(screen.getByText("document")).toBeInTheDocument();
    expect(screen.getAllByText("responsive").length).toBeGreaterThan(0);
    expect(screen.getByText("not_responsive: 0.100 · responsive: 0.900")).toBeInTheDocument();
    expect(screen.getByText("Rows 1–1 of 1")).toBeInTheDocument();
  });
});
