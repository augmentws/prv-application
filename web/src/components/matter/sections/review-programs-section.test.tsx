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
const navigation = vi.hoisted(() => ({
  replace: vi.fn(),
  params: new URLSearchParams("program=program-1&action=action-1"),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/clients/client-1/matters/matter-1/review-programs",
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.params = new URLSearchParams("program=program-1&action=action-1");
});

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={queryClient}><ReviewProgramsSection /></QueryClientProvider>);
}

function mockReplayProgramApi() {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/review-workflow-templates") return [{
      key: "FIRST_PASS_REVIEW",
      version: 2,
      schema_version: 1,
      content_hash: "a".repeat(64),
      name: "First Pass Review",
      description: "Guided review",
      workflow_type: "FIRST_PASS_REVIEW",
      phases: [],
      stages: [
        { key: "MATTER_DEFINITION", name: "Matter definition", phase_key: "MATTER_DEFINITION", ordinal: 1, batching_policy: "NONE", requires_human_gate: true },
        { key: "DEFINITION_ASSESSMENT", name: "Definition assessment", phase_key: "MATTER_DEFINITION", ordinal: 3, batching_policy: "AD_HOC", requires_human_gate: true },
        { key: "QA_TASK_SETUP", name: "Q&A task setup", phase_key: "TRAINING_SET", ordinal: 5, batching_policy: "NONE", requires_human_gate: true },
        { key: "DISCOVERY_QA", name: "Discovery Q&A", phase_key: "TRAINING_SET", ordinal: 6, batching_policy: "PARTITIONED_SET", requires_human_gate: true },
        { key: "DISCOVERY_ANALYSIS", name: "Discovery analysis", phase_key: "TRAINING_SET", ordinal: 7, batching_policy: "NONE", requires_human_gate: false },
        { key: "ISSUE_SUPPORT_DECISION", name: "Issue support decision", phase_key: "TRAINING_SET", ordinal: 8, batching_policy: "NONE", requires_human_gate: true },
      ],
    }] as never;
    if (path === "/v1/matters/matter-1/review-programs") return [{
      id: "program-1",
      tenant_id: "tenant-1",
      matter_id: "matter-1",
      template_key: "FIRST_PASS_REVIEW",
      template_version: 2,
      template_content_hash: "a".repeat(64),
      key: "standard-first-pass",
      name: "Standard first pass",
      description: "Responsive review",
      status: "COMPLETED",
      current_run_id: "run-complete",
      current_version: { version: 6 },
      created_at: "2026-10-21T00:00:00Z",
    }] as never;
    if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
      id: "run-complete",
      program_id: "program-1",
      status: "COMPLETED",
      current_stage: "COMPLETION",
      current_stage_ordinal: 18,
      progress_summary: {
        accepted_qa_task: { task_id: "task-1", task_version_id: "task-version-2", version: 2 },
        qa_task_repin: { from_task_version_id: "task-version-1", to_task_version_id: "task-version-2" },
        discovery_qa: { batch_set_id: "set-1", task_id: "task-1", task_version_id: "task-version-2", target_document_count: 2000, selected_document_count: 1800, target_batch_size: 1000, keep_families_together: true },
      },
      created_at: "2026-10-21T00:00:00Z",
      completed_at: "2026-10-22T00:00:00Z",
      failure_retriable: false,
      failure_summary: null,
      stage_attempts: [
        { id: "attempt-definition", stage_key: "MATTER_DEFINITION", attempt: 1, status: "COMPLETED", input_snapshot: { mode: "CREATE" }, result_summary: { revision: 1 }, started_at: "2026-10-21T00:00:00Z", completed_at: "2026-10-21T01:00:00Z" },
        { id: "attempt-assessment", stage_key: "DEFINITION_ASSESSMENT", attempt: 2, status: "COMPLETED", input_snapshot: { assessment_id: "assessment-1", matter_definition_id: "definition-1", matter_definition_revision_id: "revision-1" }, result_summary: { question_count: 2 }, started_at: "2026-10-21T01:00:00Z", completed_at: "2026-10-21T01:30:00Z" },
        { id: "attempt-qa-setup", stage_key: "QA_TASK_SETUP", attempt: 1, status: "COMPLETED", input_snapshot: {}, result_summary: { accepted_qa_task: { task_id: "task-1", task_version_id: "task-version-1", version: 1 } }, started_at: "2026-10-21T01:30:00Z", completed_at: "2026-10-21T01:45:00Z" },
        { id: "attempt-discovery", stage_key: "DISCOVERY_QA", attempt: 1, status: "COMPLETED", input_snapshot: { target_document_count: 2000 }, result_summary: { discovery_qa: { batch_set_id: "set-1", task_id: "task-1", task_version_id: "task-version-2", target_document_count: 2000, selected_document_count: 1800, target_batch_size: 1000, keep_families_together: true } }, started_at: "2026-10-21T01:45:00Z", completed_at: "2026-10-21T04:00:00Z" },
        { id: "attempt-analysis", stage_key: "DISCOVERY_ANALYSIS", attempt: 1, status: "COMPLETED", input_snapshot: {}, result_summary: { profile_run_ids: ["profile-run-1", "profile-run-2"], execution_profiles: [{ execution_profile_id: "profile-1", resolved_model: "jev-insurance-2026" }, { execution_profile_id: "profile-2", resolved_model: "gpt-6-luna" }], source_batches: [{ id: "batch-1", name: "First Pass · discovery round 1 · 1 of 2", document_count: 900 }, { id: "batch-2", name: "First Pass · discovery round 1 · 2 of 2", document_count: 900 }], batch_count: 2, support_status: "SUFFICIENT", policy: { minimum_positive_support: 25, minimum_negative_support: 25 }, overall: { exact_count: 1700, partial_count: 50, disagreement_count: 45, incomplete_count: 5 }, fields: [], low_support: [] }, started_at: "2026-10-21T04:00:00Z", completed_at: "2026-10-21T04:03:00Z" },
        { id: "attempt-support-1", stage_key: "ISSUE_SUPPORT_DECISION", attempt: 1, status: "FAILED", input_snapshot: {}, result_summary: {}, error_summary: "Earlier analysis was incomplete.", error_code: "ANALYSIS_INCOMPLETE", started_at: "2026-10-21T02:00:00Z", completed_at: "2026-10-21T02:10:00Z" },
        { id: "attempt-support-2", stage_key: "ISSUE_SUPPORT_DECISION", attempt: 2, status: "COMPLETED", input_snapshot: { low_support_count: 2 }, result_summary: { decision: "IGNORE_LOW_SUPPORT" }, started_at: "2026-10-21T03:00:00Z", completed_at: "2026-10-21T03:10:00Z" },
      ],
      actions: [{
        id: "action-qa-generate",
        stage_attempt_id: "attempt-qa-setup",
        action_type: "QA_TASK_PUBLICATION",
        stage_key: "QA_TASK_SETUP",
        status: "RESOLVED",
        title: "Publish the generated Q&A task",
        explanation: "Review the generated task before publishing it.",
        preview_hash: "c".repeat(64),
        decision_context: {
          compiler: { progress: { question_count: 32, recommendation_count: 32, warning_count: 0, omission_count: 0 } },
        },
        allowed_choices: [
          { key: "PUBLISH", label: "Publish Q&A task", consequence: "Use the validated task for discovery review." },
          { key: "STOP", label: "Stop program", consequence: "Cancel this run while preserving its history and completed work." },
        ],
        resolution_payload: { choice_key: "PUBLISH" },
        resolution_reason: "Q&A task generation started",
        resolved_by_user_id: "user-1",
        resolved_at: "2026-10-21T01:45:00Z",
      }, {
        id: "action-support",
        stage_attempt_id: "attempt-support-2",
        action_type: "ISSUE_SUPPORT_DECISION",
        stage_key: "ISSUE_SUPPORT_DECISION",
        status: "RESOLVED",
        title: "Resolve low-support coding categories",
        explanation: "Choose how to proceed with categories that have limited examples.",
        preview_hash: "b".repeat(64),
        decision_context: {},
        allowed_choices: [
          { key: "SUPPLEMENT", label: "Review more documents", consequence: "Run targeted discovery." },
          { key: "IGNORE_LOW_SUPPORT", label: "Accept limitations", consequence: "Continue with the recorded limitations." },
        ],
        resolution_payload: { choice_key: "IGNORE_LOW_SUPPORT" },
        resolution_reason: "The senior reviewer accepted the limited category support.",
        resolved_by_user_id: "user-1",
        resolved_at: "2026-10-21T03:10:00Z",
      }],
    }] as never;
    if (path === "/v1/matters/matter-1/definition-assessments/assessment-1") return {
      id: "assessment-1",
      guidance_id: "definition-1",
      matter_definition_revision_id: "revision-1",
      refined_matter_definition_revision_id: "revision-2",
      status: "COMPLETED",
      summarized_count: 994,
      failed_count: 4,
      synthesis_result: { status: "SUFFICIENT" },
    } as never;
    if (path === "/v1/matters/matter-1/definition-assessments/assessment-1/questions") return [{
      id: "question-1",
      question: "Should Issue 4 — Property Insurance Reform include executive housing recommendations?",
      rationale: "The sampled documents use broader disaster-recovery language.",
      priority: "HIGH",
      blocking: true,
      status: "ANSWERED",
      answer: "Include them only when they recommend a concrete insurance policy change.",
      answered_at: "2026-10-21T01:20:00Z",
    }, {
      id: "question-2",
      question: "Should generic economic commentary be included?",
      rationale: "The existing definition does not state a boundary.",
      priority: "MEDIUM",
      blocking: false,
      status: "DISMISSED",
      answer: null,
      answered_at: "2026-10-21T01:21:00Z",
    }] as never;
    if (path === "/v1/matters/matter-1/analysis-tasks/task-1/versions") return [{
      id: "task-version-1", version: 1, status: "RETIRED", compilation_status: "READY",
      destination_metadata_definition_ids: ["field-insurance"], published_at: "2026-10-21T01:44:00Z",
    }, {
      id: "task-version-2", version: 2, status: "PUBLISHED", compilation_status: "READY",
      destination_metadata_definition_ids: ["field-issues", "field-responsive"], published_at: "2026-10-22T01:44:00Z",
      decision_specification: {
        decision_context: { summary: "Determine whether the document is responsive to the defined insurance issues." },
        questions: {
          "responsiveness.overall": {
            type: "noul",
            instructions: "Is the document responsive to the Matter Definition?",
            criteria: { true: "Responsive evidence is present.", false: "No responsive evidence is present." },
          },
        },
        decision_policy: { recommendations: { responsiveness: { operator: "MAPPED_VALUE" } } },
        state_contract: { required_paths: ["matter.decision_context", "document.paragraphs"] },
      },
    }] as never;
    if (path === "/v1/matters/matter-1/metadata-definitions") return [{
      id: "field-insurance", key: "insurance_topics", display_name: "Insurance topics",
    }, {
      id: "field-issues", key: "issue_tag", display_name: "Issue Tag",
    }, {
      id: "field-responsive", key: "responsiveness", display_name: "Responsiveness",
    }] as never;
    if (path === "/v1/matters/matter-1/analysis-tasks/task-1/batch-sets/set-1") return {
      id: "set-1", status: "READY", population_count: 1800, batch_count: 2, target_batch_size: 1000,
    } as never;
    if (path === "/v1/matters/matter-1/analysis-tasks/question-answering-jobs?batch_set_id=set-1") return [
      { id: "jev-1", scope_id: "batch-1", review_batch_id: "batch-1", review_batch_name: "Batch 1", task_name: "Discovery", decision_provider: "JEV", model_key: "jev-latest", status: "COMPLETED", successful_document_count: 900, document_count: 900, failed_document_count: 0, started_at: "2026-10-21T02:00:00Z", completed_at: "2026-10-21T02:30:00Z" },
      { id: "jev-2", scope_id: "batch-2", review_batch_id: "batch-2", review_batch_name: "Batch 2", task_name: "Discovery", decision_provider: "JEV", model_key: "jev-latest", status: "COMPLETED", successful_document_count: 895, document_count: 900, failed_document_count: 5, started_at: "2026-10-21T02:31:00Z", completed_at: "2026-10-21T03:30:00Z" },
      { id: "openai-1", scope_id: "batch-1", review_batch_id: "batch-1", review_batch_name: "Batch 1", task_name: "Discovery", decision_provider: "OPENAI_DECISIONS", model_key: "gpt-6-luna", status: "COMPLETED", successful_document_count: 900, document_count: 900, failed_document_count: 0, started_at: "2026-10-21T02:05:00Z", completed_at: "2026-10-21T02:45:00Z" },
      { id: "openai-2", scope_id: "batch-2", review_batch_id: "batch-2", review_batch_name: "Batch 2", task_name: "Discovery", decision_provider: "OPENAI_DECISIONS", model_key: "gpt-6-luna", status: "COMPLETED", successful_document_count: 900, document_count: 900, failed_document_count: 0, started_at: "2026-10-21T02:46:00Z", completed_at: "2026-10-21T03:45:00Z" },
    ] as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

