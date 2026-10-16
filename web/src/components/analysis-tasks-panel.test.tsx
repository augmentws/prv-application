import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisTasksPanel } from "@/components/analysis-tasks-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const version = {
  id: "version-1",
  matter_analysis_task_id: "task-1",
  version: 1,
  status: "PUBLISHED",
  compilation_status: "READY",
  definition_markdown: "# Privilege\n\nReview legal advice communications.",
  definition_content_hash: "a".repeat(64),
  destination_metadata_definition_ids: ["definition-1"],
  decision_provider: "JEV",
  decision_model: null,
  decision_specification: {
    questions: {
      "privilege.legal_advice": {
        type: "noul",
        instructions: "Does the document request or provide legal advice?",
        source_refs: [{ task_version_id: "version-1", excerpt_hash: "b".repeat(64) }],
        aggregation: { operator: "ANY_WINDOW" },
      },
    },
    state_contract: { builder_version: "document-review-state-v1", required_paths: ["document.text"] },
  },
  specification_content_hash: "c".repeat(64),
  input_contract: {},
  output_contract: {},
  evidence_policy: {},
  routing_policy: {},
  compiler_skill_definition_version_id: "skill-version-1",
  compiler_skill_run_id: "skill-run-1",
  compiler_workflow_run_id: "compiler-workflow-1",
  compiler_model_configuration: {},
  validation_report: {},
  source_provenance: {},
  created_by_user_id: "user-1",
  published_by_user_id: "user-1",
  created_at: "2026-09-26T12:00:00Z",
  published_at: "2026-09-26T12:10:00Z",
} as const;

const destinationDefinition = {
  id: "definition-1",
  matter_id: "matter-1",
  key: "issue_coding",
  display_name: "Issue coding",
  description: "Issues found in the document",
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
  ai_assignable: true,
  status: "ACTIVE",
  created_at: "2026-09-26T12:00:00Z",
} as const;

const task = {
  id: "task-1",
  matter_id: "matter-1",
  key: "privilege_review",
  name: "Privilege review",
  description: "First-pass privilege review",
  task_type: "QUESTION_ANSWERING",
  workflow_key: "question_answering_v1",
  current_version: 1,
  published_version: 1,
  status: "ACTIVE",
  created_by_user_id: "user-1",
  created_at: "2026-09-26T12:00:00Z",
  updated_at: "2026-09-26T12:10:00Z",
  version,
} as const;

