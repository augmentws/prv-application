import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobsSection } from "@/components/matter/sections/jobs/jobs-section";
import { MATTER, MATTER_BASE_PATH, renderInMatter } from "@/components/matter/test-utils";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "", push: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push, replace: vi.fn() }), useSearchParams: () => navigation.params }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.params = new URLSearchParams();
  window.localStorage.clear();
});

describe("Matter jobs", () => {
  it("shows a snapshotted zero-progress bulk tag job as waiting and links each job type to its own page", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
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
    navigation.pathname = `${MATTER_BASE_PATH}/jobs/bulk-tagging`;
    const view = renderInMatter(<JobsSection jobType="BULK_TAGGING" />);

    expect(await screen.findByRole("heading", { name: "Bulk tagging" })).toBeInTheDocument();
    expect(screen.getByText("Responsiveness: Responsive")).toBeInTheDocument();
    expect(screen.getByText("“price coordination” + 1 filter")).toBeInTheDocument();
    expect(screen.getByText("waiting")).toBeInTheDocument();
    expect(screen.getByText("Waiting for capacity")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Topic clustering" })).not.toBeInTheDocument();

    const jobTypes = within(screen.getByRole("navigation", { name: "Job types" }));
    expect(jobTypes.getByRole("link", { name: "Bulk tagging" })).toHaveAttribute("aria-current", "page");
    expect(jobTypes.getByRole("link", { name: "Topic clustering" })).toHaveAttribute("href", `${MATTER_BASE_PATH}/jobs/topic-clustering`);
    expect(jobTypes.getAllByRole("link")).toHaveLength(7);
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Jobs")).toHaveAttribute("aria-current", "page");

    navigation.pathname = `${MATTER_BASE_PATH}/jobs/topic-clustering`;
    view.rerender(<JobsSection jobType="TOPIC_CLUSTERING" />);

    expect(await screen.findByRole("heading", { name: "Topic clustering" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Job types" })).getByRole("link", { name: "Topic clustering" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("heading", { name: "Bulk tagging" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-1");
    expect(screen.getByRole("link", { name: "View topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-2");
  });

  it("shows classification analysis as a job type and retries a canceled whole-matter run", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
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
    const user = userEvent.setup();

    navigation.pathname = `${MATTER_BASE_PATH}/jobs/classification-analysis`;
    renderInMatter(<JobsSection jobType="CLASSIFICATION_ANALYSIS" />);

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
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/jobs/ANALYSIS_TASK_BATCH_RUN/qa-run-1/failed-documents?offset=0&limit=100") return {
        items: [], total: 5, retryable_count: 5, offset: 0, limit: 100,
      } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["job.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [] as never;
      if (path === "/v1/jobs/ANALYSIS_TASK_BATCH_RUN/qa-run-2/cancel" && init?.method === "POST") return { id: "execution-2", status: "CANCELED" } as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/question-answering-jobs?batch_set_id=set-1") return [{
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
      }, {
        id: "qa-run-2",
        review_batch_id: "batch-2",
        review_batch_name: "Discovery batch 2",
        matter_analysis_task_id: "task-2",
        task_name: "Responsiveness review",
        task_version: 1,
        decision_provider: "JEV",
        model_key: "jev-latest",
        status: "RUNNING",
        processed_document_count: 125,
        successful_document_count: 125,
        document_count: 500,
        failed_document_count: 0,
        retryable_failed_document_count: 0,
        error_message: null,
        started_at: "2026-10-12T12:02:00Z",
        completed_at: null,
        created_at: "2026-10-12T12:01:00Z",
        updated_at: "2026-10-12T12:03:00Z",
        execution: {
          attempt_number: 1,
          stage_label: "Evaluating documents",
          progress_completed: 125,
          progress_total: 500,
          available_actions: ["CANCEL"],
        },
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    navigation.pathname = `${MATTER_BASE_PATH}/jobs/question-answering`;
    renderInMatter(<JobsSection jobType="QUESTION_ANSWERING" batchSetId="set-1" />);

    expect(await screen.findByRole("heading", { name: "Question answering" })).toBeInTheDocument();
    expect(screen.getByText("Privilege review")).toBeInTheDocument();
    expect(screen.getByText("Published v3")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Privilege validation" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.getByText("OpenAI Decisions")).toBeInTheDocument();
    expect(screen.getByText("495 / 500 successful")).toBeInTheDocument();
    expect(screen.getByText("Showing linked Batch Set jobs")).toBeInTheDocument();
    expect(screen.getByText("Only Q&A jobs created from Batch Set set-1 are shown.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Show all Q&A jobs" })).toHaveAttribute("href", `${MATTER_BASE_PATH}/jobs/question-answering`);
    expect(screen.queryByText("Completed With Errors · attempt 2")).not.toBeInTheDocument();
    expect(screen.queryByText("5 documents could not be evaluated")).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open batch" })[0]).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.getByRole("button", { name: "Review errors for Privilege review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "More actions for Responsiveness review on Discovery batch 2" }));
    await user.click(await screen.findByRole("menuitem", { name: "Cancel" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/jobs/ANALYSIS_TASK_BATCH_RUN/qa-run-2/cancel",
      { method: "POST" },
    ));
    await user.click(screen.getByRole("button", { name: "More actions for Privilege review on Privilege validation" }));
    expect(await screen.findByRole("menuitem", { name: "Delete job" })).toBeInTheDocument();
    await user.click(await screen.findByRole("menuitem", { name: "View errors" }));
    expect(await screen.findByRole("heading", { name: "Failed documents" })).toBeInTheDocument();
    expect(screen.getByText("Privilege review on Privilege validation. Successful documents will not run again.")).toBeInTheDocument();
  });

  it("reviews completed classification output on the job with a probability threshold", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
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
    const user = userEvent.setup();

    navigation.pathname = `${MATTER_BASE_PATH}/jobs/classification-analysis`;
    renderInMatter(<JobsSection jobType="CLASSIFICATION_ANALYSIS" selectedJobId="run-1" />);

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