describe("ReviewProgramsSection", () => {
  it("opens a completed run in read-only demo replay", async () => {
    mockReplayProgramApi();
    renderSection();

    await userEvent.click(await screen.findByRole("button", { name: "Present run" }));

    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("view=replay"),
      { scroll: false },
    );
    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("stage=MATTER_DEFINITION"),
      { scroll: false },
    );
  });

  it("prefers the most advanced completed run over a newer empty canceled run", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "MATTER_DEFINITION", name: "Matter definition", phase_key: "MATTER_DEFINITION", ordinal: 1, batching_policy: "NONE", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: null, current_version: { version: 6 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-canceled", program_id: "program-1", status: "CANCELED", current_stage: "MATTER_DEFINITION",
        current_stage_ordinal: 1, progress_summary: {}, created_at: "2026-10-28T00:00:00Z", completed_at: "2026-10-28T00:00:06Z",
        failure_retriable: false, failure_summary: null, actions: [],
        stage_attempts: [{ id: "attempt-canceled", stage_key: "MATTER_DEFINITION", attempt: 1, status: "CANCELED" }],
      }, {
        id: "run-completed", program_id: "program-1", status: "COMPLETED", current_stage: "COMPLETION",
        current_stage_ordinal: 18, progress_summary: {}, created_at: "2026-10-23T00:00:00Z", completed_at: "2026-11-04T00:00:00Z",
        failure_retriable: false, failure_summary: null, actions: [],
        stage_attempts: [{ id: "attempt-completed", stage_key: "MATTER_DEFINITION", attempt: 1, status: "COMPLETED" }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    await userEvent.click(await screen.findByRole("button", { name: "Present run" }));

    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("run=run-completed"),
      { scroll: false },
    );
    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("attempt=attempt-completed"),
      { scroll: false },
    );
  });

  it("replays a recorded stage and decision without exposing mutation controls", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=ISSUE_SUPPORT_DECISION&attempt=attempt-support-2");
    mockReplayProgramApi();
    renderSection();

    expect(await screen.findByText("Demo replay")).toBeInTheDocument();
    expect(screen.getByText("Read only")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Issue support decision" })).toBeInTheDocument();
    expect(screen.getByText("Attempt 2")).toBeInTheDocument();
    const outcomeCard = screen.getByRole("heading", { name: "Recorded outcome" }).closest(".rounded-xl");
    expect(outcomeCard).not.toBeNull();
    expect(within(outcomeCard as HTMLElement).getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("Accept limitations")).toBeInTheDocument();
    expect(screen.getByText("The senior reviewer accepted the limited category support.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept limitations" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Previous stage" }));
    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("stage=DISCOVERY_ANALYSIS"),
      { scroll: false },
    );
    await userEvent.click(screen.getByRole("button", { name: "Exit replay" }));
    const exitCall = navigation.replace.mock.calls.at(-1);
    expect(String(exitCall?.[0])).not.toContain("view=replay");
  });

  it("links whole-matter scoring replay to the classifier scoring outcome", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=WHOLE_COLLECTION_SCORING&attempt=attempt-scoring");
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "WHOLE_COLLECTION_SCORING", name: "Whole-collection scoring", phase_key: "COLLECTION_REVIEW", ordinal: 14, batching_policy: "WHOLE_MATTER", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "COMPLETED", current_run_id: null, current_version: { version: 6 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-complete", program_id: "program-1", status: "COMPLETED", current_stage: "COMPLETION",
        current_stage_ordinal: 18, progress_summary: {}, created_at: "2026-10-21T00:00:00Z", completed_at: "2026-10-22T00:00:00Z",
        failure_retriable: false, failure_summary: null, actions: [],
        stage_attempts: [{
          id: "attempt-scoring", stage_key: "WHOLE_COLLECTION_SCORING", attempt: 2, status: "COMPLETED",
          input_snapshot: {}, started_at: "2026-10-21T04:00:00Z", completed_at: "2026-10-21T06:00:00Z",
          result_summary: {
            state: "COMPLETED", collection_document_count: 639050,
            aggregate_model_document_counts: { population_document_count: 639050, scored_document_count: 634698, abstained_document_count: 0, excluded_document_count: 4352, failed_document_count: 0 },
            models: [{ classification_model_id: "model-1", scoring_run_id: "scoring-1", field_name: "Issue Tag", status: "COMPLETED", scored_document_count: 634698, abstained_document_count: 0, excluded_document_count: 4352, failed_document_count: 0 }],
          },
        }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Whole-matter scoring outcome")).toBeInTheDocument();
    expect(screen.getAllByText("639,050").length).toBeGreaterThan(0);
    expect(screen.getAllByText("634,698").length).toBeGreaterThan(0);
    expect(screen.getByText("4,352")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View scoring results" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/classification-models/model-1/scoring-runs/scoring-1",
    );
    expect(screen.getByRole("link", { name: "View scoring results" })).toHaveAttribute("target", "_blank");
  });

  it("shows the classifier and named decision-model comparison from Step 15", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=CLASSIFIER_POSITIVE_COVERAGE&attempt=attempt-coverage");
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "COMPLETED", current_run_id: null, current_version: { version: 6 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-complete", program_id: "program-1", status: "COMPLETED", current_stage: "COMPLETION",
        current_stage_ordinal: 18, created_at: "2026-10-21T00:00:00Z", completed_at: "2026-10-22T00:00:00Z",
        failure_retriable: false, failure_summary: null, actions: [],
        progress_summary: {
          accepted_qa_task: { execution_profiles: [{ resolved_model: "jev-latest" }, { resolved_model: "gpt-6-luna" }] },
          classifier_positive_coverage: { minimum_confidence: 0.85, positive_document_count: 15791 },
          final_alignment: {
            state: "COMPLETED", population_scope: "CLASSIFIER_POSITIVE_AT_OR_ABOVE_COVERAGE_THRESHOLD",
            minimum_confidence: 0.85, population_document_count: 15791,
            alignment_counts: { EXACT_ALL: 1924, QA_PAIR_EXACT: 11180, PARTIAL: 2613, DISAGREEMENT: 71, INCOMPLETE: 3 },
            source_coverage_counts: { CURRENT: 15788, PARTIAL_OR_MISSING_PROFILE: 3 },
            qa_coverage_policy: { minimum_confidence: 0.85, selected_document_count: 15791 },
            issues: [{ issue_key: "issue_tag:issue_1_market_disruption", classifier: 7318, profile_a: 3632, profile_b: 3799, qa_pair: 3500, all_three: 3465 }],
          },
        },
        stage_attempts: [{
          id: "attempt-coverage", stage_key: "CLASSIFIER_POSITIVE_COVERAGE", attempt: 1, status: "COMPLETED",
          input_snapshot: {}, result_summary: {}, started_at: "2026-10-21T06:00:00Z", completed_at: "2026-10-21T09:00:00Z",
        }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Classifier and decision-model comparison")).toBeInTheDocument();
    expect(screen.getByText(/jev-latest and gpt-6-luna/)).toBeInTheDocument();
    expect(screen.getAllByText("15,791").length).toBeGreaterThan(0);
    expect(screen.getByText("11,180")).toBeInTheDocument();
    expect(screen.getByText("Issue 1 Market Disruption")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "jev-latest" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "gpt-6-luna" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View full comparison" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/review-programs/program-1/runs/run-complete/final-alignment",
    );
    expect(screen.getByRole("link", { name: "View full comparison" })).toHaveAttribute("target", "_blank");
  });

  it("returns a retriable failed attempt from replay to the live run", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-failed&view=replay&stage=CLASSIFIER_POSITIVE_COVERAGE&attempt=attempt-failed");
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-failed", current_version: { version: 12 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-failed", program_id: "program-1", status: "FAILED", current_stage: "CLASSIFIER_POSITIVE_COVERAGE",
        current_stage_ordinal: 15, created_at: "2026-10-21T00:00:00Z", failure_retriable: true,
        failure_summary: "One or more classifier-positive coverage Q&A profile runs failed", actions: [],
        stage_attempts: [{
          id: "attempt-failed", stage_key: "CLASSIFIER_POSITIVE_COVERAGE", attempt: 6, status: "FAILED",
          input_snapshot: {}, result_summary: {}, error_summary: "One or more classifier-positive coverage Q&A profile runs failed",
          error_code: "CLASSIFIER_POSITIVE_COVERAGE_QA_PROFILE_FAILED", started_at: "2026-10-21T01:00:00Z", completed_at: "2026-10-21T01:10:00Z",
        }],
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Attempt did not complete")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Exit replay to retry" }));

    const exitCall = navigation.replace.mock.calls.at(-1);
    expect(String(exitCall?.[0])).not.toContain("view=replay");
  });

  it("shows the questions, recorded results, and exact definition change link for an assessment run", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=DEFINITION_ASSESSMENT&attempt=attempt-assessment");
    mockReplayProgramApi();
    renderSection();

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-1",
    ));
    expect(await screen.findByText(/Should Issue 4 — Property Insurance Reform include executive housing recommendations\?/)).toBeInTheDocument();
    expect(screen.getByText("Include them only when they recommend a concrete insurance policy change.")).toBeInTheDocument();
    expect(screen.getByText("Question dismissed without an answer.")).toBeInTheDocument();
    expect(screen.getByText("994 summarized · 4 failed")).toBeInTheDocument();
    expect(screen.getByText("Revision created")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open assessment" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/definition?guidance=definition-1&assessment=assessment-1",
    );
    expect(screen.getByRole("link", { name: "View definition changes" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/definition?guidance=definition-1&assessment=assessment-1&compare_from=revision-1&compare_to=revision-2",
    );
    expect(screen.getByRole("link", { name: "View definition changes" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "View definition changes" })).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("shows the final Q&A task by default and selects earlier retained versions from a dropdown", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=QA_TASK_SETUP&attempt=attempt-qa-setup");
    mockReplayProgramApi();
    renderSection();

    const history = await screen.findByRole("region", { name: "Q&A task configuration history" });
    expect(screen.getByText("2 versions · final version 2")).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Coordinator run" })).not.toBeInTheDocument();
    expect(within(history).getByText("The task changed after the original Stage 5 publication.")).toBeInTheDocument();
    expect(within(history).getByText("Final task used by program")).toBeInTheDocument();
    expect(within(history).queryByText("Earlier task setup")).not.toBeInTheDocument();
    expect(within(history).queryByText("Task version 1")).not.toBeInTheDocument();
    expect(within(history).queryByText("Insurance topics")).not.toBeInTheDocument();
    expect(within(history).getByText("Task version 2")).toBeInTheDocument();
    expect(within(history).getByText("Final version used")).toBeInTheDocument();
    expect(within(history).getByText("Issue Tag")).toBeInTheDocument();
    expect(within(history).getByText("Responsiveness")).toBeInTheDocument();
    expect(within(history).getByText("1 question")).toBeInTheDocument();
    expect(within(history).getByText("1 recommendation")).toBeInTheDocument();
    expect(screen.queryByText("32 questions · 32 recommendations")).not.toBeInTheDocument();
    expect(within(history).getByLabelText("Decision prompt preview")).toBeInTheDocument();
    expect(within(history).getByText("Determine whether the document is responsive to the defined insurance issues.")).toBeInTheDocument();
    expect(within(history).getByText("Is the document responsive to the Matter Definition?")).toBeInTheDocument();
    expect(within(history).queryByText("Required Paths")).not.toBeInTheDocument();
    await userEvent.click(within(history).getByRole("tab", { name: "JSON view" }));
    expect(within(history).getByLabelText("Decision Specification JSON")).toHaveTextContent('"state_contract"');

    await userEvent.click(within(history).getByRole("combobox", { name: "Task version" }));
    await userEvent.click(screen.getByRole("option", { name: "Version 1 · Original Stage 5 setup" }));
    expect(within(history).getByText("Earlier task setup")).toBeInTheDocument();
    expect(within(history).getByText("Task version 1")).toBeInTheDocument();
    expect(within(history).getByText("Original Stage 5 setup")).toBeInTheDocument();
    expect(within(history).getByText("Insurance topics")).toBeInTheDocument();
    expect(within(history).queryByText("Final task used by program")).not.toBeInTheDocument();
    expect(within(history).queryByText("Task version 2")).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("Recorded stage data"));
    const finalResults = screen.getByText("Final results and setup history").parentElement?.querySelector("pre");
    expect(finalResults).toHaveTextContent('"accepted_qa_task"');
    expect(finalResults).toHaveTextContent('"task_version_id": "task-version-2"');
    expect(finalResults).toHaveTextContent('"original_stage_accepted_qa_task"');
    expect(finalResults).toHaveTextContent('"task_version_id": "task-version-1"');
    expect(screen.getByRole("heading", { name: "Recorded outcome" })).toBeInTheDocument();
    expect(screen.getByText("Publish Q&A task")).toBeInTheDocument();
    expect(screen.queryByText("Stop program")).not.toBeInTheDocument();
    expect(screen.queryByText("Q&A task generation started")).not.toBeInTheDocument();
  });

  it("summarizes the complete Discovery Q&A execution instead of requiring the stage JSON", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=DISCOVERY_QA&attempt=attempt-discovery");
    mockReplayProgramApi();
    renderSection();

    const summary = await screen.findByRole("region", { name: "Discovery Q&A recorded results" });
    expect(within(summary).getByText("Discovery Q&A execution")).toBeInTheDocument();
    expect(screen.getByText("2h 15m")).toBeInTheDocument();
    expect(await within(summary).findByText("1,800 selected")).toBeInTheDocument();
    expect(within(summary).getByText("Target 2,000 · 200 short")).toBeInTheDocument();
    expect(within(summary).getByText("2 batches")).toBeInTheDocument();
    expect(within(summary).getByText("Up to 1,000 documents each")).toBeInTheDocument();
    expect(within(summary).getByText("Kept together")).toBeInTheDocument();
    expect(within(summary).getByText("Version 2")).toBeInTheDocument();
    expect(within(summary).getByText("Jev")).toBeInTheDocument();
    expect(within(summary).getByText("1,795")).toBeInTheDocument();
    expect(within(summary).getByText("5")).toBeInTheDocument();
    expect(within(summary).getByText("OpenAI Decisions")).toBeInTheDocument();
    expect(within(summary).getByText("1h 30m")).toBeInTheDocument();
    expect(within(summary).getByText("1h 40m")).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "View linked jobs" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/jobs/question-answering?batch_set=set-1",
    );
    expect(screen.getByText("Technical stage record (JSON)")).toBeInTheDocument();
  });

  it("links Discovery Analysis to each retained source batch comparison", async () => {
    navigation.params = new URLSearchParams("program=program-1&run=run-complete&view=replay&stage=DISCOVERY_ANALYSIS&attempt=attempt-analysis");
    mockReplayProgramApi();
    renderSection();

    const sourceData = await screen.findByText("Source analysis data");
    const analysis = sourceData.closest(".rounded-lg");
    expect(analysis).not.toBeNull();
    expect(within(analysis as HTMLElement).getByText("1,700")).toBeInTheDocument();
    expect(within(analysis as HTMLElement).getByRole("link", { name: /First Pass · discovery round 1 · 1 of 2/ })).toHaveAttribute(
      "href",
      "/review/matters/matter-1/batches/batch-1/statistics",
    );
    expect(within(analysis as HTMLElement).getByRole("link", { name: /First Pass · discovery round 1 · 2 of 2/ })).toHaveAttribute(
      "href",
      "/review/matters/matter-1/batches/batch-2/statistics",
    );
    for (const link of within(analysis as HTMLElement).getAllByRole("link")) {
      expect(link).toHaveAttribute("target", "_blank");
    }
  });

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
              execution_profiles: [
                { execution_profile_id: "profile-1", resolved_model: "jev-insurance-2026" },
                { execution_profile_id: "profile-2", resolved_model: "gpt-6-luna" },
              ],
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
    expect(screen.getAllByRole("columnheader", { name: "jev-insurance-2026" })).not.toHaveLength(0);
    expect(screen.getAllByRole("columnheader", { name: "gpt-6-luna" })).not.toHaveLength(0);
    expect(screen.queryByText("Model A")).not.toBeInTheDocument();
    expect(screen.queryByText("Model B")).not.toBeInTheDocument();
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
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
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
            coverage_plan: { required_profile_count: 2, maximum_candidate_model_document_count: 230, default_minimum_confidence: 0.5, batching_policy: "AD_HOC_BATCH" },
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
    expect(screen.getByText("Ad hoc batch")).toBeInTheDocument();
    expect(screen.getByText(/Existing documents reviewed by both pinned profiles are excluded/)).toBeInTheDocument();
    const minimumConfidence = screen.getByLabelText("Minimum classifier confidence %");
    expect(minimumConfidence).toHaveValue(50);
    await userEvent.clear(minimumConfidence);
    await userEvent.type(minimumConfidence, "70");
    await userEvent.click(screen.getByRole("button", { name: "Run dual-Q&A coverage" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/actions/action-1/resolve"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "e".repeat(64),
      choice_key: "RUN_COVERAGE",
      input: { minimum_confidence: 0.7 },
    });
  });

  it("offers retry or acceptance when classifier-positive coverage completes within tolerance", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 12 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "CLASSIFIER_POSITIVE_COVERAGE",
        current_stage_ordinal: 15, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "CLASSIFIER_POSITIVE_COVERAGE", status: "WAITING" }],
        progress_summary: {
          classifier_positive_coverage_state: "COMPLETED_WITH_ERRORS",
          classifier_positive_coverage: { state: "COMPLETED_WITH_ERRORS", uncovered_document_count: 3, coverage_population_document_count: 15214 },
        },
        actions: [{
          id: "action-errors", action_type: "CLASSIFIER_POSITIVE_COVERAGE_ERRORS", stage_key: "CLASSIFIER_POSITIVE_COVERAGE", status: "OPEN",
          title: "Resolve incomplete classifier-positive Q&A coverage", explanation: "3 of 15,214 documents remain unresolved.", preview_hash: "f".repeat(64),
          decision_context: {
            classifier_positive_coverage_errors: {
              coverage_population_document_count: 15214, unresolved_document_count: 3,
              unresolved_document_rate: 3 / 15214, failure_tolerance: 0.005,
              profile_runs: [{ id: "jev-profile", model_name: "jev-latest", status: "COMPLETED_WITH_ERRORS", completed_document_count: 15211, reused_document_count: 0, failed_document_count: 3, processed_document_count: 15214, run_count: 4 }],
            },
          },
          allowed_choices: [
            { key: "RETRY_FAILED_DOCUMENTS", label: "Retry failed documents", consequence: "Retry only unresolved documents." },
            { key: "ACCEPT_UNRESOLVED_DOCUMENTS", label: "Accept unresolved documents", consequence: "Record a limitation and continue." },
            { key: "STOP", label: "Stop program", consequence: "Stop." },
          ],
        }],
      }] as never;
      if (path.endsWith("/actions/action-errors/resolve") && options?.method === "POST") return {} as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Original population")).toBeInTheDocument();
    expect(screen.getByText("15,214")).toBeInTheDocument();
    expect(screen.getByText("0.02%")).toBeInTheDocument();
    expect(screen.getByText("15,211 completed · 0 reused · 3 failed across 4 runs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry failed documents" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Accept unresolved documents" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-errors/resolve",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("reports live classifier-positive coverage progress for both Q&A profiles", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 2, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "CLASSIFIER_POSITIVE_COVERAGE", name: "Classifier-positive Q&A coverage", phase_key: "COLLECTION_REVIEW", ordinal: 15, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 2, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 12 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs" && !options) return [{
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "CLASSIFIER_POSITIVE_COVERAGE",
        current_stage_ordinal: 15, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        stage_attempts: [{ stage_key: "CLASSIFIER_POSITIVE_COVERAGE", status: "RUNNING" }], actions: [],
        progress_summary: {
          classifier_positive_coverage_state: "RUNNING_PROFILES",
          classifier_positive_coverage: {
            state: "RUNNING_PROFILES", review_batch_id: "coverage-batch-1", uncovered_document_count: 100,
            already_covered_document_count: 25, required_profiles: [{ id: "profile-1" }, { id: "profile-2" }],
          },
        },
      }] as never;
      if (path === "/v1/matters/matter-1/analysis-tasks/question-answering-jobs?review_batch_id=coverage-batch-1") return [{
        id: "qa-run-1", review_batch_id: "coverage-batch-1", review_batch_name: "Coverage", task_name: "Issue coding",
        decision_provider: "JEV", model_key: "jev", status: "RUNNING", processed_document_count: 60,
        successful_document_count: 40, document_count: 100, failed_document_count: 0,
      }, {
        id: "qa-run-2", review_batch_id: "coverage-batch-1", review_batch_name: "Coverage", task_name: "Issue coding",
        decision_provider: "OPENAI_DECISIONS", model_key: "gpt", status: "COMPLETED", processed_document_count: 100,
        successful_document_count: 80, document_count: 100, failed_document_count: 0,
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Reviewing classifier-positive documents")).toBeInTheDocument();
    expect(screen.getByText("160 of 200 resolved")).toBeInTheDocument();
    expect(screen.getByText(/120 newly successful · 40 existing results reused/)).toBeInTheDocument();
    expect(screen.getByText("jev")).toBeInTheDocument();
    expect(screen.getByText("gpt")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View coverage jobs" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/jobs/question-answering?review_batch=coverage-batch-1",
    );
    expect(screen.getByText("Classifier-positive Q&A coverage").closest("li")).toHaveClass("sm:col-span-2");
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
        progress_summary: { classifier_positive_coverage: { minimum_confidence: 0.85, positive_document_count: 175 } },
        stage_attempts: [{ stage_key: "NO_CODE_QC", status: "WAITING" }],
        actions: [{
          id: "action-1", action_type: "NEGATIVE_POPULATION_QC_SETUP", stage_key: "NO_CODE_QC", status: "OPEN",
          title: "Configure Negative-population QC", explanation: "Configure blind QC sampling.", preview_hash: "f".repeat(64),
          decision_context: {
            defaults: { random_sample_size: 200, near_threshold_sample_size: 100, cohort_control_sample_size: 50, mandatory_review_limit: 100, target_batch_size: 100, keep_families_together: true },
            negative_population_qc_inventory: {
              state: "READY", input_manifest_content_hash: "a".repeat(64), scoring_population_document_count: 64000, excluded_positive_document_count: 15791, near_threshold_band: 0.1,
              procedures: {
                RANDOM_AUDIT: { available_count: 48000, execution_route: "DUAL_QA" },
                NEAR_THRESHOLD: { available_count: 1200, execution_route: "HUMAN_REVIEW" },
                RECORD_TYPE_CONTROL: { available_count: 48000, execution_route: "HUMAN_REVIEW", record_type_counts: { EMAIL: 30000, ATTACHMENT: 18000 } },
                PROCESSING_EXCEPTIONS: { available_count: 42, execution_route: "RETRY_OR_HUMAN_REVIEW", source_remediation_count: 3 },
              },
            },
            final_alignment: {
              schema_version: 1,
              manifest_id: "manifest-1",
              population_document_count: 180,
              alignment_counts: { EXACT_ALL: 120, QA_PAIR_EXACT: 20, PARTIAL: 12, DISAGREEMENT: 8, INCOMPLETE: 20 },
              source_coverage_counts: { CURRENT: 170, STALE_OR_PARTIAL: 4, MISSING_QA: 5, SOURCE_UNAVAILABLE: 1 },
              qa_coverage_policy: { minimum_confidence: 0.85, selected_document_count: 175 },
              issues: [{ issue_key: "responsive:true" }, { issue_key: "privilege:true" }],
            },
          },
          allowed_choices: [{ key: "START_QC", label: "Start Negative-population QC" }, { key: "STOP", label: "Stop program" }],
        }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs/run-1/qa-population-statistics") return {
        run_id: "run-1", task_version_id: "task-version-1", document_count: 5200,
        both_profiles_document_count: 5000, one_profile_document_count: 200,
        fields: [{ id: "field-1", key: "insurance_topics", display_name: "Insurance topics", type: "ENUM", cardinality: "MULTIPLE", allowed_values: [{ key: "reform", label: "Reform" }] }],
        selected_metadata_definition_id: "field-1",
        sources: [
          { id: "profile-1", source_type: "RUN", run_type: "WORKFLOW", purpose: null, status: "COMPLETED", preparation_status: "READY", label: "jev-latest", detail: "JEV", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
          { id: "profile-2", source_type: "RUN", run_type: "WORKFLOW", purpose: null, status: "COMPLETED", preparation_status: "READY", label: "gpt-6-luna", detail: "OpenAI Decisions", metadata_definition_ids: ["field-1"], created_at: "2026-10-21T00:00:00Z" },
        ],
        source_summaries: [
          { source_id: "profile-1", evaluated_document_count: 5100, no_value_document_count: 2000, skipped_document_count: 0, failed_document_count: 0, incomplete_document_count: 0, not_evaluated_document_count: 100, value_counts: [{ value_key: "reform", display_label: "Reform", document_count: 3100 }], value_counts_truncated: false },
          { source_id: "profile-2", evaluated_document_count: 5100, no_value_document_count: 2100, skipped_document_count: 0, failed_document_count: 0, incomplete_document_count: 0, not_evaluated_document_count: 100, value_counts: [{ value_key: "reform", display_label: "Reform", document_count: 3000 }], value_counts_truncated: false },
        ],
        pairwise_agreements: [{ left_source_id: "profile-1", right_source_id: "profile-2", compared_document_count: 5000, exact_match_count: 4500, partial_match_count: 300, disagreement_count: 200, incomplete_document_count: 200, exact_agreement: 0.9, mean_jaccard: 0.94, confusion_matrix: [], class_agreement: [{ value_key: "reform", display_label: "Reform", both_selected_count: 2900, left_only_count: 200, right_only_count: 100, both_not_selected_count: 1800, jaccard: 0.90625 }] }],
        multi_source_agreement: { compared_document_count: 5000, incomplete_document_count: 200, class_agreement: [{ value_key: "reform", display_label: "Reform", source_counts: [{ source_id: "profile-1", document_count: 3100 }, { source_id: "profile-2", document_count: 3000 }], all_selected_count: 2900, any_selected_count: 3200, jaccard: 0.90625 }] },
      } as never;
      if (path.endsWith("/actions/action-1/negative-population-qc-setup") && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "NO_CODE_QC",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Classifier-positive documents at or above 85%")).toBeInTheDocument();
    expect(screen.getByText("175")).toBeInTheDocument();
    expect(screen.queryByText(/retired broad-population|Recalculate final alignment/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Classifier and both Q&A models exactly agree")).not.toBeInTheDocument();
    expect(screen.getByText("High-risk no-code review")).toBeInTheDocument();
    expect(screen.getByText("Record-type control review")).toBeInTheDocument();
    expect(screen.getByText("Processing-exception review")).toBeInTheDocument();
    expect(screen.getByText("1,200 available")).toBeInTheDocument();
    expect(screen.getByText("42 available")).toBeInTheDocument();
    expect(screen.queryByLabelText("Documents per batch")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View Q&A population statistics" })).toHaveAttribute(
      "href",
      "/app/clients/client-1/matters/matter-1/review-programs/program-1/runs/run-1/qa-statistics",
    );
    expect(screen.getByText("Negative-population QC").closest("li")).toHaveTextContent("Ad Hoc Batch");
    expect(screen.getByText(/64,000 documents in the frozen classifier population/)).toHaveTextContent(
      "15,791 excluded from negative QC because classifier, Q&A, or human coding is positive",
    );
    expect(screen.getByRole("link", { name: "View Q&A population statistics" })).toHaveAttribute("target", "_blank");
    const randomAudit = screen.getAllByLabelText("Documents to select")[0];
    await userEvent.clear(randomAudit);
    await userEvent.type(randomAudit, "250");
    await userEvent.click(screen.getByRole("button", { name: "Start selected QC procedures" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/actions/action-1/negative-population-qc-setup",
      expect.objectContaining({ method: "POST" }),
    ));
    const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/negative-population-qc-setup"));
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      preview_hash: "f".repeat(64),
      inventory_content_hash: "a".repeat(64),
      selected_procedures: ["RANDOM_AUDIT"],
      selected_record_types: [],
      random_sample_size: 250,
      near_threshold_sample_size: 0,
      cohort_control_sample_size: 0,
      mandatory_review_limit: 0,
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
          decision_context: { negative_population_qc: { review_batch_id: "qc-batch-1", report: {
            selected_document_count: 350, positive_document_count: 14, unresolved_document_count: 2,
            mandatory_unreviewable_count: 3, random_audit_estimate: { observed_positive_rate: 0.04, positive_count: 4, resolved_count: 100 },
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
    expect(screen.getByText("4 of 100 resolved random documents")).toBeInTheDocument();
    expect(screen.getByText("New positives · all lanes")).toBeInTheDocument();
    expect(screen.getByText("Unresolved documents")).toBeInTheDocument();
    expect(screen.getByText("Manual resolution")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View batch statistics" })).toHaveAttribute(
      "href",
      "/review/matters/matter-1/batches/qc-batch-1/statistics",
    );
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

  it("shows negative-population QC failure details when the attempt and parent run disagree", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, options) => {
      if (path === "/v1/review-workflow-templates") return [{
        key: "FIRST_PASS_REVIEW", version: 3, schema_version: 1, content_hash: "a".repeat(64),
        name: "First Pass Review", description: "Guided review", workflow_type: "FIRST_PASS_REVIEW", phases: [],
        stages: [{ key: "NO_CODE_QC", name: "Negative-population QC", phase_key: "QC_COMPLETION", ordinal: 17, batching_policy: "AD_HOC_BATCH", requires_human_gate: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs") return [{
        id: "program-1", tenant_id: "tenant-1", matter_id: "matter-1", template_key: "FIRST_PASS_REVIEW",
        template_version: 3, template_content_hash: "a".repeat(64), key: "standard-first-pass", name: "Standard first pass",
        description: "Responsive review", status: "ACTIVE", current_run_id: "run-1", current_version: { version: 13 }, created_at: "2026-10-21T00:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs") return [{
        id: "run-1", program_id: "program-1", status: "RUNNING", current_stage: "NO_CODE_QC",
        current_stage_ordinal: 17, created_at: "2026-10-21T00:00:00Z", failure_retriable: false, failure_summary: null,
        progress_summary: { negative_population_qc_state: "FAILED", negative_population_qc: {
          state: "FAILED", selected_document_count: 300, review_batch_id: "qc-batch-1",
          profile_runs: [
            { id: "jev-run", model_name: "jev-latest", status: "COMPLETED_WITH_ERRORS", completed_document_count: 299, reused_document_count: 0, failed_document_count: 1, failure_counts: { TYPESAFE_DOCUMENT_TOKEN_LIMIT_EXCEEDED: 1 } },
            { id: "openai-run", model_name: "gpt-6-luna", status: "COMPLETED", completed_document_count: 300, reused_document_count: 0, failed_document_count: 0, failure_counts: {} },
          ],
        } },
        stage_attempts: [{ stage_key: "NO_CODE_QC", status: "FAILED", error_summary: "Negative-population QC Q&A was incomplete.", error_code: "NEGATIVE_POPULATION_QC_QA_PROFILE_FAILED" }],
        actions: [],
      }] as never;
      if (path === "/v1/matters/matter-1/review-programs/program-1/runs/run-1/reconcile" && options?.method === "POST") return {
        id: "run-1", program_id: "program-1", status: "WAITING_FOR_USER", current_stage: "NO_CODE_QC",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    renderSection();

    expect(await screen.findByText("Negative-population QC Q&A was incomplete.")).toBeInTheDocument();
    expect(screen.getByText("300 documents")).toBeInTheDocument();
    expect(screen.getByText("1 · 0.33%")).toBeInTheDocument();
    expect(screen.getByText("jev-latest")).toBeInTheDocument();
    expect(screen.getByText("299 completed · 0 reused · 1 failed")).toBeInTheDocument();
    expect(screen.getByText(/does not rerun either model/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View batch statistics" })).toHaveAttribute(
      "href",
      "/review/matters/matter-1/batches/qc-batch-1/statistics",
    );
    await userEvent.click(screen.getAllByRole("button", { name: "Refresh state" }).at(-1)!);
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-programs/program-1/runs/run-1/reconcile",
      expect.objectContaining({ method: "POST" }),
    ));
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