describe("AnalysisTasksPanel", () => {
  it("shows compilation stages, destination coverage, and validation repairs", async () => {
    const generatingVersion = {
      ...version,
      status: "DRAFT",
      compilation_status: "GENERATING",
      decision_specification: null,
      specification_content_hash: null,
      compilation_progress: {
        stage: "REPAIRING_OUTPUT",
        message: "Candidate 1 failed validation; the compiler is repairing its output.",
        validation_attempt: 1,
        source_reference_count: 35,
        destinations: [{
          key: "issue_coding",
          display_name: "Issue coding",
          type: "ENUM",
          cardinality: "MULTIPLE",
          active_value_count: 34,
        }],
        validation_failures: [{ attempt: 1, message: "Topic 401 requires a mapped question." }],
      },
    } as const;
    const generatingTask = { ...task, current_version: 1, published_version: null, version: generatingVersion } as const;
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [generatingTask] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions") return [generatingVersion] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    expect(await screen.findByText("Candidate 1 failed validation; the compiler is repairing its output.")).toBeInTheDocument();
    expect(screen.getByText("35 source references")).toBeInTheDocument();
    expect(screen.getByText("34 active values", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Topic 401 requires a mapped question.", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/does not expose private model reasoning/i)).toBeInTheDocument();
  });

  it("offers task types and decision providers", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "New analysis task" }));
    expect(screen.getByText("Issue coding")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Task type" }));
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(screen.getByRole("option", { name: "Question answering" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Classification" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Fact development" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("combobox", { name: "Decision provider" }));
    expect(screen.getByRole("option", { name: "TypeSafe JEV" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "OpenAI Decisions" })).toBeInTheDocument();
  });

  it("creates a fact-development task pinned to published Review Guidance", async () => {
    const created: unknown[] = [];
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks" && init?.method === "POST") {
        created.push(JSON.parse(String(init.body)));
        throw new Error("stop after capturing the request");
      }
      if (path === "/v1/matters/matter-1/analysis-tasks") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/guidance") return [
        { id: "guidance-1", name: "Second pass", status: "ACTIVE", published_revision: 2 },
        { id: "guidance-2", name: "Unpublished draft", status: "ACTIVE", published_revision: null },
      ] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "New analysis task" }));
    await user.type(screen.getByLabelText("Name"), "Pricing facts");
    await user.click(screen.getByRole("combobox", { name: "Task type" }));
    await user.click(screen.getByRole("option", { name: "Fact development" }));
    expect(screen.queryByText("Issue coding")).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Review Guidance" }));
    expect(screen.queryByRole("option", { name: /Unpublished draft/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Second pass · revision 2" }));
    await user.type(screen.getByLabelText("Fact guidance"), "One event per fact.");
    await user.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(created).toEqual([{
      key: "pricing_facts",
      name: "Pricing facts",
      description: null,
      task_type: "FACT_DEVELOPMENT",
      definition_markdown: "One event per fact.",
      review_guidance_id: "guidance-1",
    }]));
  });

  it("stores the selected decision provider when creating a question-answering task", async () => {
    const openAiTask = {
      ...task,
      id: "task-openai",
      version: { ...version, id: "version-openai", decision_provider: "OPENAI_DECISIONS", decision_model: "gpt-6-luna" },
    } as const;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks" && init?.method === "POST") return openAiTask as never;
      if (path === "/v1/matters/matter-1/analysis-tasks") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "New analysis task" }));
    await user.type(screen.getByRole("textbox", { name: "Name" }), "OpenAI first pass");
    await user.type(screen.getByRole("textbox", { name: "Task definition" }), "Review the document for responsiveness.");
    await user.click(screen.getByRole("combobox", { name: "Decision provider" }));
    await user.click(screen.getByRole("option", { name: "OpenAI Decisions" }));
    await user.click(screen.getByRole("button", { name: "Create task" }));

    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"decision_provider":"OPENAI_DECISIONS"'),
      }),
    ));
  });

  it("hides classifier records without an activated version", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [{
        id: "model-1",
        name: "Issue classifier",
        status: "ACTIVE",
        active_model_version_id: null,
        target_metadata_definition_id: "definition-1",
        target_field: { display_name: "Issue coding" },
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "New analysis task" }));
    await user.click(screen.getByRole("combobox", { name: "Task type" }));
    await user.click(screen.getByRole("option", { name: "Classification" }));

    expect(screen.getByText("No activated classification models are available.")).toBeInTheDocument();
    expect(screen.queryByText("Issue classifier")).not.toBeInTheDocument();
    expect(screen.queryByText("Classifier records without an activated version")).not.toBeInTheDocument();
  });

  it("creates a draft revision from an unchanged published version", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [task] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions" && init?.method === "POST") {
        return task as never;
      }
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions") return [version] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    await user.click(await screen.findByRole("button", { name: "Create draft revision" }));

    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks/task-1/versions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          definition_markdown: version.definition_markdown,
          based_on_version: 1,
          decision_provider: "JEV",
          destination_metadata_definition_ids: ["definition-1"],
        }),
      }),
    ));
  });

  it("runs a published classification task across the whole matter", async () => {
    const classificationVersion = {
      ...version,
      id: "classification-version-1",
      matter_analysis_task_id: "classification-task-1",
      decision_specification: null,
      specification_content_hash: null,
      destination_metadata_definition_ids: ["definition-1"],
      classification_model_id: "model-1",
      classification_model_version_id: "model-version-1",
      classification_embedding_job_id: "embedding-1",
    } as const;
    const classificationTask = {
      ...task,
      id: "classification-task-1",
      key: "issue_classifier",
      name: "Issue classifier",
      task_type: "CLASSIFICATION",
      workflow_key: "classification_scoring_v1",
      version: classificationVersion,
    } as const;
    const analysisRun = {
      id: "analysis-run-1",
      tenant_id: "tenant-1",
      matter_id: "matter-1",
      matter_analysis_task_id: "classification-task-1",
      matter_analysis_task_version_id: "classification-version-1",
      scope_type: "MATTER",
      scope_id: "matter-1",
      classification_model_id: "model-1",
      classification_model_version_id: "model-version-1",
      embedding_job_id: "embedding-1",
      dataset_snapshot_id: "snapshot-1",
      classification_scoring_run_id: null,
      status: "PREPARING_FEATURES",
      current_stage: "MATERIALIZING_FEATURES",
      progress_completed: 1,
      progress_total: 4,
      population_document_count: 290099,
      feature_processed_count: 120000,
      feature_ready_count: 119000,
      feature_missing_count: 900,
      feature_failed_count: 100,
      failure_code: null,
      failure_summary: null,
      failure_retriable: false,
      created_by_user_id: "user-1",
      started_at: "2026-09-26T12:00:01Z",
      completed_at: null,
      canceled_at: null,
      created_at: "2026-09-26T12:00:00Z",
      updated_at: "2026-09-26T12:00:01Z",
      execution: {
        id: "execution-1",
        job_type: "CLASSIFICATION_ANALYSIS_TASK",
        job_id: "analysis-run-1",
        attempt_number: 1,
        retry_of_workflow_run_id: null,
        workflow_key: "classification_analysis_task_v1",
        dbos_workflow_id: "classification-analysis-task:analysis-run-1",
        status: "RUNNING",
        current_stage: "MATERIALIZING_FEATURES",
        stage_label: "Materializing Features",
        progress_completed: 1,
        progress_total: 4,
        progress_unit: "stages",
        progress: {},
        cancel_requested_at: null,
        cancel_requested_by_user_id: null,
        last_heartbeat_at: "2026-09-26T12:00:01Z",
        failure_code: null,
        failure_summary: null,
        failure_retriable: false,
        initiated_by_user_id: "user-1",
        started_at: "2026-09-26T12:00:01Z",
        completed_at: null,
        created_at: "2026-09-26T12:00:00Z",
        updated_at: "2026-09-26T12:00:01Z",
        available_actions: ["CANCEL"],
      },
    } as const;
    let runCanceled = false;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [classificationTask] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/versions") return [classificationVersion] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs" && init?.method === "POST") return analysisRun as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs/analysis-run-1/cancel" && init?.method === "POST") {
        runCanceled = true;
        return { ...analysisRun, status: "CANCELED", current_stage: "CANCELED", completed_at: "2026-09-26T12:02:00Z", canceled_at: "2026-09-26T12:02:00Z", execution: { ...analysisRun.execution, status: "CANCELED", current_stage: "CANCELED", cancel_requested_at: "2026-09-26T12:02:00Z", completed_at: "2026-09-26T12:02:00Z", available_actions: ["RETRY"] } } as never;
      }
      if (path === "/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/analysis-run-1/retry" && init?.method === "POST") return { ...analysisRun.execution, id: "execution-2", attempt_number: 2, retry_of_workflow_run_id: "execution-1", status: "QUEUED", current_stage: "QUEUED", available_actions: ["CANCEL"] } as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs") return [runCanceled ? { ...analysisRun, status: "CANCELED", current_stage: "CANCELED", completed_at: "2026-09-26T12:02:00Z", canceled_at: "2026-09-26T12:02:00Z", execution: { ...analysisRun.execution, status: "CANCELED", current_stage: "CANCELED", cancel_requested_at: "2026-09-26T12:02:00Z", completed_at: "2026-09-26T12:02:00Z", available_actions: ["RETRY"] } } : analysisRun] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [{
        id: "embedding-1",
        status: "COMPLETED",
        embedding_model: "voyage-4-nano",
        embedding_dimensions: 1024,
        processed_count: 100,
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Run classification" })).toBeInTheDocument();
    expect(screen.getByText(/no per-document batch rows are created/i)).toBeInTheDocument();
    expect(await screen.findByText(/120,000 of 290,099 features processed/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs/analysis-run-1/cancel",
      { method: "POST" },
    ));
    expect(await screen.findByText("canceled")).toBeInTheDocument();
    expect(screen.getByText("Attempt 1", { exact: false })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry run" }));
    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/analysis-run-1/retry",
      { method: "POST" },
    ));
    await user.click(screen.getByRole("button", { name: "Run classification" }));
    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"scope_type":"MATTER"'),
      }),
    ));
  });

  it("links a completed classification run to its job results", async () => {
    const classificationVersion = {
      ...version,
      id: "classification-version-1",
      matter_analysis_task_id: "classification-task-1",
      decision_specification: null,
      specification_content_hash: null,
      destination_metadata_definition_ids: ["definition-1"],
      classification_model_id: "model-1",
      classification_model_version_id: "model-version-1",
      classification_embedding_job_id: "embedding-1",
    } as const;
    const classificationTask = {
      ...task,
      id: "classification-task-1",
      key: "issue_classifier",
      name: "Issue classifier",
      task_type: "CLASSIFICATION",
      workflow_key: "classification_scoring_v1",
      version: classificationVersion,
    } as const;
    const completedRun = {
      id: "analysis-run-1",
      tenant_id: "tenant-1",
      matter_id: "matter-1",
      matter_analysis_task_id: "classification-task-1",
      matter_analysis_task_version_id: "classification-version-1",
      scope_type: "MATTER",
      scope_id: "matter-1",
      classification_model_id: "model-1",
      classification_model_version_id: "model-version-1",
      embedding_job_id: "embedding-1",
      dataset_snapshot_id: "snapshot-1",
      classification_scoring_run_id: "scoring-run-1",
      status: "COMPLETED",
      current_stage: "COMPLETED",
      progress_completed: 4,
      progress_total: 4,
      population_document_count: 290099,
      feature_processed_count: 290099,
      feature_ready_count: 287080,
      feature_missing_count: 3019,
      feature_failed_count: 0,
      failure_code: null,
      failure_summary: null,
      failure_retriable: false,
      created_by_user_id: "user-1",
      started_at: "2026-09-26T12:00:01Z",
      completed_at: "2026-09-26T12:10:00Z",
      canceled_at: null,
      created_at: "2026-09-26T12:00:00Z",
      updated_at: "2026-09-26T12:10:00Z",
      execution: null,
    } as const;
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [classificationTask] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/versions") return [classificationVersion] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/classification-task-1/runs") return [completedRun] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel clientId="client-1" matterId="matter-1" /></QueryClientProvider>);

    expect(await screen.findByText(/290,099 features processed/)).toBeInTheDocument();
    expect(screen.queryByText("Classification output")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open job" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1?tab=jobs&job_type=CLASSIFICATION_ANALYSIS&job=analysis-run-1",
    );
  });

  it("launches a published task against one batch document and displays its result", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [task] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [destinationDefinition] as never;
      if (path === "/v1/matters/matter-1/classification-models") return [] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions") return [version] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [{ id: "batch-1", name: "Privilege sample", status: "READY", document_count: 1 }] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/documents?limit=500") return [{ matter_document_id: "document-1", collection_item_id: "item-1", sequence_number: 1, review_status: "NOT_STARTED" }] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions/1/playground-runs" && init?.method === "POST") return {
        workflow_run_id: "workflow-1",
        review_batch_id: "batch-1",
        review_batch_run_id: "run-1",
        matter_document_id: "document-1",
        task_version_id: "version-1",
        status: "QUEUED",
      } as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions/1/playground-runs/workflow-1") return {
        workflow_run_id: "workflow-1",
        review_batch_id: "batch-1",
        review_batch_run_id: "run-1",
        matter_document_id: "document-1",
        task_version_id: "version-1",
        status: "COMPLETED",
        result_id: "result-1",
      } as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/runs/run-1/documents/document-1/decision-result") return {
        id: "result-1",
        review_batch_run_id: "run-1",
        workflow_run_id: "workflow-1",
        matter_document_id: "document-1",
        matter_analysis_task_version_id: "version-1",
        definition_content_hash: "a".repeat(64),
        specification_content_hash: "c".repeat(64),
        source_artifact_id: "artifact-1",
        source_content_hash: "d".repeat(64),
        state_content_hash: "e".repeat(64),
        question_set_hash: "f".repeat(64),
        decision_policy_hash: "1".repeat(64),
        paragraph_map_version: "paragraph-map-v1",
        status: "PARTIAL",
        coverage: { complete: false, evidence_complete: false },
        answers: { "privilege.legal_advice": { type: "noul", noul: 0.93 } },
        recommendations: { potentially_privileged: { matched: true } },
        routes: {},
        evidence: {},
        raw_answer_hash: "2".repeat(64),
        policy_evaluation_hash: "3".repeat(64),
        engine_key: "jev",
        provider: "typesafe",
        model: "jev-test",
        provider_request_id: "provider-1",
        provider_metadata: {},
        evaluation_skill_run_id: "skill-run-2",
        evidence_skill_run_id: null,
        model_invocation_id: "invocation-1",
        reused_from_result_id: null,
        attempts: 1,
        request_count: 1,
        input_tokens: 42,
        output_tokens: 5,
        latency_ms: 120,
        error_code: null,
        error_message: null,
        started_at: "2026-09-26T12:20:00Z",
        completed_at: "2026-09-26T12:20:01Z",
        created_at: "2026-09-26T12:20:01Z",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(<QueryClientProvider client={client}><AnalysisTasksPanel matterId="matter-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Privilege review" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Single-document playground" })).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Ready batch" }));
    await user.click(screen.getByRole("option", { name: "Privilege sample · 1 docs" }));
    await user.click(screen.getByRole("combobox", { name: "Document" }));
    await user.click(await screen.findByRole("option", { name: "#1 · item-1" }));
    await user.click(screen.getByRole("button", { name: "Run document" }));

    expect(await screen.findByText("Evidence localization pending")).toBeInTheDocument();
    expect(screen.getByText(/0.93/)).toBeInTheDocument();
    expect(screen.getByText(/42 input tokens · 5 output tokens/)).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(coreApi)).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks/task-1/versions/1/playground-runs",
      expect.objectContaining({ method: "POST" }),
    ));
  });
});
