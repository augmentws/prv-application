import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { assessmentCanRetry, assessmentStageIndex, MatterDefinitionAssessmentPanel } from "@/components/matter-definition-assessment-panel";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("assessmentStageIndex", () => {
  it("does not present failed or canceled assessments as planning", () => {
    expect(assessmentStageIndex("FAILED")).toBe(-1);
    expect(assessmentStageIndex("CANCELED")).toBe(-1);
  });

  it("maps active and successful statuses to their progress stage", () => {
    expect(assessmentStageIndex("PLANNING")).toBe(0);
    expect(assessmentStageIndex("SUMMARIZING")).toBe(3);
    expect(assessmentStageIndex("COMPLETED_WITH_ERRORS")).toBe(5);
  });
});

describe("assessmentCanRetry", () => {
  it("allows failed assessments and partial assessments with failed documents", () => {
    expect(assessmentCanRetry("FAILED", 0)).toBe(true);
    expect(assessmentCanRetry("COMPLETED_WITH_ERRORS", 350)).toBe(true);
  });

  it("does not retry successful or skipped-only assessments", () => {
    expect(assessmentCanRetry("COMPLETED", 0)).toBe(false);
    expect(assessmentCanRetry("COMPLETED_WITH_ERRORS", 0)).toBe(false);
  });
});

