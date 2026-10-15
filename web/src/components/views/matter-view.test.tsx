import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterView } from "@/components/views/matter-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("MatterView jobs", () => {
  it("shows a snapshotted zero-progress bulk tag job as waiting", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["job.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [{
        id: "definition-1",
        display_name: "Responsiveness",
        type: "ENUM",
        allowed_values: [{ key: "responsive", label: "Responsive", active: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [{
        id: "topic-job-1",
        operating_mode: "AUTO",
        requested_topic_count: null,
        sample_size: 1000,
        assignment_mode: "APPEND",
        status: "AWAITING_REVIEW",
        document_count: 100,
        processed_document_count: 0,
        topic_count: 2,
        assigned_document_count: 0,
        failed_count: 0,
        created_at: "2026-09-24T11:00:00Z",
        clusters: [],
      }, {
        id: "topic-job-2",
        operating_mode: "AUTO",
        requested_topic_count: null,
        sample_size: 1000,
        assignment_mode: "APPEND",
        status: "COMPLETED",
        document_count: 100,
        processed_document_count: 100,
        topic_count: 1,
        assigned_document_count: 95,
        failed_count: 0,
        created_at: "2026-09-24T10:00:00Z",
        clusters: [{ id: "cluster-1", name: "Pricing" }],
      }] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [{
        id: "bulk-1",
        metadata_definition_id: "definition-1",
        value: "responsive",
        assignments: [{ metadata_definition_id: "definition-1", value: "responsive" }],
        search_definition: { query: "price coordination", filters: [{ field: "custodian" }] },
        status: "RUNNING",
        processed_count: 0,
        matched_count: 100,
        tagged_count: 0,
        failed_count: 0,
        error_message: null,
        created_at: "2026-09-24T12:00:00Z",
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="jobs" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Bulk tagging" })).toBeInTheDocument();
    expect(screen.getByText("Responsiveness: Responsive")).toBeInTheDocument();
    expect(screen.getByText("“price coordination” + 1 filter")).toBeInTheDocument();
    expect(screen.getByText("waiting")).toBeInTheDocument();
    expect(screen.getByText("Waiting for capacity")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Topic clustering" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "Job type" }));
    await user.click(screen.getByRole("option", { name: "Topic clustering" }));

    expect(screen.getByRole("heading", { name: "Topic clustering" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Bulk tagging" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-1");
    expect(screen.getByRole("link", { name: "View topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-2");
    expect(window.localStorage.getItem("priv-view:matter-jobs:selected-type")).toBe("TOPIC_CLUSTERING");
  });

  it("shows classification analysis as a job type and retries a canceled whole-matter run", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["job.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks") return [{ id: "task-1", name: "Whole matter classifier", task_type: "CLASSIFICATION" }] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/runs") return [{
        id: "run-1",
        scope_type: "MATTER",
        status: "CANCELED",
        current_stage: "CANCELED",
        progress_completed: 2,
        progress_total: 4,
        population_document_count: 290099,
        failure_summary: null,
        created_at: "2026-10-09T12:00:00Z",
        execution: {
          attempt_number: 1,
          stage_label: "Canceled",
          progress_completed: 2,
          progress_total: 4,
          failure_summary: null,
          available_actions: ["RETRY"],
        },
      }] as never;
      if (path === "/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/run-1/retry" && init?.method === "POST") return { id: "execution-2", status: "QUEUED" } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="jobs" /></QueryClientProvider>);

    await screen.findByRole("heading", { name: "Bulk tagging" });
    await user.click(screen.getByRole("combobox", { name: "Job type" }));
    await user.click(screen.getByRole("option", { name: "Classification analysis" }));

    expect(await screen.findByRole("heading", { name: "Classification analysis" })).toBeInTheDocument();
    expect(screen.getByText("Whole matter classifier")).toBeInTheDocument();
    expect(screen.getByText("Whole matter")).toBeInTheDocument();
    expect(screen.getByText("290,099 documents frozen")).toBeInTheDocument();
    expect(screen.getByText("Canceled · attempt 1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/run-1/retry",
      { method: "POST" },
    ));
  });

  it("shows question-answering analysis runs as a matter job type", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/jobs/ANALYSIS_TASK_BATCH_RUN/qa-run-1/failed-documents?offset=0&limit=100") return {
        items: [], total: 5, retryable_count: 5, offset: 0, limit: 100,
      } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["job.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/question-answering-jobs") return [{
        id: "qa-run-1",
        review_batch_id: "batch-1",
        review_batch_name: "Privilege validation",
        matter_analysis_task_id: "task-1",
        task_name: "Privilege review",
        task_version: 3,
        decision_provider: "OPENAI_DECISIONS",
        model_key: "gpt-6-luna",
        status: "COMPLETED_WITH_ERRORS",
        processed_document_count: 500,
        successful_document_count: 495,
        document_count: 500,
        failed_document_count: 5,
        retryable_failed_document_count: 5,
        error_message: "5 documents could not be evaluated",
        started_at: "2026-10-12T12:00:00Z",
        completed_at: null,
        created_at: "2026-10-12T11:59:00Z",
        updated_at: "2026-10-12T12:01:00Z",
        execution: {
          attempt_number: 2,
          stage_label: "Completed With Errors",
          progress_completed: 5,
          progress_total: 5,
          available_actions: [],
        },
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="jobs" /></QueryClientProvider>);

    await screen.findByRole("heading", { name: "Bulk tagging" });
    await user.click(screen.getByRole("combobox", { name: "Job type" }));
    await user.click(screen.getByRole("option", { name: "Question answering" }));

    expect(await screen.findByRole("heading", { name: "Question answering" })).toBeInTheDocument();
    expect(screen.getByText("Privilege review")).toBeInTheDocument();
    expect(screen.getByText("Published v3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Privilege validation" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.getByText("OpenAI Decisions")).toBeInTheDocument();
    expect(screen.getByText("495 / 500 successful")).toBeInTheDocument();
    expect(screen.queryByText("Completed With Errors · attempt 2")).not.toBeInTheDocument();
    expect(screen.queryByText("5 documents could not be evaluated")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open batch" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.getByRole("button", { name: "Review errors for Privilege review" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for Privilege review on Privilege validation" }));
    expect(await screen.findByRole("menuitem", { name: "Delete job" })).toBeInTheDocument();
    await user.click(await screen.findByRole("menuitem", { name: "View errors" }));
    expect(await screen.findByRole("heading", { name: "Failed documents" })).toBeInTheDocument();
    expect(screen.getByText("Privilege review on Privilege validation. Successful documents will not run again.")).toBeInTheDocument();
  });

  it("reviews completed classification output on the job with a probability threshold", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["job.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks") return [{ id: "task-1", name: "Issue classifier", task_type: "CLASSIFICATION" }] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/runs") return [{
        id: "run-1",
        task_name: "Issue classifier",
        scope_type: "MATTER",
        classification_model_id: "model-1",
        classification_scoring_run_id: "scoring-run-1",
        status: "COMPLETED",
        current_stage: "COMPLETED",
        progress_completed: 4,
        progress_total: 4,
        population_document_count: 290099,
        feature_ready_count: 287080,
        feature_missing_count: 3019,
        feature_failed_count: 0,
        failure_summary: null,
        failure_retriable: false,
        created_at: "2026-10-09T12:00:00Z",
        execution: null,
      }] as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-run-1") return {
        id: "scoring-run-1",
        status: "COMPLETED",
        scored_document_count: 287080,
        abstained_document_count: 0,
        excluded_document_count: 3019,
        failed_document_count: 0,
        prediction_artifact_id: "prediction-artifact-1",
        scoring_policy: { one_vs_rest_thresholds: { topic_401: 0.5, topic_402: 0.5 } },
      } as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-run-1/prediction-summary") return {
        scoring_run_id: "scoring-run-1",
        model_version_id: "model-version-1",
        ordered_class_keys: ["topic_401", "topic_402"],
        minimum_probability: null,
        total_document_count: 290099,
        evaluated_document_count: 287080,
        predicted_document_count: 107962,
        no_prediction_document_count: 179118,
        predicted_value_count: 110126,
        abstained_document_count: 0,
        class_counts: [
          { class_key: "topic_401", label: "Olympics", document_count: 12000 },
          { class_key: "topic_402", label: "FEMA", document_count: 9000 },
        ],
      } as never;
      if (path === "/v1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-run-1/prediction-summary?minimum_probability=0.80") return {
        scoring_run_id: "scoring-run-1",
        model_version_id: "model-version-1",
        ordered_class_keys: ["topic_401", "topic_402"],
        minimum_probability: 0.8,
        total_document_count: 290099,
        evaluated_document_count: 287080,
        predicted_document_count: 7000,
        no_prediction_document_count: 280080,
        predicted_value_count: 8000,
        abstained_document_count: 0,
        class_counts: [
          { class_key: "topic_401", label: "Olympics", document_count: 5000 },
          { class_key: "topic_402", label: "FEMA", document_count: 3000 },
        ],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView
      clientId="client-1"
      matterId="matter-1"
      requestedTab="jobs"
      requestedJobType="CLASSIFICATION_ANALYSIS"
      selectedJobId="run-1"
    /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Issue classifier" })).toBeInTheDocument();
    expect(await screen.findByText("Scoring output")).toBeInTheDocument();
    expect(await screen.findByText("Predictions by class")).toBeInTheDocument();
    expect(screen.getByText(/110,126/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Prediction confidence threshold" }));
    await user.click(screen.getByRole("option", { name: "Probability ≥ 80%" }));
    expect(await screen.findByText(/each class probability is at least 80%/)).toBeInTheDocument();
    expect(screen.getByText(/8,000/)).toBeInTheDocument();
  });
});

