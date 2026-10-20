import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisRunSelect, BatchDecisionAnalysisPanel, BatchReviewWorkspace, DisagreementReviewPanel } from "@/components/batch-review-workspace";
import { ClassificationAnalysisJobDetail } from "@/components/classification-analysis-job-detail";
import type { MatterAnalysisTaskRunRead, ReviewBatchAnalysisJobRead, ReviewBatchDisagreementDocumentRead, ReviewBatchRead, ReviewBatchRunRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/document-viewer-dialog", () => ({
  DocumentViewerSurface: ({ item, highlightedParagraphs = [] }: { item: { original_filename: string }; highlightedParagraphs?: number[] }) => <div>{item.original_filename}<span data-testid="highlighted-paragraphs">{highlightedParagraphs.join(",")}</span></div>,
}));

const citedDocumentId = "2290912f-9d6f-44a1-a133-069f8877a8cd";

const batch = {
  id: "batch-1",
  matter_id: "matter-1",
  name: "First-level review",
  description: "Review sample",
  selection_type: "RANDOM_MATTER",
  selection_definition: {},
  source_batch_id: null,
  search_index_generation_id: null,
  sample_size: 1,
  random_seed: "seed",
  assigned_user_id: "user-1",
  assigned_user: { id: "user-1", display_name: "Reviewer", email: "reviewer@example.com" },
  reviewer_value_visibility: "OWN_VALUES",
  status: "READY",
  search_status: "READY",
  search_error_message: null,
  searchable_coding_run_id: null,
  searchable_coding_status: null,
  searchable_coding_error_message: null,
  searchable_coding_fields: [],
  workflow_id: "review-batch:batch-1",
  document_count: 1,
  error_message: null,
  created_by_user_id: "user-1",
  coding_groups: [{
    id: "group-1",
    source_metadata_group_id: "source-group-1",
    display_name: "Review decisions",
    description: null,
    sort_order: 0,
    fields: [{
      id: "field-1",
      metadata_definition_id: "definition-1",
      sort_order: 0,
      definition_snapshot: {
        key: "responsiveness",
        display_name: "Responsiveness",
        description: "Code whether the document is responsive.",
        type: "ENUM",
        cardinality: "SINGLE",
        allowed_values: [{ key: "responsive", label: "Responsive", active: true }],
      },
    }],
  }],
  created_at: "2026-09-15T12:00:00Z",
  updated_at: "2026-09-15T12:00:00Z",
  completed_at: "2026-09-15T12:00:00Z",
} satisfies ReviewBatchRead;

const analysisTask = {
  id: "task-1",
  matter_id: "matter-1",
  key: "privilege_review",
  name: "Privilege review",
  description: "First-pass privilege review",
  task_type: "QUESTION_ANSWERING",
  workflow_key: "question_answering_v1",
  current_version: 2,
  published_version: 1,
  status: "ACTIVE",
  created_by_user_id: "user-1",
  created_at: "2026-09-26T12:00:00Z",
  updated_at: "2026-09-26T12:10:00Z",
  version: {},
  published_version_details: {
    decision_provider: "OPENAI_DECISIONS",
    decision_model: "gpt-6-luna",
  },
} as const;

const classificationTask = {
  ...analysisTask,
  id: "task-classifier-1",
  key: "issue_classifier",
  name: "Issue classifier",
  description: "Apply the approved issue classifier",
  task_type: "CLASSIFICATION",
  workflow_key: "classification_analysis_task_v1",
  current_version: 1,
  published_version_details: {
    decision_provider: "JEV",
    decision_model: null,
    classification_model_id: "classification-model-1",
    classification_model_version_id: "classification-model-version-1",
    classification_embedding_job_id: "embedding-job-1",
  },
} as const;

const analysisRun = {
  id: "analysis-run-1",
  review_batch_id: "batch-1",
  run_type: "WORKFLOW",
  purpose: "REVIEW",
  status: "COMPLETED",
  result_policy: "ISOLATED",
  parent_run_id: null,
  actor_user_id: null,
  agent_definition_version_id: null,
  workflow_run_record_id: "workflow-run-1",
  configuration_snapshot: {
    mode: "BATCH",
    task_id: "task-1",
    task_name: "Privilege review",
    task_version: 1,
    decision_provider: "OPENAI_DECISIONS",
    model_key: "gpt-6-luna",
    destination_fields: [{ id: "definition-issue", key: "issue_coding", display_name: "Issue Coding" }],
  },
  initiated_by_user_id: "user-1",
  processed_document_count: 0,
  error_message: null,
  started_at: null,
  completed_at: null,
  created_at: "2026-09-27T12:00:00Z",
  updated_at: "2026-09-27T12:00:00Z",
} as const;