describe("MatterDefinitionAssessmentPanel cancellation", () => {
  it("confirms and cancels a running assessment", async () => {
    const assessment = {
      id: "assessment-running",
      name: "Running evidence assessment",
      status: "SUMMARIZING",
      execution_mode: "EVIDENCE_FIRST_V1",
      synthesis_status: "NOT_STARTED",
      completion_disposition: null,
      current_synthesis_attempt_id: null,
      technical_diagnostics_visible: true,
      synthesis_diagnostics: {},
      guidance_refinement_status: "NOT_READY",
      selected_count: 250,
      summarized_count: 0,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: null,
      synthesis_result: null,
      configuration_snapshot: {},
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      if (path.endsWith("/evidence-outcomes")) return {
        readiness_counts: { READY: 127, PENDING: 123 },
        stage_status_counts: {},
        source_workflow_count: 127,
        source_artifact_count: 0,
        technical_diagnostics_visible: true,
        usage: { request_count: 0 },
        documents: [],
      } as never;
      if (path.endsWith("/cancel") && init?.method === "POST") return {
        ...assessment,
        status: "CANCELED",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("127 / 250")).toBeInTheDocument();
    expect(screen.getByText("Evidence created")).toBeInTheDocument();
    expect(screen.queryByText("Documents summarized")).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("heading", { name: "Cancel assessment?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel assessment" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-running/cancel",
      { method: "POST" },
    ));
  });
});

describe("MatterDefinitionAssessmentPanel regeneration", () => {
  it("shows only actionable evidence outcomes and keeps workflow diagnostics collapsed", async () => {
    const assessment = {
      id: "assessment-evidence",
      name: "Evidence assessment",
      status: "COMPLETED",
      synthesis_status: "COMPLETED",
      completion_disposition: "READY_WITH_WARNINGS",
      current_synthesis_attempt_id: null,
      technical_diagnostics_visible: true,
      synthesis_diagnostics: {},
      guidance_refinement_status: "NOT_REQUIRED",
      selected_count: 2,
      summarized_count: 2,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: { status: "COMPLETED" },
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=false")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      if (path.endsWith("/evidence-outcomes")) return {
        assessment_id: assessment.id,
        document_count: 2,
        readiness_counts: { READY: 1, READY_WITH_FALLBACK: 1 },
        stage_status_counts: {
          extraction: { COMPLETED: 2 },
          adjudication: { COMPLETED: 2 },
          document_decision: { COMPLETED: 2 },
          compatibility_projection: { COMPLETED: 2 },
          narrative: { COMPLETED: 1, COMPLETED_WITH_ERRORS: 1 },
        },
        source_workflow_count: 5,
        source_artifact_count: 8,
        technical_diagnostics_visible: true,
        usage: { request_count: 6 },
        documents: [
          {
            matter_document_id: "11111111-1111-4111-8111-111111111111",
            extraction: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            adjudication: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            document_decision: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            compatibility_projection: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            narrative: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            synthesis_readiness: "READY",
            selected_output_kind: "NARRATIVE_PRESENTATION",
            retryable_stages: [],
          },
          {
            matter_document_id: "22222222-2222-4222-8222-222222222222",
            extraction: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            adjudication: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            document_decision: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            compatibility_projection: { status: "COMPLETED", completed_count: 1, failed_count: 0, retryable: false, artifact_ids: {} },
            narrative: { status: "COMPLETED_WITH_ERRORS", completed_count: 0, failed_count: 1, retryable: true, error_summary: "Optional narrative exceeded its output budget.", artifact_ids: {} },
            synthesis_readiness: "READY_WITH_FALLBACK",
            selected_output_kind: "DETERMINISTIC_COMPATIBILITY",
            retryable_stages: ["NARRATIVE"],
          },
        ],
      } as never;
      if (path.endsWith("/retry") && init?.method === "POST") return {
        assessment_id: assessment.id,
        matter_document_id: "22222222-2222-4222-8222-222222222222",
        stage: "NARRATIVE",
        target_workflow_run_id: "33333333-3333-4333-8333-333333333333",
        operation_workflow_id: "evidence-stage-retry:test",
        status: "QUEUED",
        reused_completed_work: true,
        operator_message: "Narrative retry queued. Completed durable work will be reused.",
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/Retry narrative\. Completed stages will be reused/)).toBeInTheDocument();
    expect(screen.getByText("Documents summarized")).toBeInTheDocument();
    expect(screen.queryByText("Evidence created")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Evidence-first analysis" })).not.toBeInTheDocument();
    expect(screen.queryByText("Narrative fallback used.")).not.toBeInTheDocument();
    const affectedOutcomes = screen.getByText(/Retry narrative\. Completed stages will be reused/).closest("details");
    await user.click(affectedOutcomes?.querySelector("summary") as HTMLElement);
    expect(screen.getByText(/Optional narrative exceeded its output budget/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Actions for document 22222222" }));
    await user.click(await screen.findByText("Retry narrative"));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-evidence/evidence-outcomes/22222222-2222-4222-8222-222222222222/retry",
      { method: "POST", body: JSON.stringify({ stage: "NARRATIVE" }) },
    ));
    const diagnostics = screen.getByRole("heading", { name: "Technical diagnostics" }).closest("details");
    expect(diagnostics).not.toHaveAttribute("open");
    await user.click(diagnostics?.querySelector("summary") as HTMLElement);
    expect(await screen.findByText("Evidence-first workflow diagnostics")).toBeInTheDocument();
  });

  it("explains why a document needs attention and gives an operator next step", async () => {
    const assessment = {
      id: "assessment-evidence-failure",
      name: "Evidence assessment with one failed document",
      review_batch_id: "batch-evidence-failure",
      status: "COMPLETED_WITH_ERRORS",
      synthesis_status: "COMPLETED",
      completion_disposition: "READY_WITH_WARNINGS",
      current_synthesis_attempt_id: null,
      technical_diagnostics_visible: true,
      synthesis_diagnostics: {},
      guidance_refinement_status: "NOT_REQUIRED",
      selected_count: 250,
      summarized_count: 249,
      skipped_count: 0,
      failed_count: 1,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: { status: "COMPLETED" },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=false")) return { steps: [], skill_runs: [] } as never;
      if (path.endsWith("/evidence-outcomes")) return {
        assessment_id: assessment.id,
        document_count: 250,
        readiness_counts: { READY: 249, ACTION_REQUIRED: 1 },
        stage_status_counts: { extraction: { COMPLETED: 249, FAILED: 1 } },
        source_workflow_count: 249,
        source_artifact_count: 249,
        technical_diagnostics_visible: true,
        usage: { request_count: 249 },
        documents: [{
          matter_document_id: "869cce0b-c9df-44c1-b053-f194169f1330",
          extraction: { status: "FAILED", completed_count: 0, failed_count: 1, retryable: false, error_code: "EXTRACTION_DBOS_MAX_RETRIES", error_summary: "Selected document has no preferred text source", artifact_ids: {} },
          adjudication: { status: "NOT_STARTED", completed_count: 0, failed_count: 0, retryable: false, artifact_ids: {} },
          document_decision: { status: "NOT_STARTED", completed_count: 0, failed_count: 0, retryable: false, artifact_ids: {} },
          compatibility_projection: { status: "NOT_STARTED", completed_count: 0, failed_count: 0, retryable: false, artifact_ids: {} },
          narrative: { status: "NOT_REQUESTED", completed_count: 0, failed_count: 0, retryable: false, artifact_ids: {} },
          synthesis_readiness: "ACTION_REQUIRED",
          selected_output_kind: null,
          retryable_stages: [],
        }],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/1 failed document can be retried without reprocessing the 249 completed summaries/i)).toBeInTheDocument();
    await user.click(await screen.findByText("1 document outcome needing attention"));
    expect(screen.getByText(/Why it needs attention:/).closest("p")).toHaveTextContent("Extraction failed. Selected document has no preferred text source");
    expect(screen.getByText(/Impact:/).closest("p")).toHaveTextContent("This document was not included in the assessment synthesis");
    expect(screen.getByText(/Next step:/).closest("p")).toHaveTextContent("Open the document to confirm the missing content, add or regenerate its preferred text source");
    expect(screen.getByRole("link", { name: "Open document" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-evidence-failure&document=869cce0b-c9df-44c1-b053-f194169f1330");
    expect(screen.queryByText("Operator review required")).not.toBeInTheDocument();
  });

  it("explains a required synthesis failure and retries only synthesis", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Privilege assessment",
      status: "FAILED",
      synthesis_status: "PARTIAL",
      completion_disposition: "ACTION_REQUIRED",
      current_synthesis_attempt_id: "attempt-1",
      technical_diagnostics_visible: true,
      synthesis_diagnostics: { required_failure_count: 1 },
      guidance_refinement_status: "NOT_READY",
      selected_count: 250,
      summarized_count: 239,
      skipped_count: 11,
      failed_count: 1,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: { status: "COMPLETED_WITH_ERRORS" },
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.endsWith("/synthesis-units")) return [{
        id: "unit-1",
        unit_type: "REFINEMENT_DIMENSION",
        unit_key: "TEMPORAL_SCOPE",
        status: "FAILED",
        error_code: "USAGE_LIMIT_EXCEEDED",
        error_message: "Output limit exceeded",
        attempt_count: 1,
      }] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      if (path.endsWith("/regenerate-synthesis") && init?.method === "POST") {
        return { ...assessment, status: "QUEUED", synthesis_status: "NOT_STARTED" } as never;
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    const diagnosticsHeading = await screen.findByRole("heading", { name: "Technical diagnostics" });
    const diagnostics = diagnosticsHeading.closest("details");
    expect(diagnostics).not.toHaveAttribute("open");
    expect(diagnostics).toHaveClass("order-last");
    await user.click(diagnostics?.querySelector("summary") as HTMLElement);
    expect(diagnostics).toHaveAttribute("open");
    expect(await screen.findByText("Synthesis health · action required")).toBeInTheDocument();
    expect(await screen.findByText(/usage_limit_exceeded: Output limit exceeded/i)).toBeInTheDocument();
    expect(screen.getByText(/completed units will be reused/i)).toBeInTheDocument();
    expect(screen.queryByText("Assessment failed")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry assessment" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Retry synthesis" })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Retry synthesis" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-1/regenerate-synthesis",
      { method: "POST" },
    ));
  });

  it("shows reporting-only unmapped criteria with retained evidence and synthesis guidance", async () => {
    const assessment = {
      id: "assessment-unmapped",
      name: "Guidance assessment",
      status: "COMPLETED",
      synthesis_status: "COMPLETED",
      completion_disposition: "READY_WITH_WARNINGS",
      current_synthesis_attempt_id: "attempt-unmapped",
      technical_diagnostics_visible: true,
      synthesis_diagnostics: {},
      guidance_refinement_status: "NOT_REQUIRED",
      selected_count: 10,
      summarized_count: 10,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {
        status: "COMPLETED",
        other_unmapped_criteria: {
          criterion_key: "other_unmapped_criteria",
          label: "Other / unmapped criteria",
          reporting_only: true,
          occurrence_count: 2,
          document_count: 1,
          items: [{
            source_key: "novel_scope",
            occurrence_count: 2,
            document_count: 1,
            match_type_counts: { NEAR_MISS: 2 },
            representative_evidence: [{
              matter_document_id: "11111111-1111-4111-8111-111111111111",
              label: "Novel scope",
              match_type: "NEAR_MISS",
              confidence: 0.76,
              evidence: [{ reason: "The document presents an uncatalogued scope issue.", paragraph_ids: ["¶4"] }],
            }],
          }],
          synthesis: {
            disposition: "QUESTION_PROPOSED",
            rationale: "The unmatched label may identify a guidance gap.",
            question: "Should novel-scope documents be included?",
          },
        },
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.endsWith("/synthesis-units")) return [{
        id: "unit-unmapped",
        unit_type: "SIGNAL_DISPOSITION",
        unit_key: "OTHER_UNMAPPED_CRITERIA",
        status: "COMPLETED",
        attempt_count: 1,
      }] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    const diagnosticsHeading = await screen.findByRole("heading", { name: "Technical diagnostics" });
    await user.click(diagnosticsHeading.closest("details")?.querySelector("summary") as HTMLElement);
    expect(await screen.findByRole("heading", { name: "Other / unmapped criteria" })).toBeInTheDocument();
    expect(screen.getByText(/retained for review but are not counted as guidance topics/i)).toBeInTheDocument();
    expect(screen.getByText(/synthesis recommendation · question proposed/i)).toBeInTheDocument();
    expect(screen.getByText(/should novel-scope documents be included/i)).toBeInTheDocument();
    await user.click(screen.getByText("novel_scope"));
    expect(screen.getByText(/uncatalogued scope issue/i)).toBeInTheDocument();
    expect(screen.getByText(/near miss 2/i)).toBeInTheDocument();
  });

  it("explains recoverable synthesis output adjustments", async () => {
    const assessment = {
      id: "assessment-normalized",
      name: "Normalized assessment",
      status: "COMPLETED_WITH_ERRORS",
      synthesis_status: "COMPLETED",
      completion_disposition: "READY_WITH_WARNINGS",
      current_synthesis_attempt_id: "attempt-normalized",
      technical_diagnostics_visible: true,
      synthesis_diagnostics: {
        normalization_warning_count: 1,
        normalization_warnings: [{
          unit_type: "SIGNAL_DISPOSITION",
          unit_key: "NEAR_MISS_GENERAL",
          code: "INAPPLICABLE_QUESTION_CONTENT_DROPPED",
          path: "question",
          message: "Question content was removed because the disposition does not propose a question.",
          original_value: "Should we add a rule?",
          normalized_value: "",
        }],
      },
      guidance_refinement_status: "NOT_REQUIRED",
      selected_count: 10,
      summarized_count: 10,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: { status: "COMPLETED" },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.endsWith("/synthesis-units")) return [{
        id: "unit-normalized",
        unit_type: "SIGNAL_DISPOSITION",
        unit_key: "NEAR_MISS_GENERAL",
        status: "COMPLETED",
        attempt_count: 1,
      }] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Assessment synthesis · completed")).toBeInTheDocument();
    const diagnosticsHeading = await screen.findByRole("heading", { name: "Technical diagnostics" });
    await user.click(diagnosticsHeading.closest("details")?.querySelector("summary") as HTMLElement);
    expect(screen.getByText("Synthesis health · ready with warnings")).toBeInTheDocument();
    await user.click(screen.getByText("1 model output adjustment"));
    expect(screen.getByText(/Question content was removed/)).toBeInTheDocument();
    expect(screen.getByText(/INAPPLICABLE_QUESTION_CONTENT_DROPPED/)).toBeInTheDocument();
  });

  it("shows operator conclusions while withholding tenant technical diagnostics", async () => {
    const assessment = {
      id: "assessment-operator",
      name: "Operator assessment",
      status: "COMPLETED",
      synthesis_status: "COMPLETED",
      completion_disposition: "READY_WITH_WARNINGS",
      current_synthesis_attempt_id: "attempt-operator",
      technical_diagnostics_visible: false,
      synthesis_diagnostics: {},
      guidance_refinement_status: "NOT_REQUIRED",
      selected_count: 250,
      summarized_count: 239,
      skipped_count: 0,
      failed_count: 11,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {
        status: "COMPLETED",
        narrative: "The assessed documents support two privilege conclusions.",
        findings: [{
          topic_key: "attorney_client_privilege",
          label: "Attorney-client privilege",
          finding: "The sample contains communications seeking legal advice.",
          limitations: ["The conclusion is limited to the assessed sample."],
          evidence: [{ matter_document_id: "document-1", paragraph_ids: ["¶1"], reason: "Legal advice requested." }],
        }],
        topics: [
          { topic_key: "attorney_client_privilege", label: "Attorney-client privilege" },
          { topic_key: "work_product", label: "Work product" },
        ],
        clarification_questions: [],
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.endsWith("/synthesis-units")) throw new Error("Operator must not request synthesis units");
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByRole("heading", { name: "Assessment conclusions" })).toBeInTheDocument();
    expect(screen.getByText("Attorney-client privilege")).toBeInTheDocument();
    expect(screen.getByText("The sample contains communications seeking legal advice.")).toBeInTheDocument();
    expect(screen.getByText("1 criterion with no separate finding")).toBeInTheDocument();
    expect(screen.getByText("Assessment synthesis · completed with document errors")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Technical diagnostics" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Other / unmapped criteria" })).not.toBeInTheDocument();
    expect(coreApi).not.toHaveBeenCalledWith(expect.stringContaining("/synthesis-units"));
  });

  it("archives failed assessments and can reveal and restore history", async () => {
    const failed = {
      id: "assessment-failed",
      name: "Failed planning attempt",
      status: "FAILED",
      archived_at: null,
      archive_reason: null,
      guidance_refinement_status: "NOT_READY",
      selected_count: 0,
      summarized_count: 0,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      coverage_snapshot: null,
      synthesis_result: null,
    };
    const completed = {
      ...failed,
      id: "assessment-completed",
      name: "Successful assessment",
      status: "COMPLETED",
    };
    let isArchived = false;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments" && !init?.method) {
        return (isArchived ? [completed] : [failed, completed]) as never;
      }
      if (path === "/v1/matters/matter-1/definition-assessments?include_archived=true") {
        return [{
          ...failed,
          archived_at: isArchived ? "2026-10-26T12:00:00Z" : null,
          archive_reason: isArchived ? "MANUAL" : null,
        }, completed] as never;
      }
      if (path.endsWith("/archive-failed") && init?.method === "POST") {
        isArchived = true;
        return { archived_count: 1, assessment_ids: [failed.id] } as never;
      }
      if (path.endsWith("/assessment-failed/restore") && init?.method === "POST") {
        isArchived = false;
        return failed as never;
      }
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Archive failed (1)" }));
    await waitFor(() => expect(screen.queryByText(/Failed planning attempt/)).not.toBeInTheDocument());
    expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/archive-failed",
      { method: "POST", body: JSON.stringify({ guidance_id: null }) },
    );

    await user.click(screen.getByRole("button", { name: "Show archived" }));
    expect(await screen.findByRole("button", { name: "Restore" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Assessment" })).toHaveTextContent("Failed planning attempt · archived");

    await user.click(screen.getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-failed/restore",
      { method: "POST" },
    ));
  });

  it("defaults new assessments to provider batching and sends the selected mode", async () => {
    let launchPayload: Record<string, unknown> | undefined;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments" && init?.method === "POST") {
        launchPayload = JSON.parse(String(init.body));
        return { id: "assessment-new" } as never;
      }
      if (path === "/v1/matters/matter-1/definition-assessments") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ id: "revision-1", revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "New" }));
    expect(screen.getByLabelText("Target documents")).toHaveValue(500);
    const batching = screen.getByRole("checkbox", { name: /Use Batch API/ });
    expect(batching).toBeChecked();
    await user.type(screen.getByLabelText("Assessment name"), "Batch assessment");
    await user.click(screen.getByRole("button", { name: "Start assessment" }));
    await waitFor(() => {
      expect(launchPayload?.use_batching).toBe(true);
      expect(launchPayload?.target_document_count).toBe(500);
      expect(launchPayload?.execution_mode).toBe("DIRECT_V1");
    });
  });

  it("allows an operator to select the controlled evidence-first route", async () => {
    let launchPayload: Record<string, unknown> | undefined;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments" && init?.method === "POST") {
        launchPayload = JSON.parse(String(init.body));
        return { id: "assessment-evidence" } as never;
      }
      if (path === "/v1/matters/matter-1/definition-assessments") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ id: "revision-1", revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "New" }));
    await user.type(screen.getByLabelText("Assessment name"), "Evidence assessment");
    await user.click(screen.getByRole("combobox", { name: "Processing path" }));
    await user.click(screen.getByRole("option", { name: "Evidence-first · controlled rollout" }));
    await user.click(screen.getByRole("button", { name: "Start assessment" }));

    await waitFor(() => expect(launchPayload?.execution_mode).toBe("EVIDENCE_FIRST_V1"));
  });

  it("offers summary reuse or frozen-batch reanalysis", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED",
      review_batch_id: "batch-1",
      selected_count: 12,
      summarized_count: 11,
      skipped_count: 1,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: 12000,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {},
    };
    let renamePayload: Record<string, unknown> | undefined;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path === "/v1/matters/matter-1/definition-assessments/assessment-1" && init?.method === "PATCH") {
        renamePayload = JSON.parse(String(init.body));
        return { ...assessment, name: renamePayload?.name } as never;
      }
      if (path.endsWith("/queries")) return [] as never;
      if (path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=false")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      if (path.endsWith("/regenerate-document-analyses") && init?.method === "POST") {
        return { ...assessment, status: "QUEUED", summarized_count: 0 } as never;
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    const coverageHeading = await screen.findByText("Coverage: sufficient");
    expect(within(coverageHeading.parentElement as HTMLElement).getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Documents skipped")).toBeInTheDocument();
    expect(screen.getByText(/1 selected document was skipped because no analyzable text was available/i)).toBeInTheDocument();
    expect(screen.getByText(/This was not a model failure/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run analysis" })).toHaveAttribute(
      "href",
      "/review/matters/matter-1?batch=batch-1&analysis=run",
    );
    await user.click(screen.getByRole("button", { name: "Rename assessment" }));
    const renameInput = screen.getByLabelText("Assessment name");
    await user.clear(renameInput);
    await user.type(renameInput, "Updated coverage assessment");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await waitFor(() => {
      expect(renamePayload).toEqual({ name: "Updated coverage assessment" });
      expect(screen.getByRole("combobox", { name: "Assessment" })).toHaveTextContent("Updated coverage assessment");
    });
    const regenerateButton = await screen.findByRole("button", { name: "Regenerate assessment" });
    expect(regenerateButton).toBeVisible();
    const regenerateTooltip = screen.getByRole("tooltip", { name: "Regenerate assessment" });
    expect(regenerateButton).toHaveAttribute("aria-describedby", regenerateTooltip.id);
    expect(regenerateTooltip).toHaveClass("group-hover:visible", "group-focus-within:visible");
    await user.click(regenerateButton);
    expect(screen.getByText("Reuse current document analyses")).toBeInTheDocument();
    expect(screen.getByText("Reanalyze the frozen batch")).toBeInTheDocument();
    expect(screen.getByText(/11 existing analyses/)).toBeInTheDocument();
    expect(screen.getByText(/all 12 frozen documents/)).toBeInTheDocument();
    expect(screen.getByText(/Synthesis completed without generating clarification questions/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reanalyze documents" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-1/regenerate-document-analyses",
      { method: "POST" },
    ));
  });

  it("refreshes the cumulative frozen-batch summary count after retrying failed documents", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED_WITH_ERRORS",
      guidance_refinement_status: "NOT_READY",
      review_batch_id: "batch-1",
      selected_count: 1000,
      summarized_count: 119,
      skipped_count: 1,
      failed_count: 880,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: 12000,
      coverage_snapshot: null,
      synthesis_result: null,
    };
    let listRequests = 0;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments" && !init?.method) {
        listRequests += 1;
        return [{
          ...assessment,
          ...(listRequests > 1 ? { status: "QUEUED", summarized_count: 986, failed_count: 9 } : {}),
        }] as never;
      }
      if (path === "/v1/matters/matter-1/definition-assessments/assessment-1/retry" && init?.method === "POST") {
        return { ...assessment, status: "QUEUED" } as never;
      }
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("119 / 1,000")).toBeInTheDocument();
    expect(await screen.findByText("Refinement questions will appear after assessment synthesis completes.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry failed documents" }));
    expect(await screen.findByText("986 / 1,000")).toBeInTheDocument();
    expect(listRequests).toBeGreaterThan(1);
  });

  it("shows retrieval clarification without offering a blind retry", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Topic coverage assessment",
      status: "FAILED",
      error_message: "Retrieval needs clarification before this assessment can start.",
      review_batch_id: null,
      selected_count: 0,
      summarized_count: 0,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: null,
      coverage_snapshot: null,
      synthesis_result: null,
      configuration_snapshot: {
        failure_kind: "RETRIEVAL_NEEDS_CLARIFICATION",
        retrieval_validation: { zero_result_topics: ["Marketing", "Bottled Water"] },
      },
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries") || path.endsWith("/questions")) return [] as never;
      if (path.includes("/execution?include_skill_runs=true")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("Retrieval needs clarification")).toBeInTheDocument();
    expect(screen.getByText(/Marketing, Bottled Water/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry assessment" })).not.toBeInTheDocument();
  });

  it("lets a reviewer select a suggested refinement answer", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED",
      guidance_refinement_status: "NOT_READY",
      review_batch_id: "batch-1",
      selected_count: 2,
      summarized_count: 2,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: 2000,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {},
    };
    const question = {
      id: "question-1",
      question: "Should indirect storm claims be included?",
      rationale: "The existing boundary is ambiguous.",
      priority: "HIGH",
      status: "OPEN",
      evidence: [{
        matter_document_id: "document-1",
        paragraph_ids: ["¶4", "¶7"],
        reason: "The cited discussion treats an indirect claim differently from a direct claim.",
      }],
      suggested_answers: ["Include indirect claims.", "Exclude indirect claims."],
      answer: null,
    };
    let updatePayload: Record<string, unknown> | undefined;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries")) return [] as never;
      if (path.endsWith("/questions") && !init?.method) return [question] as never;
      if (path.endsWith("/questions/question-1") && init?.method === "PUT") {
        const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
        updatePayload = payload;
        return { ...question, status: "ANSWERED", answer: payload.answer } as never;
      }
      if (path.includes("/execution?include_skill_runs=false")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    const supportingDocuments = await screen.findByText("Supporting documents (1)");
    await user.click(supportingDocuments);
    expect(screen.getByText("Document 1 · ¶4, ¶7")).toBeInTheDocument();
    expect(screen.getByText("The cited discussion treats an indirect claim differently from a direct claim.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open cited document" })).toHaveAttribute(
      "href",
      "/review/matters/matter-1?batch=batch-1&document=document-1&paragraph=%C2%B64%2C%C2%B67",
    );
    expect(screen.getByRole("link", { name: "Open cited document" })).toHaveAttribute("target", "_blank");
    await user.click(await screen.findByRole("button", { name: "Include indirect claims." }));
    expect(screen.getByPlaceholderText("Select a suggestion or enter a custom answer…")).toHaveValue(
      "Include indirect claims.",
    );
    await user.click(screen.getByRole("button", { name: "Answer" }));
    await waitFor(() => expect(updatePayload).toEqual({
      status: "ANSWERED",
      answer: "Include indirect claims.",
    }));
  });

  it("does not block other questions while one answer request is pending", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED",
      guidance_refinement_status: "NOT_READY",
      review_batch_id: "batch-1",
      selected_count: 2,
      summarized_count: 2,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: 2000,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {},
    };
    const questions = [
      {
        id: "question-1",
        question: "Should the first boundary be included?",
        rationale: "The first boundary is ambiguous.",
        priority: "HIGH",
        status: "OPEN",
        evidence: [],
        suggested_answers: ["Include the first boundary."],
        answer: null,
      },
      {
        id: "question-2",
        question: "Should the second boundary be included?",
        rationale: "The second boundary is ambiguous.",
        priority: "MEDIUM",
        status: "OPEN",
        evidence: [],
        suggested_answers: ["Include the second boundary."],
        answer: null,
      },
    ];
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries")) return [] as never;
      if (path.endsWith("/questions") && !init?.method) return questions as never;
      if (path.endsWith("/questions/question-1") && init?.method === "PUT") {
        return await new Promise<never>(() => undefined);
      }
      if (path.includes("/execution?include_skill_runs=false")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={() => undefined}
          onViewDraftDifferences={() => undefined}
        />
      </QueryClientProvider>,
    );

    const firstCard = (await screen.findByText("Should the first boundary be included?")).parentElement?.parentElement;
    const secondCard = screen.getByText("Should the second boundary be included?").parentElement?.parentElement;
    expect(firstCard).toBeTruthy();
    expect(secondCard).toBeTruthy();
    await user.click(within(firstCard as HTMLElement).getByRole("button", { name: "Include the first boundary." }));
    await user.click(within(firstCard as HTMLElement).getByRole("button", { name: "Answer" }));

    await waitFor(() => expect(within(firstCard as HTMLElement).getByRole("button", { name: "Answer" })).toBeDisabled());
    expect(within(secondCard as HTMLElement).getByRole("button", { name: "Include the second boundary." })).toBeEnabled();
    await user.click(within(secondCard as HTMLElement).getByRole("button", { name: "Include the second boundary." }));
    expect(within(secondCard as HTMLElement).getByPlaceholderText("Select a suggestion or enter a custom answer…")).toHaveValue(
      "Include the second boundary.",
    );
  });

  it("presents the generated refinement as an explicit review-and-publish step", async () => {
    const onReviewDraft = vi.fn();
    const onViewDraftDifferences = vi.fn();
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED",
      guidance_refinement_status: "COMPLETED",
      review_batch_id: "batch-1",
      selected_count: 2,
      summarized_count: 2,
      skipped_count: 0,
      failed_count: 0,
      partial_coverage_count: 0,
      invalid_result_count: 0,
      estimated_input_tokens: 2000,
      coverage_snapshot: { status: "SUFFICIENT" },
      synthesis_result: {},
    };
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
      if (path.endsWith("/queries")) return [] as never;
      if (path.endsWith("/questions")) return [{
        id: "question-1",
        question: "Should indirect claims be included?",
        rationale: "The boundary was unclear.",
        priority: "HIGH",
        blocking: true,
        status: "ANSWERED",
        answer: "Include indirect claims.",
        evidence: [],
        suggested_answers: [],
      }] as never;
      if (path.includes("/execution?include_skill_runs=false")) return {
        request_count: 0,
        input_tokens: 0,
        cached_input_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        steps: [],
        skill_runs: [],
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MatterDefinitionAssessmentPanel
          matterId="matter-1"
          revisions={[{ revision: 1 } as never]}
          publishedRevision={1}
          onReviewDraft={onReviewDraft}
          onViewDraftDifferences={onViewDraftDifferences}
        />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("A revised Matter Definition draft is ready.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "View differences" }));
    expect(onViewDraftDifferences).toHaveBeenCalledOnce();
    await userEvent.click(screen.getByRole("button", { name: "Review draft and publish" }));
    expect(onReviewDraft).toHaveBeenCalledOnce();
  });
});
