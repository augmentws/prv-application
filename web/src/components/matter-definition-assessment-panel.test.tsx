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

describe("MatterDefinitionAssessmentPanel regeneration", () => {
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
    });
  });

  it("offers summary reuse or frozen-batch reanalysis", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED_WITH_ERRORS",
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
    expect(within(coverageHeading.parentElement as HTMLElement).getByText("completed with errors")).toBeInTheDocument();
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
