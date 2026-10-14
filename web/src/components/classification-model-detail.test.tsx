import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClassificationModelDetail } from "@/components/classification-model-detail";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ClassificationModelDetail", () => {
  it("shows only the newest attempt as the current processing status", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models/model-1") return {
        id: "model-1",
        name: "Responsiveness model",
        status: "ACTIVE",
        target_field: { display_name: "Responsiveness" },
        current_configuration: { classification_strategy: "ONE_VS_REST" },
      } as never;
      if (path.endsWith("/training-jobs")) return [
        {
          id: "job-current1",
          status: "FAILED",
          current_stage: "FAILED",
          progress_completed: 1,
          progress_total: 4,
          created_at: "2026-10-06T18:08:00Z",
          updated_at: "2026-10-06T18:25:00Z",
          failure_code: "INSUFFICIENT_CLASS_SUPPORT_PER_FOLD",
          failure_summary: "Class rare cannot meet minimum support in every group-aware fold",
          failure_retriable: false,
          dataset_snapshot: null,
          training_run: null,
        },
        {
          id: "job-previous",
          status: "FAILED",
          current_stage: "FAILED",
          progress_completed: 1,
          progress_total: 4,
          created_at: "2026-10-06T18:04:00Z",
          updated_at: "2026-10-06T18:06:00Z",
          failure_code: "CLASSIFICATION_TRAINING_JOB_FAILED",
          failure_summary: "DBOS retries exhausted",
          failure_retriable: true,
          dataset_snapshot: null,
          training_run: null,
        },
      ] as never;
      if (path.endsWith("/builds") || path.endsWith("/versions")) return [] as never;
      if (path.endsWith("/scoring-runs") || path === "/v1/matters/matter-1/review-batches") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <ClassificationModelDetail clientId="client-1" matterId="matter-1" modelId="model-1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Training job job-curr")).toBeInTheDocument();
    expect(screen.getByText("Class rare cannot meet minimum support in every group-aware fold")).toBeInTheDocument();
    expect(screen.getByText("Previous attempts (1)")).toBeInTheDocument();
    expect(screen.queryByText("Training job job-prev")).not.toBeVisible();
  });

  it("shows completed training quality and its confusion matrix", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/classification-models/model-1") return {
        id: "model-1",
        name: "Responsiveness model",
        status: "ACTIVE",
        target_field: { display_name: "Responsiveness" },
        current_configuration: { classification_strategy: "BINARY" },
      } as never;
      if (path.endsWith("/training-jobs")) return [{
        id: "job-12345678",
        status: "COMPLETED",
        current_stage: "COMPLETED",
        progress_completed: 4,
        progress_total: 4,
        updated_at: "2026-10-05T12:05:00Z",
        failure_code: null,
        failure_summary: null,
        failure_retriable: false,
        dataset_snapshot: {
          population_count: 30,
          labeled_count: 30,
          feature_ready_count: 30,
          training_eligible_count: 30,
          excluded_classes: [],
        },
        training_run: {
          status: "COMPLETED",
          current_stage: "COMPLETED",
          progress_completed: 5,
          progress_total: 5,
        },
      }] as never;
      if (path.endsWith("/builds")) return [{
        id: "run-12345678",
        status: "COMPLETED",
        current_stage: "COMPLETED",
        random_seed: 42,
        validation_specification: { fold_count: 5 },
        created_at: "2026-10-05T12:00:00Z",
        failure_summary: null,
      }] as never;
      if (path.endsWith("/versions")) return [{
        id: "version-1",
        version_number: 1,
        status: "CANDIDATE",
        created_at: "2026-10-05T12:05:00Z",
        excluded_classes: [{
          class_key: "rare",
          reason_code: "INSUFFICIENT_POSITIVE_DOCUMENTS",
          message: "Class rare does not have enough feature-ready positive documents.",
          feature_ready_positive_count: 2,
          positive_group_count: 2,
          feature_ready_negative_count: 28,
          negative_group_count: 20,
          required_positive_count: 10,
          required_group_count: 5,
          fold_support: null,
          fold_negative_support: null,
        }],
      }] as never;
      if (path.endsWith("/versions/version-1/evaluation")) return {
        classification_strategy: "BINARY",
        ordered_class_keys: ["not_responsive", "responsive"],
        metrics: { accuracy: 0.9, macro_f1: 0.89 },
        per_class_metrics: {
          not_responsive: { positive_support: 12, precision: 0.91, recall: 0.83, f1: 0.87 },
          responsive: { positive_support: 18, precision: 0.89, recall: 0.94, f1: 0.91, pr_auc: 0.96 },
        },
        confusion_matrix: [[10, 2], [1, 17]],
        warnings: [],
      } as never;
      if (path.endsWith("/scoring-runs") || path === "/v1/matters/matter-1/review-batches") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <QueryClientProvider client={client}>
        <ClassificationModelDetail clientId="client-1" matterId="matter-1" modelId="model-1" />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: /Responsiveness model/ })).toBeInTheDocument();
    expect(screen.getByText("Not activated")).toBeInTheDocument();
    expect(screen.queryByText("ACTIVE")).not.toBeInTheDocument();
    expect(await screen.findByText("Training job job-1234")).toBeInTheDocument();
    expect(screen.getByText("Runtime stage 5 of 5")).toBeInTheDocument();
    expect(await screen.findByText("Macro F1")).toBeInTheDocument();
    expect(screen.getAllByText("0.890").length).toBeGreaterThan(0);
    expect(screen.getByText("Actual ↓ / Predicted →")).toBeInTheDocument();
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("1 class excluded from this training run")).toBeInTheDocument();
    expect(screen.getByText(/2 positives \/ 28 negatives in 2 \/ 20 leakage-safe groups/)).toBeInTheDocument();
  });

  it("previews and activates an approved model version", async () => {
    const user = userEvent.setup();
    const approvedVersion = {
      id: "version-approved",
      version_number: 1,
      status: "APPROVED",
      created_at: "2026-10-05T12:05:00Z",
      trained_class_keys: ["responsive"],
      ordered_class_keys: ["responsive"],
      excluded_classes: [],
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/classification-models/model-1") return {
        id: "model-1",
        name: "Responsiveness model",
        status: "ACTIVE",
        active_model_version_id: null,
        target_field: { display_name: "Responsiveness" },
        current_configuration: { classification_strategy: "BINARY" },
      } as never;
      if (path.endsWith("/training-jobs") || path.endsWith("/builds") || path.endsWith("/scoring-runs") || path === "/v1/matters/matter-1/review-batches") return [] as never;
      if (path.endsWith("/versions")) return [approvedVersion] as never;
      if (path.endsWith("/versions/version-approved/evaluation")) return {
        classification_strategy: "BINARY",
        ordered_class_keys: ["responsive"],
        metrics: {},
        per_class_metrics: {},
        confusion_matrix: [],
        warnings: [],
      } as never;
      if (path.endsWith("/activation/preview") && init?.method === "POST") return {
        action: "ACTIVATION",
        classification_model_id: "model-1",
        model_version: approvedVersion,
        current_active_model_version_id: null,
        expected_model_row_version: 3,
        preview_token: "a".repeat(64),
        can_promote: true,
        blocking_reasons: [],
        policy_snapshot: {},
        metric_snapshot: {},
      } as never;
      if (path.endsWith("/activation") && init?.method === "POST") return {
        action: "ACTIVATION",
        workflow_id: "activation-1",
        status: "COMPLETED",
        classification_model_id: "model-1",
        model_version_id: "version-approved",
        model: {
          id: "model-1",
          name: "Responsiveness model",
          status: "ACTIVE",
          active_model_version_id: "version-approved",
          target_field: { display_name: "Responsiveness" },
          current_configuration: { classification_strategy: "BINARY" },
        },
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><ClassificationModelDetail clientId="client-1" matterId="matter-1" modelId="model-1" /></QueryClientProvider>);

    await user.type(await screen.findByLabelText("Decision reason"), "Quality review passed");
    await user.click(screen.getByRole("button", { name: "Review activation" }));
    expect(await screen.findByRole("heading", { name: "Activate model version?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Activate version" }));

    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/classification-models/model-1/activation",
      expect.objectContaining({ method: "POST" }),
    ));
    const activationCall = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/activation"));
    expect(JSON.parse(String(activationCall?.[1]?.body))).toMatchObject({
      model_version_id: "version-approved",
      expected_model_row_version: 3,
      reason: "Quality review passed",
    });
  });

  it("creates an immutable confidence-window projection from a completed scoring run", async () => {
    const user = userEvent.setup();
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/classification-models/model-1") return {
        id: "model-1",
        name: "Issue model",
        status: "ACTIVE",
        target_metadata_definition_id: "definition-1",
        target_field: { display_name: "Issue Coding" },
        current_configuration: { classification_strategy: "ONE_VS_REST" },
      } as never;
      if (path.endsWith("/training-jobs") || path.endsWith("/builds")) return [] as never;
      if (path.endsWith("/versions")) return [{
        id: "version-1",
        version_number: 1,
        status: "ACTIVE",
        created_at: "2026-10-05T12:05:00Z",
        trained_class_keys: ["topic_401", "topic_402"],
        ordered_class_keys: ["topic_401", "topic_402"],
        excluded_classes: [],
      }] as never;
      if (path.endsWith("/versions/version-1/evaluation")) return {
        classification_strategy: "ONE_VS_REST",
        ordered_class_keys: ["topic_401", "topic_402"],
        metrics: {},
        per_class_metrics: {},
        confusion_matrix: [],
        warnings: [],
      } as never;
      if (path.endsWith("/scoring-runs")) return [{
        id: "score-1",
        model_version_id: "version-1",
        dataset_scope_type: "MATTER",
        dataset_scope_id: "matter-1",
        status: "COMPLETED",
        population_document_count: 100,
        scored_document_count: 98,
        abstained_document_count: 2,
        excluded_document_count: 0,
        failed_document_count: 0,
        created_at: "2026-10-06T12:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [{
        id: "batch-1",
        name: "All matter review",
        selection_type: "ALL_MATTER",
        status: "READY",
        search_status: "READY",
        document_count: 100,
        coding_groups: [{ fields: [{ metadata_definition_id: "definition-1" }] }],
      }] as never;
      if (path.endsWith("/review-batches/batch-1/classifier-runs") && init?.method === "POST") return {
        id: "projection-1",
        review_batch_id: "batch-1",
      } as never;
      if (path.endsWith("/review-batches/batch-1/runs") && !init?.method) return [{
        id: "projection-1",
        review_batch_id: "batch-1",
        status: "COMPLETED",
        created_at: "2026-10-06T12:10:00Z",
        result_summary: { projected_document_count: 20, projected_value_count: 21, failed_document_count: 1 },
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(<QueryClientProvider client={client}><ClassificationModelDetail clientId="client-1" matterId="matter-1" modelId="model-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "Create projection" }));
    await user.click(screen.getByRole("combobox", { name: "Review batch" }));
    await user.click(screen.getByRole("option", { name: /All matter review/ }));
    await user.clear(screen.getByLabelText("Minimum confidence %"));
    await user.type(screen.getByLabelText("Minimum confidence %"), "80");
    await user.type(screen.getByLabelText("Maximum confidence %"), "90");
    await user.click(screen.getByRole("checkbox", { name: "topic_401" }));
    await user.click(screen.getByRole("button", { name: "Create projection" }));

    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/batch-1/classifier-runs",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/classifier-runs"));
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      classification_scoring_run_id: "score-1",
      minimum_confidence: 0.8,
      maximum_confidence: 0.9,
      selected_class_keys: ["topic_401"],
    });
    expect(await screen.findByText("Projection activity")).toBeInTheDocument();
    expect(await screen.findByText("20")).toBeInTheDocument();
  });
});
