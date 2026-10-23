import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
            defaults: { target_document_count: 500, control_sample_size: 50, target_batch_size: 100, keep_families_together: true },
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
      control_sample_size: 50,
      target_batch_size: 100,
      keep_families_together: true,
    });
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
              overall: { exact_count: 31, partial_count: 2, disagreement_count: 3, incomplete_count: 1 },
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
    expect(screen.getByText("31")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
    expect(screen.getByText("Low-support categories remain visible in the frozen analysis and require an explicit decision before classifier training.")).toBeInTheDocument();
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
            discovery_analysis: { profile_run_ids: ["run-a", "run-b"], overall: { exact_count: 31, partial_count: 2, disagreement_count: 3 } },
            training_label_resolution: {},
          },
          allowed_choices: [
            { key: "USE_ALL_MODEL_CODES", label: "Use all model codings", consequence: "Use both sources and exclude conflicts." },
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
    await userEvent.click(screen.getByRole("button", { name: "Review model differences" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "REVIEW_DISAGREEMENTS",
    });
  });

  it("links live ad hoc difference batches and confirms adjudication", async () => {
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
          title: "Complete the difference review", explanation: "Complete every document before continuing.", preview_hash: "b".repeat(64),
          decision_context: {
            discovery_analysis: { profile_run_ids: ["run-a", "run-b"], overall: { exact_count: 31, partial_count: 2, disagreement_count: 3 } },
            training_label_resolution: {
              policy: "ADJUDICATED_DISAGREEMENTS", profile_run_ids: ["run-a", "run-b"],
              difference_batches: [{ id: "batch-1", name: "Responsive differences", status: "READY", document_count: 2, completed_document_count: 1 }],
            },
          },
          allowed_choices: [
            { key: "CONFIRM_ADJUDICATION", label: "Confirm difference review complete", consequence: "Freeze reviewer revisions." },
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
    await userEvent.click(screen.getByRole("button", { name: "Confirm difference review complete" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "b".repeat(64),
      choice_key: "CONFIRM_ADJUDICATION",
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
          status: "OPEN",
          title: "Review Definition acceptance",
          explanation: "Confirm the definition before Q&A setup.",
          preview_hash: "b".repeat(64),
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
    await userEvent.click(screen.getByRole("button", { name: "Continue to Q&A task setup" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ preview_hash: "b".repeat(64), choice_key: "CONTINUE" });
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
        stages: [{ key: "WHOLE_COLLECTION_SCORING", name: "Whole-collection scoring", phase_key: "COLLECTION_REVIEW", ordinal: 14, batching_policy: "WHOLE_MATTER", requires_human_gate: true }],
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

  it("shows the final alignment report at the no-code QC gate", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 1, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "NO_CODE_QC", name: "No-code QC", phase_key: "QC_COMPLETION", ordinal: 17, batching_policy: "PARTITIONED_SET", requires_human_gate: true }],
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
          id: "action-1", action_type: "STAGE_GATE", stage_key: "NO_CODE_QC", status: "OPEN",
          title: "Review No-code QC", explanation: "Review final alignment before QC.", preview_hash: "f".repeat(64),
          decision_context: {
            final_alignment: {
              population_document_count: 180,
              alignment_counts: { EXACT_ALL: 120, PARTIAL: 12, DISAGREEMENT: 8 },
              source_coverage_counts: { CURRENT: 170, STALE_OR_PARTIAL: 4, MISSING_QA: 5, SOURCE_UNAVAILABLE: 1 },
              issues: [{ issue_key: "responsive:true" }, { issue_key: "privilege:true" }],
            },
          },
          allowed_choices: [{ key: "CONTINUE", label: "Continue to Completion" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Aligned population")).toBeInTheDocument();
    expect(screen.getByText("All-source exact")).toBeInTheDocument();
    expect(screen.getByText("Disputed or partial")).toBeInTheDocument();
    expect(screen.getByText(/170 documents have current-source results/)).toBeInTheDocument();
  });
});
