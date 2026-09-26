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

const task = {
  id: "task-1",
  matter_id: "matter-1",
  key: "privilege_review",
  name: "Privilege review",
  description: "First-pass privilege review",
  task_type: "PRIVILEGE_REVIEW",
  workflow_key: "privilege_review_v1",
  current_version: 1,
  published_version: 1,
  status: "ACTIVE",
  created_by_user_id: "user-1",
  created_at: "2026-09-26T12:00:00Z",
  updated_at: "2026-09-26T12:10:00Z",
  version,
} as const;

describe("AnalysisTasksPanel", () => {
  it("launches a published task against one batch document and displays its result", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks") return [task] as never;
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
