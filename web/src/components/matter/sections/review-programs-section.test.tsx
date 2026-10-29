import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewProgramsSection } from "@/components/matter/sections/review-programs-section";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/matter/matter-context", () => ({
  useMatter: () => ({ clientId: "client-1", matterId: "matter-1", permissionKeys: new Set(["review_program.view", "review_program.manage"]) }),
}));
const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/clients/client-1/matters/matter-1/review-programs",
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams("program=program-1&action=action-1"),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><ReviewProgramsSection /></QueryClientProvider>);
}

describe("ReviewProgramsSection", () => {
  it("guides an unconfigured program into Matter Definition setup", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "MATTER_DEFINITION", name: "Matter definition", phase_key: "MATTER_DEFINITION", ordinal: 1, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Investigation team review",
        status: "DRAFT",
        current_run_id: null,
        current_version: { version: 1 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("This program has not been configured yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start run" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Start Setup" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("warns before replacing an unfinished failed run", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "CLASSIFIER_TRAINING", name: "Classifier training", phase_key: "CLASSIFIER", ordinal: 11, batching_policy: "NONE", requires_human_gate: false }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-failed",
        current_version: { version: 2 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-failed",
        program_id: "program-1",
        status: "FAILED",
        current_stage: "CLASSIFIER_TRAINING",
        current_stage_ordinal: 11,
        progress_summary: {
          classifier_training: {
            state: "FAILED",
            models: [{
              metadata_definition_id: "field-1",
              field_key: "issue_tag",
              field_name: "Issue Tag",
              classification_model_id: "model-1",
            }],
          },
          training_labels: { classifier_metadata_definition_ids: ["field-1"] },
        },
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: "Database storage was temporarily unavailable.",
        stage_attempts: [{ stage_key: "CLASSIFIER_TRAINING", status: "FAILED" }],
        actions: [],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && options?.method === "POST") return {
        id: "run-replacement",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open model" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/classification-models/model-1");
    const classifierStage = screen.getByText("Classifier training").closest("li");
    expect(classifierStage).not.toBeNull();
    expect(within(classifierStage as HTMLElement).getByText("Classifier training failed")).toBeInTheDocument();
    expect(within(classifierStage as HTMLElement).getByText("Database storage was temporarily unavailable.")).toBeInTheDocument();
    expect(within(classifierStage as HTMLElement).getByText(/same frozen labels and configuration/)).toBeInTheDocument();
    expect(within(classifierStage as HTMLElement).getByRole("button", { name: "Retry Classifier training" })).toBeInTheDocument();
    expect(within(classifierStage as HTMLElement).queryByRole("button", { name: "Start replacement run" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Start replacement run" }));
    expect(screen.getByRole("heading", { name: "Start a replacement run?" })).toBeInTheDocument();
    expect(screen.getByText(/failed during Classifier Training/)).toBeInTheDocument();
    expect(screen.queryByText(/Use Retry to continue the existing run/)).not.toBeInTheDocument();
    expect(vi.mocked(coreApi).mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);

    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Start replacement run" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining('"confirm_replace_unfinished_run":true'),
      }),
    ));
  });

  it("reports live document progress for a repeated definition assessment", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [
          { key: "DEFINITION_ASSESSMENT", name: "Definition assessment", phase_key: "MATTER_DEFINITION", ordinal: 2, batching_policy: "AD_HOC_BATCH", requires_human_gate: false },
          { key: "DEFINITION_REFINEMENT", name: "Definition refinement", phase_key: "MATTER_DEFINITION", ordinal: 3, batching_policy: "NONE", requires_human_gate: true },
        ],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 2 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
        current_stage: "DEFINITION_ASSESSMENT",
        current_stage_ordinal: 2,
        progress_summary: { current_assessment_id: "assessment-2", definition_assessment_round: 2 },
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [
          { stage_key: "DEFINITION_ASSESSMENT", attempt: 1, status: "FAILED", error_summary: "Earlier retrieval failed." },
          { stage_key: "DEFINITION_ASSESSMENT", attempt: 2, status: "COMPLETED" },
          { stage_key: "DEFINITION_REFINEMENT", attempt: 1, status: "COMPLETED" },
          { stage_key: "DEFINITION_ASSESSMENT", attempt: 3, status: "RUNNING" },
        ],
        actions: [],
      }] as never;
      if (path === "/v1/matters/matter-1/definition-assessments") return [{
        id: "assessment-2",
        guidance_id: "guidance-1",
        status: "SUMMARIZING",
        requested_document_count: 1000,
        selected_count: 1000,
        summarized_count: 50,
        skipped_count: 2,
        failed_count: 3,
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Definition reassessment · Round 2")).toBeInTheDocument();
    expect(screen.getByText("55 of 1,000 documents processed")).toBeInTheDocument();
    expect(screen.getByText("50 summarized · 2 skipped · 3 failed")).toBeInTheDocument();
    const initialAssessmentStage = screen.getByText("Definition assessment").closest("li");
    const refinementStage = screen.getByText("Definition refinement").closest("li");
    expect(initialAssessmentStage).not.toBeNull();
    expect(refinementStage).not.toBeNull();
    expect(within(initialAssessmentStage as HTMLElement).getByText("completed")).toBeInTheDocument();
    expect(within(initialAssessmentStage as HTMLElement).queryByText("running")).not.toBeInTheDocument();
    expect(within(refinementStage as HTMLElement).getByText("Reassessment · Round 2")).toBeInTheDocument();
    expect(within(refinementStage as HTMLElement).getByText("running")).toBeInTheDocument();
    const refreshState = screen.getByRole("button", { name: "Refresh state" });
    expect(refreshState).toHaveAttribute("aria-describedby", "review-program-refresh-state-help");
    expect(screen.getByText("Checks pending events and completed work, then updates the program if its recorded state is behind. It does not retry work.")).toHaveAttribute("role", "tooltip");
    expect(screen.queryByRole("button", { name: "Reconcile" })).not.toBeInTheDocument();
    const assessmentHelp = screen.getByRole("button", { name: "About Definition assessment" });
    expect(assessmentHelp).toHaveAttribute("aria-describedby", "review-program-stage-2-help");
    expect(screen.getByText("Test the Matter Definition against a targeted document batch and identify gaps, ambiguities, and retrieval weaknesses.")).toHaveAttribute("role", "tooltip");
    expect(screen.getByRole("link", { name: "Open current assessment" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/definition?assessment=assessment-2&guidance=guidance-1",
    );
  });

  it("configures a new Matter Definition through the guided setup action", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "MATTER_DEFINITION", name: "Matter definition", phase_key: "MATTER_DEFINITION", ordinal: 1, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/guidance") return [] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 1 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
        current_stage: "MATTER_DEFINITION",
        current_stage_ordinal: 1,
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [{ stage_key: "MATTER_DEFINITION", status: "WAITING" }],
        actions: [{
          id: "action-1",
          action_type: "MATTER_DEFINITION_SETUP",
          stage_key: "MATTER_DEFINITION",
          status: "OPEN",
          title: "Set up the Matter Definition",
          explanation: "Create, clone, or adopt a definition before assessment.",
          preview_hash: "b".repeat(64),
          allowed_choices: [
            { key: "CREATE", label: "Create definition", consequence: "Create a definition." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/definition-setup") && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    await userEvent.type(await screen.findByLabelText("Definition name"), "Hurricane response");
    await userEvent.type(screen.getByLabelText("Matter Definition"), "Review hurricane disruption communications.");
    await userEvent.click(screen.getByRole("button", { name: "Save and assess" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/definition-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/definition-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual(expect.objectContaining({
      preview_hash: "b".repeat(64),
      mode: "CREATE",
      definition_key: "hurricane-response",
      name: "Hurricane response",
      content_markdown: "Review hurricane disruption communications.",
      assessment_target_document_count: 500,
      assessment_control_sample_size: 0,
      assessment_use_batching: true,
      acknowledge_large_run_warning: false,
    }));
  });

  it("selects coding fields and starts generated Q&A task setup", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "QA_TASK_SETUP", name: "Q&A task setup", phase_key: "DISCOVERY_QA", ordinal: 5, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [{
        id: "field-1",
        key: "responsive",
        display_name: "Responsive",
        description: null,
        type: "BOOLEAN",
        cardinality: "SINGLE",
        allowed_values: null,
        value_source: "ASSERTED",
        assertion_policy: "IMMEDIATE",
        resolution_policy: "EXPLICIT_ONLY",
        searchable: true,
        facetable: true,
        normalize_to_lowercase: false,
        reviewable: true,
        ai_assignable: true,
        status: "ACTIVE",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 2 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
        current_stage: "QA_TASK_SETUP",
        current_stage_ordinal: 5,
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [{ stage_key: "QA_TASK_SETUP", status: "WAITING" }],
        actions: [{
          id: "action-1",
          action_type: "QA_TASK_SETUP",
          stage_key: "QA_TASK_SETUP",
          status: "OPEN",
          title: "Generate the first-pass Q&A task",
          explanation: "Choose fields and generate the task.",
          preview_hash: "b".repeat(64),
          allowed_choices: [
            { key: "GENERATE", label: "Generate Q&A task", consequence: "Generate it." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/qa-setup") && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    await userEvent.click(await screen.findByRole("checkbox", { name: /Responsive/ }));
    await userEvent.click(screen.getByRole("button", { name: "Generate Q&A task" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/qa-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/qa-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      task_key: "first_pass_qa",
      name: "First-pass Q&A",
      description: "Generated from the accepted Matter Definition.",
      destination_metadata_definition_ids: ["field-1"],
    });
  });

  it("previews and starts a frozen dual-profile discovery run", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DISCOVERY_QA", name: "Discovery Q&A", phase_key: "TRAINING_SET", ordinal: 6, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 3 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DISCOVERY_QA",
        current_stage_ordinal: 6, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DISCOVERY_QA", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "DISCOVERY_QA_SETUP", stage_key: "DISCOVERY_QA", status: "OPEN",
          title: "Start discovery Q&A", explanation: "Freeze one population and run both profiles.", preview_hash: "b".repeat(64),
          decision_context: {
            retrieval_plan: { query_count: 4 }, search_index_generation: { generation: 7 },
            defaults: { target_document_count: 500, target_batch_size: 100, keep_families_together: true },
          },
          allowed_choices: [{ key: "START", label: "Start discovery Q&A", consequence: "Start it." }, { key: "STOP", label: "Stop program", consequence: "End this run." }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/discovery-setup") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("4")).toBeInTheDocument();
    expect(screen.getByText("Generation 7")).toBeInTheDocument();
    expect(screen.queryByLabelText("Broad control")).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Document target"));
    await userEvent.type(screen.getByLabelText("Document target"), "800");
    await userEvent.click(screen.getByRole("button", { name: "Start discovery Q&A" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/discovery-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/discovery-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      target_document_count: 800,
      target_batch_size: 100,
      keep_families_together: true,
    });
  });

  it("cancels an active Discovery Q&A task before offering reset on the task", async () => {
    let discoveryCanceled = false;
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DISCOVERY_QA", name: "Discovery Q&A", phase_key: "TRAINING_SET", ordinal: 6, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 3 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: discoveryCanceled ? "WAITING_FOR_USER" : "RUNNING", current_stage: "DISCOVERY_QA",
        current_stage_ordinal: 6, current_discovery_round: 1, progress_summary: { discovery_qa_state: discoveryCanceled ? "CANCELED" : "RUNNING_PROFILES", discovery_qa: { batch_set_id: "set-1", task_id: "task-1", execution_profile_ids: ["profile-1", "profile-2"] } },
        created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DISCOVERY_QA", status: discoveryCanceled ? "CANCELED" : "RUNNING" }], actions: [],
      }] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/batch-sets/set-1") return { id: "set-1", status: "READY", population_count: 1500, batch_count: 3 } as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/question-answering-jobs?batch_set_id=set-1") return [
        { id: "jev-1", scope_id: "batch-1", review_batch_id: "batch-1", review_batch_name: "Batch 1", task_name: "Discovery", decision_provider: "JEV", model_key: "jev-latest", status: "COMPLETED", successful_document_count: 500, document_count: 500, failed_document_count: 0 },
        { id: "openai-1", scope_id: "batch-1", review_batch_id: "batch-1", review_batch_name: "Batch 1", task_name: "Discovery", decision_provider: "OPENAI_DECISIONS", model_key: "gpt-6-luna", status: "COMPLETED", successful_document_count: 500, document_count: 500, failed_document_count: 0 },
        { id: "jev-2", scope_id: "batch-2", review_batch_id: "batch-2", review_batch_name: "Batch 2", task_name: "Discovery", decision_provider: "JEV", model_key: "jev-latest", status: "RUNNING", successful_document_count: 230, document_count: 500, failed_document_count: 0 },
        { id: "openai-2", scope_id: "batch-2", review_batch_id: "batch-2", review_batch_name: "Batch 2", task_name: "Discovery", decision_provider: "OPENAI_DECISIONS", model_key: "gpt-6-luna", status: "QUEUED", successful_document_count: 0, document_count: 500, failed_document_count: 0 },
      ] as never;
      if (path.endsWith("/cancel-discovery-qa") && options?.method === "POST") {
        discoveryCanceled = true;
        return { id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER" } as never;
      }
      if (path.endsWith("/reset-discovery-qa") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Reviewing documents")).toBeInTheDocument();
    expect(screen.getByText("1 of 3 batches complete · 1 running · 1 queued")).toBeInTheDocument();
    expect(screen.getByText("1,230 of 3,000 successful")).toBeInTheDocument();
    expect(screen.getByText("Jev 730/1,500 · OpenAI Decisions 500/1,500")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View batch progress" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/jobs/question-answering?batch_set=set-1");
    expect(screen.getByText("Discovery Q&A").closest("li")).toHaveClass("sm:col-span-2");
    expect(screen.queryByRole("button", { name: "Reset Discovery Q&A" })).not.toBeInTheDocument();
    const task = screen.getByText("Discovery Q&A").closest("li");
    expect(task).not.toBeNull();
    await userEvent.click(within(task!).getByRole("button", { name: "Cancel Discovery Q&A" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/stops the active Batch Set/)).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel Discovery Q&A" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/cancel-discovery-qa",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(await screen.findByText("Canceled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel Discovery Q&A" })).not.toBeInTheDocument();
    const canceledTask = screen.getByText("Discovery Q&A").closest("li");
    expect(canceledTask).not.toBeNull();
    await userEvent.click(within(canceledTask!).getByRole("button", { name: "Reset Discovery Q&A" }));
    const resetDialog = await screen.findByRole("dialog");
    expect(within(resetDialog).getByText(/archives the canceled Discovery Q&A Batch Set/)).toBeInTheDocument();
    await userEvent.click(within(resetDialog).getByRole("button", { name: "Reset Discovery Q&A" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/reset-discovery-qa",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/reset-discovery-qa"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual(expect.objectContaining({
      reason: "Reset from the Review Program workspace",
    }));
  });

  it("shows discovery alignment and starts a supplemental retrieval round", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "ISSUE_SUPPORT_DECISION", name: "Issue support decision", phase_key: "TRAINING_SET", ordinal: 8, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 4 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "ISSUE_SUPPORT_DECISION",
        current_stage_ordinal: 8, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "ISSUE_SUPPORT_DECISION", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "ISSUE_SUPPORT_DECISION", stage_key: "ISSUE_SUPPORT_DECISION", status: "OPEN",
          title: "Resolve low-support coding categories", explanation: "Review alignment before classifier training.", preview_hash: "b".repeat(64),
          decision_context: {
            discovery_analysis: {
              support_status: "INSUFFICIENT",
              batch_count: 2,
              policy: { minimum_positive_support: 25, minimum_negative_support: 25 },
              overall: { exact_count: 31, partial_count: 2, disagreement_count: 3, incomplete_count: 1 },
              fields: [{
                key: "responsive",
                display_name: "Responsiveness",
                exact_count: 20,
                partial_count: 0,
                disagreement_count: 3,
                incomplete_count: 1,
                classes: [
                  { value_key: "true", left_selected_count: 4, right_selected_count: 3, intersection_count: 3, union_count: 4, negative_count: 32, disagreement_count: 1, support_status: "LOW_SUPPORT" },
                  { value_key: "false", left_selected_count: 32, right_selected_count: 33, intersection_count: 32, union_count: 33, negative_count: 3, disagreement_count: 1, support_status: "SUFFICIENT" },
                ],
              }, {
                key: "issue",
                display_name: "Issue",
                exact_count: 11,
                partial_count: 2,
                disagreement_count: 0,
                incomplete_count: 0,
                classes: [
                  { value_key: "coverage", left_selected_count: 3, right_selected_count: 2, intersection_count: 2, union_count: 3, negative_count: 33, disagreement_count: 1, support_status: "LOW_SUPPORT" },
                ],
              }],
              low_support: [
                { field_key: "responsive", value_key: "true", positive_count: 4, negative_count: 32 },
                { field_key: "issue", value_key: "coverage", positive_count: 3, negative_count: 33 },
              ],
            },
          },
          allowed_choices: [
            { key: "SUPPLEMENT", label: "Review more documents", consequence: "Run another discovery round." },
            { key: "IGNORE_LOW_SUPPORT", label: "Accept limitations", consequence: "Proceed with limitations." },
            { key: "REFINE_DEFINITION", label: "Refine Matter Definition", consequence: "Return to definition refinement." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DISCOVERY_QA",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Insufficient")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Discovery analysis" })).toBeInTheDocument();
    expect(screen.getByText("Frozen comparison of the two pinned Q&A profiles across 2 batches.")).toBeInTheDocument();
    expect(screen.getByText("31")).toBeInTheDocument();
    expect(screen.getByText(/Minimum support per category:/)).toHaveTextContent("25 positive and 25 negative examples");
    const responsiveness = screen.getByRole("heading", { name: "Responsiveness" }).closest("section");
    expect(responsiveness).not.toBeNull();
    expect(within(responsiveness!).getByRole("row", { name: /True 4 3 3 4 32 1 Low support/ })).toBeInTheDocument();
    expect(within(responsiveness!).getByRole("row", { name: /False 32 33 32 33 3 1 Sufficient/ })).toBeInTheDocument();
    const issue = screen.getByRole("heading", { name: "Issue" }).closest("section");
    expect(issue).not.toBeNull();
    expect(within(issue!).getByRole("row", { name: /Coverage 3 2 2 3 33 1 Low support/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review more documents" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "SUPPLEMENT",
    });
  });

  it("presents the three classifier-label policies and starts difference review", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DISAGREEMENT_REVIEW", name: "Difference review", phase_key: "TRAINING_SET", ordinal: 9, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 5 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DISAGREEMENT_REVIEW",
        current_stage_ordinal: 9, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DISAGREEMENT_REVIEW", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "TRAINING_LABEL_POLICY", stage_key: "DISAGREEMENT_REVIEW", status: "OPEN",
          title: "Choose the classifier training labels", explanation: "Choose how the two Q&A results become labels.", preview_hash: "b".repeat(64),
          decision_context: {
            discovery_analysis: {
              profile_run_ids: ["run-a", "run-b"],
              overall: { exact_count: 31, partial_count: 2, disagreement_count: 3 },
              fields: [
                { metadata_definition_id: "11111111-1111-4111-8111-111111111111", key: "responsive", display_name: "Responsiveness", partial_count: 2, disagreement_count: 3 },
                { metadata_definition_id: "22222222-2222-4222-8222-222222222222", key: "privilege", display_name: "Privilege", partial_count: 0, disagreement_count: 2 },
              ],
            },
            training_label_resolution: {},
          },
          allowed_choices: [
            { key: "USE_ALL_MODEL_CODES", label: "Use all model codings", consequence: "Use both sources and exclude conflicts." },
            { key: "USE_OVERLAPPING_VALUES", label: "Use overlapping values", consequence: "Keep only values selected by both models." },
            { key: "USE_EXACT_AGREEMENT", label: "Use exact model agreements", consequence: "Use exact matches." },
            { key: "REVIEW_DISAGREEMENTS", label: "Review model differences", consequence: "Create review batches." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DISAGREEMENT_REVIEW",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Not selected")).toBeInTheDocument();
    expect(screen.getByText("Model differences").nextElementSibling).toHaveTextContent("5");
    expect(screen.getByText(/One consolidated ad hoc review batch will be created for each selected field/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Responsiveness/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Privilege/ })).toBeChecked();
    const overlappingButton = screen.getByRole("button", { name: "Use overlapping values" });
    expect(overlappingButton).toHaveAttribute("aria-describedby", "review-program-choice-use_overlapping_values-help");
    expect(screen.getByText("Keep only values selected by both models.")).toHaveAttribute("role", "tooltip");
    await userEvent.click(screen.getByRole("checkbox", { name: /Privilege/ }));
    await userEvent.click(screen.getByRole("button", { name: "Review model differences" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "REVIEW_DISAGREEMENTS",
      input: { metadata_definition_ids: ["11111111-1111-4111-8111-111111111111"] },
    });
  });

  it("links live difference batches and allows partial reconciliation", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DISAGREEMENT_REVIEW", name: "Difference review", phase_key: "TRAINING_SET", ordinal: 9, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 6 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DISAGREEMENT_REVIEW",
        current_stage_ordinal: 9, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DISAGREEMENT_REVIEW", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "DISAGREEMENT_REVIEW_COMPLETION", stage_key: "DISAGREEMENT_REVIEW", status: "OPEN",
          title: "Choose a difference reconciliation path", explanation: "Review as many differences as needed.", preview_hash: "b".repeat(64),
          decision_context: {
            discovery_analysis: { profile_run_ids: ["run-a", "run-b"], overall: { exact_count: 31, partial_count: 2, disagreement_count: 3 } },
            training_label_resolution: {
              policy: "ADJUDICATED_DISAGREEMENTS", profile_run_ids: ["run-a", "run-b"],
              difference_batches: [{ id: "batch-1", name: "Responsive differences", status: "READY", document_count: 2, completed_document_count: 1 }],
            },
          },
          allowed_choices: [
            { key: "CONFIRM_ADJUDICATION", label: "Require complete review", consequence: "Freeze every reviewer decision." },
            { key: "CONFIRM_PARTIAL_ADJUDICATION", label: "Use reviewed decisions", consequence: "Exclude unresolved differences." },
            { key: "USE_OVERLAPPING_VALUES", label: "Use overlapping values", consequence: "Keep only values selected by both models." },
            { key: "USE_EXACT_AGREEMENT", label: "Use exact model agreements", consequence: "Use exact agreements." },
            { key: "USE_ALL_MODEL_CODES", label: "Use all model codings", consequence: "Exclude conflicts." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "TRAINING_LABEL_FINALIZATION",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Adjudicated Disagreements")).toBeInTheDocument();
    expect(screen.getByText("Responsive differences")).toBeInTheDocument();
    expect(screen.getByText(/1 of 2 completed/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open review batch" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    await userEvent.click(screen.getByRole("button", { name: "Use reviewed decisions" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "CONFIRM_PARTIAL_ADJUDICATION",
    });
  });

  it("shows policy-adjusted per-label counts before finalizing training labels", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "TRAINING_LABEL_FINALIZATION", name: "Finalize labels", phase_key: "TRAINING_SET", ordinal: 10, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 7 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "TRAINING_LABEL_FINALIZATION",
        current_stage_ordinal: 10, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "TRAINING_LABEL_FINALIZATION", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "TRAINING_LABEL_FINALIZATION", stage_key: "TRAINING_LABEL_FINALIZATION", status: "OPEN",
          title: "Finalize the classifier training labels", explanation: "Freeze the selected policy.", preview_hash: "b".repeat(64),
          decision_context: {
            discovery_analysis: { profile_run_ids: ["run-a", "run-b"], overall: { exact_count: 31, partial_count: 2, disagreement_count: 3 } },
            training_label_resolution: { policy: "OVERLAPPING_VALUES", profile_run_ids: ["run-a", "run-b"] },
            training_label_preview: {
              policy: "OVERLAPPING_VALUES",
              fields: [{
                metadata_definition_id: "field-1", key: "issue_tag", display_name: "Issue Tag",
                eligible_document_count: 28, excluded_document_count: 8, basis: "Values selected by both models",
                values: [
                  { value_key: "issue_1", value_label: "Market disruption", positive_count: 17 },
                  { value_key: "issue_2", value_label: "Hurricane losses", positive_count: 9 },
                ],
              }, {
                metadata_definition_id: "field-2", key: "responsiveness", display_name: "Responsiveness",
                eligible_document_count: 31, excluded_document_count: 5, basis: "Values selected by both models",
                values: [
                  { value_key: "true", value_label: "Responsive", positive_count: 20 },
                  { value_key: "false", value_label: "Not responsive", positive_count: 11 },
                ],
              }],
            },
          },
          allowed_choices: [
            { key: "FINALIZE", label: "Finalize training labels", consequence: "Freeze this label set." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "CLASSIFIER_TRAINING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Final training-label counts")).toBeInTheDocument();
    expect(screen.getByText("28 eligible · 8 excluded")).toBeInTheDocument();
    expect(screen.getByText("Market disruption").nextElementSibling).toHaveTextContent("17");
    expect(screen.getByText("Hurricane losses").nextElementSibling).toHaveTextContent("9");
    expect(screen.getByText("Fields to train")).toBeInTheDocument();
    expect(screen.getByLabelText("Train Issue Tag")).toBeChecked();
    expect(screen.getByLabelText("Train Responsiveness")).toBeChecked();
    await userEvent.click(screen.getByLabelText("Train Responsiveness"));
    await userEvent.click(screen.getByRole("button", { name: "Finalize training labels" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "FINALIZE",
      input: { classifier_metadata_definition_ids: ["field-1"] },
    });
  });

  it("shows the current gate and resolves it through the coordinator endpoint", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW",
        version: 1,
        schema_version: 1,
        content_hash: "a".repeat(64),
        name: "First Pass Review",
        description: "Guided review",
        workflow_type: "FIRST_PASS_REVIEW",
        phases: [],
        stages: [{ key: "DEFINITION_ACCEPTANCE", name: "Definition acceptance", phase_key: "MATTER_DEFINITION", ordinal: 4, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1",
        tenant_id: "tenant-1",
        matter_id: "matter-1",
        template_key: "FIRST_PASS_REVIEW",
        template_version: 1,
        template_content_hash: "a".repeat(64),
        key: "standard-first-pass",
        name: "Standard first pass",
        description: "Responsive review",
        status: "ACTIVE",
        current_run_id: "run-1",
        current_version: { version: 1 },
        created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1",
        program_id: "program-1",
        status: "WAITING_FOR_USER",
        current_stage: "DEFINITION_ACCEPTANCE",
        current_stage_ordinal: 4,
        created_at: "2026-10-21T00:00:00Z",
        failure_retriable: false,
        failure_summary: null,
        stage_attempts: [{ stage_key: "DEFINITION_ACCEPTANCE", status: "WAITING" }],
        actions: [{
          id: "action-1",
          action_type: "HUMAN_DECISION",
          stage_key: "DEFINITION_ACCEPTANCE",
          status: "OPEN",
          title: "Review Definition acceptance",
          explanation: "Confirm the definition before Q&A setup.",
          preview_hash: "b".repeat(64),
          decision_context: {
            matter_definition_id: "definition-2",
            assessment: {
              id: "assessment-2",
              status: "COMPLETED",
              summarized_count: 1000,
              failed_count: 0,
              question_counts: { open: 0, open_blocking: 0 },
              guidance_refinement_status: "COMPLETED",
            },
          },
          allowed_choices: [
            { key: "CONTINUE", label: "Continue to Q&A task setup", consequence: "Advance the program." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1",
        program_id: "program-1",
        status: "RUNNING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Confirm the definition before Q&A setup.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Matter Definition" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/definition?guidance=definition-2&assessment=assessment-2",
    );
    await userEvent.click(screen.getByRole("button", { name: "Continue to Q&A task setup" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ preview_hash: "b".repeat(64), choice_key: "CONTINUE" });
  });

  it("requires an explicit Matter Definition publication before reassessment", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DEFINITION_REFINEMENT", name: "Definition refinement", phase_key: "MATTER_DEFINITION", ordinal: 3, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 3 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DEFINITION_REFINEMENT",
        current_stage_ordinal: 3, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DEFINITION_REFINEMENT", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "MATTER_DEFINITION_REFINEMENT", stage_key: "DEFINITION_REFINEMENT", status: "OPEN",
          title: "Review assessment findings", explanation: "Legacy action copy", preview_hash: "b".repeat(64),
          decision_context: {
            matter_definition_id: "definition-1",
            assessment: {
              id: "assessment-1", status: "COMPLETED", summarized_count: 1000, failed_count: 0,
              question_counts: { open: 0, open_blocking: 0, answered: 2, dismissed: 0 },
              guidance_refinement_status: "COMPLETED", refined_matter_definition_revision: 2,
              publication_required: true, publication_ready: false,
            },
          },
          allowed_choices: [
            { key: "CONTINUE", label: "Check readiness and continue", consequence: "Continue." },
            { key: "STOP", label: "Stop program", consequence: "Stop." },
          ],
        }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Draft revision 2 is ready. Review it and publish it before continuing.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review and publish draft" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/definition?guidance=definition-1&assessment=assessment-1",
    );
    expect(screen.getByRole("button", { name: "Continue to reassessment" })).toBeDisabled();
  });

  it("replaces reassessment with an explicit final-round decision at the assessment limit", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DEFINITION_REFINEMENT", name: "Definition refinement", phase_key: "MATTER_DEFINITION", ordinal: 3, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 4 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DEFINITION_REFINEMENT",
        current_stage_ordinal: 3, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DEFINITION_REFINEMENT", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "MATTER_DEFINITION_REFINEMENT", stage_key: "DEFINITION_REFINEMENT", status: "OPEN",
          title: "Review assessment findings", explanation: "Legacy action copy", preview_hash: "b".repeat(64),
          decision_context: {
            matter_definition_id: "definition-1",
            assessment: {
              id: "assessment-1", status: "COMPLETED", summarized_count: 993, failed_count: 7,
              question_counts: { open: 0, open_blocking: 0, answered: 9, dismissed: 0 },
              guidance_refinement_status: "COMPLETED", refined_matter_definition_revision: 4,
              publication_required: true, publication_ready: true,
              assessment_round: 3, assessment_round_limit: 3, assessment_round_limit_reached: true,
            },
          },
          allowed_choices: [
            { key: "CONTINUE", label: "Check readiness and continue", consequence: "Continue." },
            { key: "STOP", label: "Stop program", consequence: "Stop." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DEFINITION_ACCEPTANCE",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText(/assessment round 3 reached the configured limit of 3/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue to reassessment" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run additional validation assessment" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Accept revision 4 and continue to Q&A setup" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "CONTINUE",
      input: { final_round_decision: "ACCEPT_REFINEMENT" },
    });
  });

  it("reconciles a legacy duplicate acceptance gate after final acceptance was recorded", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "DEFINITION_ACCEPTANCE", name: "Definition acceptance", phase_key: "MATTER_DEFINITION", ordinal: 4, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 4 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "DEFINITION_ACCEPTANCE",
        current_stage_ordinal: 4, progress_summary: { final_unassessed_refinement: { accepted_revision: 4 } },
        created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "DEFINITION_ACCEPTANCE", status: "WAITING" }], actions: [],
      }] as never;
      if (path.endsWith("/runs/run-1/reconcile") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "QA_TASK_SETUP",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/reconcile",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("summarizes classifier quality and routes weak categories to improvement", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_REVIEW", name: "Classifier review", phase_key: "CLASSIFIER", ordinal: 12, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 8 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "CLASSIFIER_REVIEW",
        current_stage_ordinal: 12, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "CLASSIFIER_REVIEW", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "CLASSIFIER_REVIEW", stage_key: "CLASSIFIER_REVIEW", status: "OPEN",
          title: "Review the trained classifiers", explanation: "Review quality before collection scoring.", preview_hash: "b".repeat(64),
          decision_context: {
            classifier_results: {
              overall_quality: "WEAK", model_count: 1, weak_model_count: 1, weak_category_count: 1,
              models: [{
                classification_model_id: "model-1", field_name: "Responsive", quality: "WEAK",
                training_eligible_count: 42, metrics: { primary_pr_auc: 0.612, macro_f1: 0.554 },
                weak_categories: [{ class_key: "true", reasons: ["LOW_F1"] }],
              }],
            },
          },
          allowed_choices: [
            { key: "ACCEPT_CLASSIFIERS", label: "Accept classifiers", consequence: "Continue." },
            { key: "IMPROVE_CLASSIFIERS", label: "Improve weak categories", consequence: "Review more data." },
            { key: "STOP", label: "Stop program", consequence: "End this run." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "CLASSIFIER_IMPROVEMENT",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Overall quality")).toBeInTheDocument();
    expect(screen.getAllByText("Weak")).toHaveLength(2);
    expect(screen.getByText(/42 training documents · PR AUC 0.61 · Macro F1 0.55/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Improve weak categories" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "IMPROVE_CLASSIFIERS",
    });
  });

  it("edits and starts a task-specific classifier improvement plan", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_IMPROVEMENT", name: "Classifier improvement", phase_key: "CLASSIFIER", ordinal: 13, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 9 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "CLASSIFIER_IMPROVEMENT",
        current_stage_ordinal: 13, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "CLASSIFIER_IMPROVEMENT", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "CLASSIFIER_IMPROVEMENT_SETUP", stage_key: "CLASSIFIER_IMPROVEMENT", status: "OPEN",
          title: "Target weak classifier categories", explanation: "Edit the searches before review.", preview_hash: "c".repeat(64),
          decision_context: {
            retrieval_plan: { targets: [{
              metadata_definition_id: "definition-1", field_name: "Responsive", class_key: "true", class_label: "Responsive",
              quality_reasons: ["LOW_F1"], suggested_query: "Responsive true", suggested_quota: 50,
            }] },
            defaults: { control_sample_size: 25, target_batch_size: 100, keep_families_together: true },
          },
          allowed_choices: [{ key: "START", label: "Start targeted review" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/classifier-improvement-setup") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "CLASSIFIER_IMPROVEMENT",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    const query = await screen.findByLabelText("Responsive · Responsive");
    await userEvent.clear(query);
    await userEvent.type(query, "edited responsive search");
    const quota = screen.getByLabelText("Target");
    await userEvent.clear(quota);
    await userEvent.type(quota, "75");
    const control = screen.getByLabelText("Broad control");
    await userEvent.clear(control);
    await userEvent.type(control, "10");
    await userEvent.click(screen.getByRole("button", { name: "Start targeted review" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/classifier-improvement-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/classifier-improvement-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "c".repeat(64),
      queries: [{
        metadata_definition_id: "definition-1",
        class_key: "true",
        query: "edited responsive search",
        quota: 75,
      }],
      control_sample_size: 10,
      target_batch_size: 100,
      keep_families_together: true,
    });
  });

  it("summarizes accepted classifiers and starts whole-collection scoring", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [
          { key: "CLASSIFIER_IMPROVEMENT", name: "Classifier improvement", phase_key: "CLASSIFIER", ordinal: 13, batching_policy: "PARTITIONED_SET", requires_human_gate: true },
          { key: "WHOLE_COLLECTION_SCORING", name: "Whole-collection scoring", phase_key: "COLLECTION_REVIEW", ordinal: 14, batching_policy: "WHOLE_MATTER", requires_human_gate: true },
        ],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 10 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "WHOLE_COLLECTION_SCORING",
        current_stage_ordinal: 14, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "WHOLE_COLLECTION_SCORING", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "WHOLE_COLLECTION_SCORING", stage_key: "WHOLE_COLLECTION_SCORING", status: "OPEN",
          title: "Run the accepted classifiers across the collection", explanation: "Create frozen whole-matter scoring inputs and run every accepted classifier.", preview_hash: "d".repeat(64),
          decision_context: {
            scoring_plan: {
              scope_type: "MATTER", model_count: 1,
              models: [{ classification_model_id: "model-1", field_name: "Responsive", quality: "ACCEPTABLE" }],
            },
          },
          allowed_choices: [{ key: "START_SCORING", label: "Score the collection" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "WHOLE_COLLECTION_SCORING",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Whole matter")).toBeInTheDocument();
    expect(screen.getByText("Responsive")).toBeInTheDocument();
    expect(screen.getByText("Accepted version · Acceptable")).toBeInTheDocument();
    expect(screen.getByText("Calculated at launch")).toBeInTheDocument();
    expect(screen.getByText("Skipped")).toBeInTheDocument();
    expect(screen.getByText("Not requested; the accepted classifiers continue directly to whole-collection scoring.")).toBeInTheDocument();
    expect(screen.getByText("Waiting for your decision")).toBeInTheDocument();
    expect(screen.getByText(/Scoring has not started/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Score the collection" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "d".repeat(64),
      choice_key: "START_SCORING",
    });
  });

  it("previews classifier-positive coverage and starts both Q&A reviewers", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 11 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "CLASSIFIER_POSITIVE_COVERAGE",
        current_stage_ordinal: 15, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "CLASSIFIER_POSITIVE_COVERAGE", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "CLASSIFIER_POSITIVE_COVERAGE", stage_key: "CLASSIFIER_POSITIVE_COVERAGE", status: "OPEN",
          title: "Verify classifier-positive documents with both Q&A reviewers", explanation: "Freeze the uncovered classifier-positive population.", preview_hash: "e".repeat(64),
          decision_context: {
            whole_collection_scoring: {
              state: "COMPLETED", model_count: 1, collection_document_count: 240,
              aggregate_model_document_counts: { scored_document_count: 230 },
              models: [{ classification_model_id: "model-1", field_name: "Responsive", status: "COMPLETED", scored_document_count: 230, abstained_document_count: 4, excluded_document_count: 6 }],
            },
            coverage_plan: { required_profile_count: 2, maximum_candidate_model_document_count: 230, batching_policy: "PARTITIONED_SET" },
          },
          allowed_choices: [{ key: "RUN_COVERAGE", label: "Run dual-Q&A coverage" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "CLASSIFIER_POSITIVE_COVERAGE",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Required reviewers")).toBeInTheDocument();
    expect(screen.getByText("Candidate ceiling")).toBeInTheDocument();
    expect(screen.getByText("Task-specific set")).toBeInTheDocument();
    expect(screen.getByText(/Existing documents reviewed by both pinned profiles are excluded/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run dual-Q&A coverage" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "e".repeat(64),
      choice_key: "RUN_COVERAGE",
    });
  });

  it("configures the negative-population QC sampling plan", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "NO_CODE_QC", name: "Negative-population QC", phase_key: "QC_COMPLETION", ordinal: 17, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 12 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "NO_CODE_QC",
        current_stage_ordinal: 17, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "NO_CODE_QC", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "NEGATIVE_POPULATION_QC_SETUP", stage_key: "NO_CODE_QC", status: "OPEN",
          title: "Configure Negative-population QC", explanation: "Configure blind QC sampling.", preview_hash: "f".repeat(64),
          decision_context: {
            defaults: { random_sample_size: 200, near_threshold_sample_size: 100, cohort_control_sample_size: 50, mandatory_review_limit: 100, target_batch_size: 100, keep_families_together: true },
            final_alignment: {
              population_document_count: 180,
              alignment_counts: { EXACT_ALL: 120, PARTIAL: 12, DISAGREEMENT: 8 },
              source_coverage_counts: { CURRENT: 170, STALE_OR_PARTIAL: 4, MISSING_QA: 5, SOURCE_UNAVAILABLE: 1 },
              issues: [{ issue_key: "responsive:true" }, { issue_key: "privilege:true" }],
            },
          },
          allowed_choices: [{ key: "START_QC", label: "Start Negative-population QC" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-1/negative-population-qc-setup") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "NO_CODE_QC",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Aligned population")).toBeInTheDocument();
    expect(screen.getByText("All-source exact")).toBeInTheDocument();
    expect(screen.getByText("Disputed or partial")).toBeInTheDocument();
    expect(screen.getByText(/170 documents have current-source results/)).toBeInTheDocument();
    const randomAudit = screen.getByLabelText("Random audit");
    await userEvent.clear(randomAudit);
    await userEvent.type(randomAudit, "250");
    await userEvent.click(screen.getByRole("button", { name: "Start Negative-population QC" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/negative-population-qc-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/negative-population-qc-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "f".repeat(64),
      random_sample_size: 250,
      near_threshold_sample_size: 100,
      cohort_control_sample_size: 50,
      mandatory_review_limit: 100,
      target_batch_size: 100,
      keep_families_together: true,
    });
  });

  it("presents and accepts the negative-population QC report", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "NO_CODE_QC", name: "Negative-population QC", phase_key: "QC_COMPLETION", ordinal: 17, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 13 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "NO_CODE_QC",
        current_stage_ordinal: 17, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "NO_CODE_QC", status: "WAITING" }],
        actions: [{
          id: "action-2", action_type: "NEGATIVE_POPULATION_QC_REPORT", stage_key: "NO_CODE_QC", status: "OPEN",
          title: "Review Negative-population QC findings", explanation: "Review the blind audit.", preview_hash: "9".repeat(64),
          decision_context: { negative_population_qc: { report: {
            selected_document_count: 350, positive_document_count: 14, unresolved_document_count: 2,
            mandatory_unreviewable_count: 3, random_audit_estimate: { observed_positive_rate: 0.04 },
          } } },
          allowed_choices: [{ key: "ACCEPT_QC", label: "Accept QC results" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-2/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "COMPLETION",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("350")).toBeInTheDocument();
    expect(screen.getByText("4.0%")).toBeInTheDocument();
    expect(screen.getByText("Manual resolution")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept QC results" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-2/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-2/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "9".repeat(64),
      choice_key: "ACCEPT_QC",
    });
  });

  it("shows the completion checklist and records completion notes", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "COMPLETION", name: "Completion", phase_key: "QC_COMPLETION", ordinal: 18, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 1, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 14 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "COMPLETION",
        current_stage_ordinal: 18, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        progress_summary: {}, stage_attempts: [{ stage_key: "COMPLETION", status: "WAITING" }],
        actions: [{
          id: "action-3", action_type: "REVIEW_PROGRAM_COMPLETION", stage_key: "COMPLETION", status: "OPEN",
          title: "Complete first-pass review", explanation: "Review the final checklist.", preview_hash: "7".repeat(64),
          decision_context: { completion_preview: {
            checklist: {
              matter_definition_accepted: true, qa_task_published: true, training_labels_finalized: true,
              classifiers_accepted: true, whole_collection_scored: true, classifier_positives_covered: true,
              final_alignment_ready: true, negative_population_qc_accepted: true,
            },
            child_record_count: 21, resolved_decision_count: 12, unresolved_qc_document_count: 2,
            manual_resolution_document_count: 1, publication_effect: "NONE",
          } },
          allowed_choices: [{ key: "COMPLETE", label: "Complete first-pass review" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path.endsWith("/actions/action-3/resolve") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "COMPLETED", current_stage: "COMPLETION",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Matter Definition Accepted")).toBeInTheDocument();
    expect(screen.getAllByText("Ready")).toHaveLength(8);
    expect(screen.getByText(/does not publish model or Q&A coding/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Completion notes"), "Accepted after senior review.");
    await userEvent.click(screen.getByRole("button", { name: "Complete first-pass review" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-3/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-3/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "7".repeat(64),
      choice_key: "COMPLETE",
      reason: "Accepted after senior review.",
    });
  });
});