const classificationAnalysisRun = {
  id: "classification-analysis-run-1",
  tenant_id: "tenant-1",
  matter_id: "matter-1",
  matter_analysis_task_id: "task-classifier-1",
  matter_analysis_task_version_id: "task-classifier-version-1",
  scope_type: "REVIEW_BATCH",
  scope_id: "batch-1",
  classification_model_id: "classification-model-1",
  classification_model_version_id: "classification-model-version-1",
  embedding_job_id: "embedding-job-1",
  dataset_snapshot_id: "snapshot-1",
  classification_scoring_run_id: "scoring-run-1",
  status: "COMPLETED",
  current_stage: "COMPLETED",
  progress_completed: 4,
  progress_total: 4,
  population_document_count: 100,
  feature_processed_count: 100,
  feature_ready_count: 100,
  feature_missing_count: 0,
  feature_failed_count: 0,
  failure_code: null,
  failure_summary: null,
  failure_retriable: false,
  created_by_user_id: "user-1",
  started_at: "2026-09-28T12:00:00Z",
  completed_at: "2026-09-28T12:05:00Z",
  canceled_at: null,
  created_at: "2026-09-28T12:00:00Z",
  updated_at: "2026-09-28T12:05:00Z",
  execution: null,
} satisfies MatterAnalysisTaskRunRead;