describe("MatterView batches", () => {
  it("uses a split Open review action with Statistics in its dropdown", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["batch.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/definition-assessments") return [] as never;
      if (path === "/v1/matters/matter-1/access-users?permission_key=batch.review") return [] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [{
        id: "batch-1",
        matter_id: "matter-1",
        name: "Privilege validation",
        description: "First-pass privilege review",
        selection_type: "ALL_MATTER",
        selection_definition: {},
        document_count: 2500,
        coding_groups: [],
        assigned_user_id: null,
        status: "READY",
        execution: null,
        error_message: null,
        search_error_message: null,
        created_at: "2026-10-13T12:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1" && init?.method === "DELETE") return undefined as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="batches" /></QueryClientProvider>);

    expect(await screen.findByText("Privilege validation")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open review" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.queryByRole("button", { name: "Statistics" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "More actions for Privilege validation" }));
    expect(await screen.findByRole("menuitem", { name: "Delete batch" })).toBeInTheDocument();
    const statistics = await screen.findByRole("menuitem", { name: "Statistics" });
    expect(statistics).toHaveAttribute("href", "/review/matters/matter-1/batches/batch-1/statistics");
    expect(statistics).toHaveAttribute("target", "_blank");
    await user.click(await screen.findByRole("menuitem", { name: "Delete batch" }));
    expect(await screen.findByRole("dialog", { name: "Delete batch?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete batch" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/batch-1",
      { method: "DELETE" },
    ));
  });
});

describe("MatterView metadata navigation", () => {
  it("does not expose a manual Email Domain update action", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="metadata" /></QueryClientProvider>);

    expect(await screen.findByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "Update email domains" })).not.toBeInTheDocument();
  });

  it("shows one Metadata tab with Definitions and Groups subtabs and preserves the legacy groups link", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="groups" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Metadata groups" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Metadata" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Groups" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Metadata definitions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Metadata groups" })).not.toBeInTheDocument();
  });

  it("allows an administrator to make an asserted field AI-assignable", async () => {
    const definition = {
      id: "definition-1",
      matter_id: "matter-1",
      key: "issue_coding",
      display_name: "Issue coding",
      description: "Matter issue labels",
      type: "ENUM",
      cardinality: "MULTIPLE",
      allowed_values: [{ key: "topic_401", label: "Olympics", description: null, active: true }],
      value_source: "ASSERTED",
      reference_target: null,
      template_key: null,
      template_version: null,
      assertion_policy: "IMMEDIATE",
      resolution_policy: "EXPLICIT_ONLY",
      searchable: true,
      facetable: true,
      normalize_to_lowercase: false,
      hierarchy_separator: null,
      reviewable: true,
      ai_assignable: false,
      status: "ACTIVE",
      created_at: "2026-10-07T12:00:00Z",
    } as const;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions/definition-1" && init?.method === "PATCH") return { ...definition, ai_assignable: true } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [definition] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="metadata" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit Issue coding" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Controlled values" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Agents may assign values"));
    await user.click(screen.getByRole("button", { name: "Save field settings" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1",
      { method: "PATCH", body: JSON.stringify({ ai_assignable: true }) },
    ));
    expect(await screen.findByText("agent")).toBeInTheDocument();
  });
});

describe("MatterView token usage", () => {
  it("shows matter totals, model and workflow breakdowns, and the usage ledger", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/provider-usage?offset=0&limit=100") return {
        matter_id: "matter-1",
        totals: { record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 },
        by_model: [{ provider: "google", model: "gemini-test", record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        by_job_type: [{ job_type: "MATTER_ANALYSIS_TASK_BATCH", record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        entries: [{ id: "usage-1", job_id: "job-1", job_type: "MATTER_ANALYSIS_TASK_BATCH", job_created_at: "2026-09-26T12:00:00Z", provider: "google", model: "gemini-test", started_by_display_name: "Review Manager", started_by_email: "manager@example.com", input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        entries_offset: 0,
        entries_limit: 100,
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="usage" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Token usage" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Token usage" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByText("1,545").length).toBeGreaterThan(0);
    expect(screen.getAllByText("gemini-test").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Question answering").length).toBeGreaterThan(0);
    expect(screen.getByText("Review Manager")).toBeInTheDocument();
    expect(screen.getByText(/does not yet preserve a versioned provider price snapshot/i)).toBeInTheDocument();
  });
});