function questionAnsweringJob(run: ReviewBatchRunRead): ReviewBatchAnalysisJobRead {
  return {
    id: run.id,
    job_type: "ANALYSIS_TASK_BATCH_RUN",
    task_type: "QUESTION_ANSWERING",
    matter_analysis_task_id: "task-1",
    task_name: String(run.configuration_snapshot.task_name ?? "Privilege review"),
    task_version: Number(run.configuration_snapshot.task_version ?? 1),
    status: run.status,
    created_at: run.created_at,
    updated_at: run.updated_at,
    execution: run.execution,
    review_batch_run: run,
    classification_analysis_run: null,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BatchReviewWorkspace", () => {
  it("shows every selected disagreement source and saves the chosen coding", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    const disagreement = {
      review_batch_id: "disagreement-batch-1",
      source_batch_id: "batch-1",
      matter_document_id: "document-1",
      metadata_definition_id: "definition-1",
      field_display_name: "Responsiveness",
      field_type: "ENUM",
      cardinality: "SINGLE",
      allowed_values: [
        { key: "responsive", label: "Responsive", active: true },
        { key: "not_responsive", label: "Not responsive", active: true },
      ],
      agreement_status: "DISAGREEMENT",
      sources: [
        {
          id: "reviewer-coding-1",
          source_type: "REVIEWER",
          run_type: null,
          label: "Alex Reviewer",
          detail: "alex@example.com",
          reviewer_revision: 4,
          state: "EVALUATED",
          values: ["responsive"],
          display_values: ["Responsive"],
          top_confidence: null,
        },
        {
          id: "analysis-run-1",
          source_type: "RUN",
          run_type: "WORKFLOW",
          label: "Privilege review task",
          detail: "Published v2 · OpenAI Decisions",
          reviewer_revision: null,
          state: "EVALUATED",
          values: ["not_responsive"],
          display_values: ["Not responsive"],
          top_confidence: 0.91,
        },
      ],
    } satisfies ReviewBatchDisagreementDocumentRead;

    render(<DisagreementReviewPanel
      disagreement={disagreement}
      coding={{ matter_document_id: "document-1", review_status: "NOT_STARTED", values: [], reviewer_values: [] }}
      saving={false}
      onSave={onSave}
      onSkip={vi.fn().mockResolvedValue(undefined)}
    />);

    expect(screen.getByRole("heading", { name: "Disagreements" })).toBeInTheDocument();
    expect(screen.getByText("Responsiveness")).toBeInTheDocument();
    expect(screen.getByText("Alex Reviewer")).toBeInTheDocument();
    const analysisSource = screen.getByText("Privilege review task").closest("article");
    expect(analysisSource).not.toBeNull();
    expect(within(analysisSource!).getByText("91%")).toBeInTheDocument();
    await user.click(within(analysisSource!).getByRole("button", { name: "Use this coding" }));
    expect(within(analysisSource!).getByRole("button", { name: "Selected" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Save & next" }));

    expect(onSave).toHaveBeenCalledWith({
      matter_document_id: "document-1",
      fields: [{ metadata_definition_id: "definition-1", values: ["not_responsive"], confidence: null }],
    });
  });

  it("allows an active analysis run to be canceled", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const runningRun = { ...analysisRun, status: "RUNNING" } as const;

    render(<BatchDecisionAnalysisPanel
      jobs={[questionAnsweringJob(runningRun)]}
      selectedRun={runningRun}
      onSelectRun={vi.fn()}
      documentStatus="IN_PROGRESS"
      resultLoading={false}
      publishing={false}
      onMakeSearchable={vi.fn()}
      onViewSearchableFields={vi.fn()}
      promotionLoading={false}
      onPromote={vi.fn()}
      onRunNew={vi.fn()}
      onRunAgain={vi.fn()}
      onCancel={onCancel}
    />);

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledWith(runningRun.id);
  });

  it("shows canceled documents as terminal and allows the run to be started again", async () => {
    const user = userEvent.setup();
    const onRunAgain = vi.fn();
    const canceledRun = { ...analysisRun, status: "CANCELED" } as const;

    render(<BatchDecisionAnalysisPanel
      jobs={[questionAnsweringJob(canceledRun)]}
      selectedRun={canceledRun}
      onSelectRun={vi.fn()}
      documentStatus="SKIPPED"
      resultLoading={false}
      publishing={false}
      onMakeSearchable={vi.fn()}
      onViewSearchableFields={vi.fn()}
      promotionLoading={false}
      onPromote={vi.fn()}
      onRunNew={vi.fn()}
      onRunAgain={onRunAgain}
    />);

    expect(screen.getByText("Document analysis canceled")).toBeInTheDocument();
    expect(screen.getByText("This document was not evaluated before the run was canceled. Start a new run to analyze it.")).toBeInTheDocument();
    expect(screen.queryByText("The result will appear here when evaluation completes.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run again" }));
    expect(onRunAgain).toHaveBeenCalledWith("task-1");
  });

  it("offers a projection retry without telling the user to rebuild search", async () => {
    const user = userEvent.setup();
    const onMakeSearchable = vi.fn();

    render(<BatchDecisionAnalysisPanel
      jobs={[questionAnsweringJob(analysisRun)]}
      selectedRun={analysisRun}
      onSelectRun={vi.fn()}
      documentStatus="COMPLETED"
      resultLoading={false}
      searchableRunId={analysisRun.id}
      searchableStatus="FAILED"
      searchableError="The projection was paused by an unrelated search schema change. Retry the projection."
      publishing={false}
      onMakeSearchable={onMakeSearchable}
      onViewSearchableFields={vi.fn()}
      promotionLoading={false}
      onPromote={vi.fn()}
      onRunNew={vi.fn()}
      onRunAgain={vi.fn()}
    />);

    expect(screen.getByText("The projection was paused by an unrelated search schema change. Retry the projection.")).toBeInTheDocument();
    expect(screen.queryByText(/rebuild/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry projection" }));
    expect(onMakeSearchable).toHaveBeenCalledWith(analysisRun.id);
  });

  it("allows the displayed analysis run to be changed", async () => {
    const user = userEvent.setup();
    const onSelectRun = vi.fn();
    const newerRun = {
      ...analysisRun,
      id: "analysis-run-newer",
      configuration_snapshot: { ...analysisRun.configuration_snapshot, task_version: 2 },
      created_at: "2026-09-27T12:00:00Z",
    } as const;
    const olderRun = {
      ...analysisRun,
      id: "analysis-run-older",
      configuration_snapshot: { ...analysisRun.configuration_snapshot, task_name: "Earlier analysis" },
      created_at: "2026-09-26T11:00:00Z",
    } as const;

    render(<AnalysisRunSelect jobs={[questionAnsweringJob(newerRun), questionAnsweringJob(olderRun)]} matterDefinitionAssessment={{ runId: "assessment-run", version: 1, createdAt: "2026-09-26T12:00:00Z" }} selectedRunId={newerRun.id} onSelectRun={onSelectRun} />);

    const selector = screen.getByRole("combobox", { name: "Analysis run" });
    expect(selector).toHaveTextContent("published v2");
    await user.click(selector);
    expect(screen.getByRole("option", { name: /Earlier analysis/i })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /Matter Definition assessment.*v1/i }));
    expect(onSelectRun).toHaveBeenCalledWith("assessment-run");
  });

  it("creates a review batch from any completed classification run", async () => {
    const user = userEvent.setup();
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path.endsWith("/classification-models/classification-model-1/scoring-runs/scoring-run-1")) return {
        id: "scoring-run-1",
        classification_model_id: "classification-model-1",
        model_version_id: "classification-model-version-1",
        scoring_policy: { multiclass_minimum_confidence: 0.8 },
        status: "COMPLETED",
        population_document_count: 100,
        scored_document_count: 96,
        abstained_document_count: 4,
        excluded_document_count: 0,
        failed_document_count: 0,
        prediction_artifact_id: "prediction-artifact-1",
      } as never;
      if (path.endsWith("/classification-models/classification-model-1/scoring-runs/scoring-run-1/prediction-summary")) return {
        scoring_run_id: "scoring-run-1",
        model_version_id: "classification-model-version-1",
        ordered_class_keys: ["topic_401", "topic_402"],
        minimum_probability: 0.8,
        total_document_count: 100,
        evaluated_document_count: 96,
        predicted_document_count: 80,
        no_prediction_document_count: 20,
        predicted_value_count: 80,
        abstained_document_count: 4,
        class_counts: [
          { class_key: "topic_401", label: "Topic 401", document_count: 45 },
          { class_key: "topic_402", label: "Topic 402", document_count: 35 },
        ],
      } as never;
      if (path.endsWith("/review-batches/from-classification-run") && init?.method === "POST") return {
        id: "classification-review-batch-1",
        matter_id: "matter-1",
        name: "High-confidence issue review",
        status: "QUEUED",
        selection_type: "CLASSIFICATION_PREDICTIONS",
        selection_definition: {},
        document_count: 0,
        created_at: "2026-09-28T12:10:00Z",
        updated_at: "2026-09-28T12:10:00Z",
      } as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={queryClient}><ClassificationAnalysisJobDetail
      clientId="client-1"
      matterId="matter-1"
      run={{ ...classificationAnalysisRun, task_name: "Issue classifier" }}
    /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "Create review batch" }));
    expect(screen.getByRole("heading", { name: "Create review batch from classifier values" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Batch name"));
    await user.type(screen.getByLabelText("Batch name"), "High-confidence issue review");
    expect(screen.getByLabelText("Minimum confidence %")).toHaveValue(80);
    await user.clear(screen.getByLabelText("Maximum confidence %"));
    await user.type(screen.getByLabelText("Maximum confidence %"), "90");
    await user.click(await screen.findByRole("checkbox", { name: /Topic 401/ }));
    await user.click(screen.getByRole("button", { name: "Create batch" }));

    await waitFor(() => {
      const call = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/review-batches/from-classification-run") && init?.method === "POST");
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        classification_analysis_run_id: classificationAnalysisRun.id,
        name: "High-confidence issue review",
        minimum_confidence: 0.8,
        maximum_confidence: 0.9,
        selected_class_keys: ["topic_401"],
      });
      expect(screen.getByRole("link", { name: "Open batch" })).toHaveAttribute(
        "href",
        "/review/matters/matter-1?batch=classification-review-batch-1",
      );
    });
  });

  it("starts and reruns analysis, then saves the coding panel as one document request", async () => {
    let codingSearchReady = false;
    let batchAnalysisJobs: ReviewBatchAnalysisJobRead[] = [];
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1") return { id: "matter-1", client_id: "client-1", name: "Investigation", status: "ACTIVE", created_at: "2026-09-15T12:00:00Z" } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", name: "Batch Client" } as never;
      if (path === "/v1/clients/client-1/custodians") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [{
        id: "definition-issue",
        key: "issue_coding",
        display_name: "Issue Coding",
        type: "ENUM",
        cardinality: "MULTIPLE",
        allowed_values: [{ key: "topic_401", label: "Olympics", active: true }],
        status: "ACTIVE",
        searchable: true,
        facetable: true,
      }] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [{
        id: "group-issue",
        display_name: "Issue metadata",
        sort_order: 10,
        definition_ids: ["definition-issue"],
        status: "ACTIVE",
      }] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1" && !init?.method) return codingSearchReady ? {
        ...batch,
        searchable_coding_run_id: "analysis-run-1",
        searchable_coding_status: "READY",
        searchable_coding_fields: [{ metadata_definition_id: "definition-1", key: "responsiveness", display_name: "Responsiveness", type: "ENUM", value_count: 1 }],
      } as never : batch as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/runs" && !init?.method) return [] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/analysis-runs" && !init?.method) return batchAnalysisJobs as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/topic-taxonomy") return { review_batch_run_id: "assessment-run", version: 1, topics: [] } as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/topic-facets" && init?.method === "POST") return { values: [] } as never;
      if (path.endsWith("/coding-facets/responsiveness/values") && init?.method === "POST") return { field: "responsiveness", values: [{ value: "responsive", label: "Responsive", count: 1 }] } as never;
      if (path.endsWith("/facets/issue_coding/values") && init?.method === "POST") return { field: "issue_coding", values: [{ value: "topic_401", count: 1 }], missing_count: 0, total_count: 1, has_more: false } as never;
      if (path.endsWith("/runs/assessment-run/documents/document-1/analysis")) return { matter_document_id: "document-1", status: "COMPLETED", analysis: {} } as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/search" && init?.method === "POST") return {
        total: 1,
        took_ms: 2,
        timed_out: false,
        hits: [{
          document_id: "document-1",
          score: null,
          fields: {
            collection_item_id: "item-1",
            email_subject: "Contract review request",
            original_filename: "contract.eml",
            record_type: "EMAIL",
            source_path: "mailbox/contract.eml",
            metadata: {},
          },
          highlights: {},
          best_passage: null,
        }],
        facets: {},
      } as never;
      if (path.endsWith("/reviewer-coding") && init?.method === "POST") return { id: "coding-1", review_batch_id: "batch-1", reviewer_user_id: "user-1", revision: 0, created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" } as never;
      if (path === "/v1/matters/matter-1/analysis-tasks") return [analysisTask, classificationTask] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/analysis-runs" && init?.method === "POST") {
        batchAnalysisJobs = [questionAnsweringJob(analysisRun), ...batchAnalysisJobs];
        return analysisRun as never;
      }
      if (path === "/v1/matters/matter-1/analysis-tasks/task-classifier-1/runs" && init?.method === "POST") {
        const classifierRun = {
        id: "classification-analysis-run-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        matter_analysis_task_id: "task-classifier-1",
        matter_analysis_task_version_id: "task-classifier-version-1",
        scope_type: "REVIEW_BATCH",
        scope_id: "batch-1",
        classification_model_id: "classification-model-1",
        classification_model_version_id: "classification-model-version-1",
        embedding_job_id: "embedding-job-1",
        dataset_snapshot_id: null,
        classification_scoring_run_id: null,
        status: "QUEUED",
        current_stage: "QUEUED",
        progress_completed: 0,
        progress_total: 4,
        population_document_count: 0,
        feature_processed_count: 0,
        feature_ready_count: 0,
        feature_missing_count: 0,
        feature_failed_count: 0,
        failure_code: null,
        failure_summary: null,
        failure_retriable: false,
        created_by_user_id: "user-1",
        started_at: null,
        completed_at: null,
        canceled_at: null,
        created_at: "2026-09-28T12:00:00Z",
        updated_at: "2026-09-28T12:00:00Z",
          execution: null,
        } as const;
        batchAnalysisJobs = [{
          id: classifierRun.id,
          job_type: "CLASSIFICATION_ANALYSIS_TASK",
          task_type: "CLASSIFICATION",
          matter_analysis_task_id: classificationTask.id,
          task_name: classificationTask.name,
          task_version: 1,
          status: classifierRun.status,
          created_at: classifierRun.created_at,
          updated_at: classifierRun.updated_at,
          execution: null,
          review_batch_run: null,
          classification_analysis_run: classifierRun,
        }, ...batchAnalysisJobs] as ReviewBatchAnalysisJobRead[];
        return classifierRun as never;
      }
      if (path.endsWith("/runs/analysis-run-1/coding-search") && init?.method === "POST") {
        codingSearchReady = true;
        return { review_batch_id: "batch-1", review_batch_run_id: "analysis-run-1", status: "READY" } as never;
      }
      if (path.endsWith("/runs/analysis-run-1/promotion") && init?.method === "POST") return {
        id: "promotion-1",
        matter_id: "matter-1",
        review_batch_id: "batch-1",
        review_batch_run_id: "analysis-run-1",
        matter_analysis_task_version_id: "task-version-1",
        status: "QUEUED",
        mode: "REPLACE",
        destination_snapshot: [{ id: "definition-issue", key: "issue_coding", display_name: "Issue Coding", type: "ENUM", cardinality: "MULTIPLE", assertion_policy: "IMMEDIATE", resolution_policy: "LATEST_VALID" }],
        total_document_count: 1,
        eligible_document_count: 0,
        processed_document_count: 0,
        skipped_document_count: 0,
        event_count: 0,
      } as never;
      if (path.endsWith("/runs/analysis-run-1/promotion") && !init?.method) return null as never;
      if (path.endsWith("/runs/analysis-run-1/progress")) return { review_batch_run_id: "analysis-run-1", document_count: 1, not_started_count: 1, in_progress_count: 0, completed_count: 0, skipped_count: 0, failed_count: 0 } as never;
      if (path.includes("/documents?run_id=analysis-run-1&document_id=document-1")) return [{ matter_document_id: "document-1", source_collection_id: "collection-1", collection_item_id: "item-1", sequence_number: 1, review_status: "NOT_STARTED" }] as never;
      if (path.includes(`/documents?reviewer_coding_id=coding-1&document_id=${citedDocumentId}`)) return [{ matter_document_id: citedDocumentId, source_collection_id: "collection-1", collection_item_id: "item-2", sequence_number: 2, review_status: "NOT_STARTED" }] as never;
      if (path.includes("/documents?reviewer_coding_id=coding-1")) return [{ matter_document_id: "document-1", source_collection_id: "collection-1", collection_item_id: "item-1", sequence_number: 1, review_status: "NOT_STARTED" }] as never;
      if (path.endsWith("/reviewer-coding/progress")) return { reviewer_coding_id: "coding-1", document_count: 1, not_started_count: 1, in_progress_count: 0, completed_count: 0, skipped_count: 0 } as never;
      if (path === "/v1/matters/matter-1/custodians") return [] as never;
      if (path === "/v1/matters/matter-1/documents/document-1/collection-item") return { original_filename: "contract.eml" } as never;
      if (path === `/v1/matters/matter-1/documents/${citedDocumentId}/collection-item`) return { original_filename: "cited-document.eml" } as never;
      if (path.endsWith("/reviewer-coding/documents/document-1") && !init?.method) return { matter_document_id: "document-1", review_status: "NOT_STARTED", values: [], reviewer_values: [] } as never;
      if (path.endsWith(`/reviewer-coding/documents/${citedDocumentId}`) && !init?.method) return { matter_document_id: citedDocumentId, review_status: "NOT_STARTED", values: [], reviewer_values: [] } as never;
      if (path.endsWith("/reviewer-coding/documents/document-1/values") && init?.method === "PUT") return [] as never;
      if (path === "/v1/matters/matter-1/agents?workflow_type=BATCH_CHAT") return [{ id: "batch-agent-1", name: "Batch Chat Agent" }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=BATCH_CHAT&review_batch_id=batch-1") return [{
        id: "conversation-existing", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1", review_batch_id: "batch-1",
        agent_definition_id: "batch-agent-1", agent_definition_version_id: "batch-agent-version-1", title: "Existing renewal chat",
        workflow_type: "BATCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1",
        created_at: "2026-09-14T12:00:00Z", updated_at: "2026-09-14T12:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST") {
        const payload = JSON.parse(String(init.body));
        return {
          id: "conversation-1",
          tenant_id: "tenant-1",
          client_id: "client-1",
          matter_id: "matter-1",
          review_batch_id: "batch-1",
          agent_definition_id: "batch-agent-1",
          agent_definition_version_id: "batch-agent-version-1",
          title: payload.title,
          workflow_type: "BATCH_CHAT",
          status: "ACTIVE",
          initiated_by_user_id: "user-1",
          created_at: "2026-09-15T12:00:00Z",
          updated_at: "2026-09-15T12:00:00Z",
        } as never;
      }
      if (path === "/v1/agent-conversations/conversation-1/turns" && init?.method === "POST") {
        const payload = JSON.parse(String(init.body));
        return {
          turn: { id: "turn-1", conversation_id: "conversation-1", sequence: 1, status: "QUEUED", created_by_user_id: "user-1", completed_at: null, error_message: null, created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" },
          message: { id: "message-user-1", conversation_id: "conversation-1", turn_id: "turn-1", sequence: 1, role: "USER", content: payload.message, message_data: {}, created_by_user_id: "user-1", agent_run_id: null, created_at: "2026-09-15T12:00:00Z" },
          run: { id: "agent-run-1", conversation_id: "conversation-1", turn_id: "turn-1", sequence: 1, workflow_id: "agent-run:agent-run-1", parent_run_id: null, deferred_from_run_id: null, agent_definition_version_id: "batch-agent-version-1", actor_user_id: "user-1", model_key: "configured-default", status: "QUEUED", output_text: null, request_count: 0, tool_call_count: 0, input_tokens: 0, cached_input_tokens: 0, cache_write_tokens: 0, output_tokens: 0, error_message: null, started_at: null, completed_at: null, created_at: "2026-09-15T12:00:00Z" },
        } as never;
      }
      if (path === "/v1/agent-conversations/conversation-existing/messages") return [{ id: "message-1", conversation_id: "conversation-existing", turn_id: "turn-existing", sequence: 1, role: "ASSISTANT", content: `See [Document ${citedDocumentId} ¶1-5].`, agent_run_id: "agent-run-existing", created_by_user_id: null, created_at: "2026-09-14T12:00:00Z" }] as never;
      if (path === "/v1/agent-conversations/conversation-existing/runs") return [] as never;
      if (path === "/v1/agent-conversations/conversation-existing" && init?.method === "PATCH") {
        const payload = JSON.parse(String(init.body));
        return { id: "conversation-existing", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1", review_batch_id: "batch-1", agent_definition_id: "batch-agent-1", agent_definition_version_id: "batch-agent-version-1", title: payload.title, workflow_type: "BATCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1", created_at: "2026-09-14T12:00:00Z", updated_at: "2026-09-14T12:01:00Z" } as never;
      }
      if (path === "/v1/agent-conversations/conversation-1/messages") return [] as never;
      if (path === "/v1/agent-conversations/conversation-1/runs") return [] as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={queryClient}><BatchReviewWorkspace matterId="matter-1" batchId="batch-1" initialAnalysisDialogOpen /></QueryClientProvider>);

    expect(await screen.findByText("Contract review request")).toBeInTheDocument();
    expect(await screen.findByText("contract.eml")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveClass("fixed", "inset-0", "h-dvh", "min-h-0", "overscroll-none");
    expect(screen.getByRole("main")).toHaveStyle({ overflow: "clip" });
    // The batch keeps its parent link, and the matter menu reaches the rest of the matter.
    expect(screen.getByRole("link", { name: "Batches", hidden: true })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/batches");
    expect(screen.getByRole("button", { name: "Matter: Investigation. Open matter menu", hidden: true })).toHaveTextContent("Batch Client");
    expect(screen.getByRole("button", { name: "Export results", hidden: true })).toBeEnabled();
    expect(screen.getByLabelText("Batch review layout")).toHaveStyle({
      gridTemplateColumns: "24rem minmax(0, 1fr) 30rem",
      gridTemplateRows: "minmax(0, 1fr)",
    });
    expect(screen.getByLabelText("Batch navigation")).toHaveClass("min-h-0", "overflow-hidden");
    expect(screen.getByLabelText("Batch analysis, coding, history, and chat")).toHaveClass("min-h-0", "overflow-hidden");

    expect(await screen.findByText("Run analysis against this batch")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("tab", { name: /Documents/ })).toHaveAttribute("aria-selected", "true");
    await user.click(screen.getByRole("tab", { name: "Filters" }));
    expect(screen.getByRole("tab", { name: "Filters" })).toHaveAttribute("aria-selected", "true");
    const filterPanel = screen.getByRole("tabpanel", { name: "Filters" });
    expect(filterPanel).toHaveClass("min-h-0", "overflow-y-auto", "overscroll-contain");
    const issueFilters = within(filterPanel).getByRole("region", { name: "Issue metadata filters" });
    const issueGroupToggle = within(issueFilters).getByRole("button", { name: "Issue metadata filter group" });
    expect(issueFilters).toHaveTextContent("Issue Coding");
    await user.click(issueGroupToggle);
    expect(issueGroupToggle).toHaveAttribute("aria-expanded", "false");
    expect(within(issueFilters).queryByRole("button", { name: "Issue Coding" })).not.toBeInTheDocument();
    await user.click(issueGroupToggle);
    expect(issueGroupToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByText("Issue Coding").length).toBeGreaterThan(0);
    expect(screen.queryByText("issue_coding")).not.toBeInTheDocument();
    expect(screen.getByText("Matter Definition assessment")).toBeInTheDocument();
    expect(screen.getByText("This is the original assessment output, not a question-answering Analysis Task run.")).toBeInTheDocument();
    await user.click(within(screen.getByLabelText("Batch analysis, coding, history, and chat")).getByRole("button", { name: "Run analysis" }));
    expect(await screen.findByText("Run analysis against this batch")).toBeInTheDocument();
    expect(await screen.findByRole("combobox", { name: "Published task" })).toHaveTextContent("Privilege review");
    expect(screen.getAllByText("OpenAI Decisions").length).toBeGreaterThan(0);
    expect(screen.getByText("gpt-6-luna")).toBeInTheDocument();
    expect(screen.getByText("A newer draft is not published")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run on 1 documents" }));

    await waitFor(() => {
      const launchCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/analysis-runs") && init?.method === "POST");
      expect(JSON.parse(String(launchCall?.[1]?.body))).toEqual({ matter_analysis_task_id: "task-1" });
    });
    expect(await screen.findByRole("combobox", { name: "Analysis run" })).toHaveTextContent("Privilege review · published v1");
    expect(screen.queryByText("Published v1 · OpenAI Decisions · gpt-6-luna · isolated results")).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Analysis run" }));
    await user.click(screen.getByRole("option", { name: /Matter Definition assessment.*v1/i }));
    expect(await screen.findByText("This is the original assessment output, not a question-answering Analysis Task run.")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Analysis run" }));
    await user.click(screen.getByRole("option", { name: /Privilege review.*published v1/i }));
    expect(await screen.findByRole("combobox", { name: "Analysis run" })).toHaveTextContent("Privilege review · published v1");
    expect(screen.getByText("Replace Issue Coding with this run's decisions.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run new task" }));
    expect(await screen.findByRole("combobox", { name: "Published task" })).toHaveTextContent("Privilege review");
    await user.click(screen.getByRole("combobox", { name: "Published task" }));
    await user.click(screen.getByRole("option", { name: /Issue classifier.*Classifier/i }));
    expect(screen.getByText("classification-model-version-1")).toBeInTheDocument();
    expect(screen.getByText("embedding-job-1")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run on 1 documents" }));
    await waitFor(() => {
      const classifierCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/analysis-tasks/task-classifier-1/runs") && init?.method === "POST");
      expect(JSON.parse(String(classifierCall?.[1]?.body))).toMatchObject({
        scope_type: "REVIEW_BATCH",
        scope_id: "batch-1",
        embedding_job_id: "embedding-job-1",
      });
      expect(JSON.parse(String(classifierCall?.[1]?.body)).idempotency_key).toEqual(expect.any(String));
    });
    expect(await screen.findByRole("combobox", { name: "Analysis run" })).toHaveTextContent("Issue classifier · published v1");
    expect(screen.getByText("Scoring output will become available after feature preparation completes.")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Analysis run" }));
    await user.click(screen.getByRole("option", { name: /Privilege review.*published v1/i }));
    await user.click(screen.getByRole("button", { name: "Promote coding" }));
    expect(await screen.findByRole("heading", { name: "Promote analysis coding to the matter?" })).toBeInTheDocument();
    expect(screen.getAllByText("Issue Coding").length).toBeGreaterThan(1);
    expect(screen.getByText(/Filters use each destination field's display name/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Promote to matter coding" }));
    await waitFor(() => {
      expect(vi.mocked(coreApi).mock.calls.some(([path, init]) => path.endsWith("/runs/analysis-run-1/promotion") && init?.method === "POST")).toBe(true);
    });
    await user.click(screen.getByRole("button", { name: "Run again" }));
    expect(await screen.findByRole("combobox", { name: "Published task" })).toHaveTextContent("Privilege review");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Make searchable" }));
    expect(await screen.findByLabelText("Batch Coding fields")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Responsiveness" }));
    await user.click(await screen.findByRole("checkbox", { name: /Responsive/ }));
    await user.click(screen.getByRole("combobox", { name: "Confidence for Responsiveness" }));
    await user.click(screen.getByRole("option", { name: "80% to under 90%" }));
    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { coding_filters: Array<Record<string, unknown>> };
      expect(lastRequest.coding_filters).toContainEqual({ field: "responsiveness", values: ["responsive"], minimum_confidence: 0.8, maximum_confidence: 0.9 });
    });
    await user.click(screen.getByRole("button", { name: "View filters" }));
    expect(screen.getByLabelText("Batch Coding fields")).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Issue Coding" }));
    await user.click(await screen.findByRole("checkbox", { name: /Has value/ }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: Array<{ field: string; operator: string }> };
      expect(lastRequest.filters).toContainEqual({ field: "issue_coding", operator: "EXISTS" });
    });

    await user.click(await screen.findByRole("checkbox", { name: /Olympics/ }));
    const olympicsFilter = screen.getByRole("checkbox", { name: /Olympics/ });
    await user.click(screen.getByRole("button", { name: "Negate" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: Array<{ field: string; operator: string; values?: string[] }> };
      expect(lastRequest.filters).toContainEqual({ field: "issue_coding", operator: "NOT_IN", values: ["topic_401"] });
      expect(olympicsFilter.nextElementSibling).toHaveAttribute("data-selection-mode", "excluded");
    });

    await user.click(screen.getByRole("combobox", { name: "Search mode" }));
    await user.click(screen.getByRole("option", { name: "Semantic" }));
    await user.type(screen.getByRole("spinbutton", { name: "Minimum semantic similarity" }), "0.8");
    await user.type(screen.getByRole("textbox", { name: "Search batch documents" }), "contract termination risk");
    await user.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { search_mode: string; minimum_similarity: number };
      expect(lastRequest).toMatchObject({ search_mode: "SEMANTIC", minimum_similarity: 0.8 });
    });

    await user.click(screen.getByRole("button", { name: "Coding" }));
    await user.click(screen.getByRole("combobox", { name: "Responsiveness" }));
    await user.click(screen.getByRole("option", { name: "Responsive" }));
    await user.click(screen.getByRole("button", { name: "Save & next" }));

    await waitFor(() => {
      const saveCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/values") && init?.method === "PUT");
      expect(saveCall).toBeDefined();
      expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
        matter_document_id: "document-1",
        fields: [{ metadata_definition_id: "definition-1", values: ["responsive"], confidence: null }],
      });
    });

    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByLabelText("Batch review layout")).toHaveStyle({ gridTemplateColumns: "24rem minmax(0, 1fr) 30rem" });
    expect(await screen.findByRole("combobox", { name: "Previous batch chats" })).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: `Document ${citedDocumentId} ¶1-5` }));
    expect(await screen.findByText("cited-document.eml")).toBeInTheDocument();
    expect(screen.getByTestId("highlighted-paragraphs")).toHaveTextContent("1,2,3,4,5");
    await user.click(screen.getByRole("button", { name: "Rename batch chat" }));
    const renameDialog = screen.getByRole("dialog", { name: "Rename batch chat" });
    await user.clear(within(renameDialog).getByRole("textbox", { name: "Batch chat title" }));
    await user.type(within(renameDialog).getByRole("textbox", { name: "Batch chat title" }), "Renewal evidence review");
    await user.click(within(renameDialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith("/v1/agent-conversations/conversation-existing", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ title: "Renewal evidence review" }) })));
    await user.click(screen.getByRole("button", { name: "New batch chat" }));
    expect(await screen.findByText("Chat with this batch")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start chat and send" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your first question before starting the chat.");
    const firstQuestion = "Which documents discuss renewal timing, termination risk, and proposed contract changes?";
    await user.type(screen.getByRole("textbox", { name: "Starting message Batch Chat" }), firstQuestion);
    await user.click(screen.getByRole("button", { name: "Start chat and send" }));
    await waitFor(() => {
      const createCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST");
      expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
        agent_definition_id: "batch-agent-1",
        title: firstQuestion.slice(0, 50),
        workflow_type: "BATCH_CHAT",
        review_batch_id: "batch-1",
      });
      const turnCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/agent-conversations/conversation-1/turns" && init?.method === "POST");
      expect(JSON.parse(String(turnCall?.[1]?.body))).toEqual({ message: firstQuestion });
    });
    expect(screen.getByRole("combobox", { name: "Previous batch chats" })).toBeInTheDocument();
  }, 20_000);
});
