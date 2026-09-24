import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
        />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "New" }));
    const batching = screen.getByRole("checkbox", { name: /Use Batch API/ });
    expect(batching).toBeChecked();
    await user.type(screen.getByLabelText("Assessment name"), "Batch assessment");
    await user.click(screen.getByRole("button", { name: "Start assessment" }));
    await waitFor(() => expect(launchPayload?.use_batching).toBe(true));
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
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/definition-assessments") return [assessment] as never;
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
        />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole("button", { name: "Regenerate assessment" }));
    expect(screen.getByText("Reuse current document analyses")).toBeInTheDocument();
    expect(screen.getByText("Reanalyze the frozen batch")).toBeInTheDocument();
    expect(screen.getByText(/11 existing analyses/)).toBeInTheDocument();
    expect(screen.getByText(/all 12 frozen documents/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reanalyze documents" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/definition-assessments/assessment-1/regenerate-document-analyses",
      { method: "POST" },
    ));
  });

  it("lets a reviewer select a suggested refinement answer", async () => {
    const assessment = {
      id: "assessment-1",
      name: "Coverage assessment",
      status: "COMPLETED",
      guidance_refinement_status: "NOT_READY",
      review_batch_id: null,
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
      evidence: [],
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
        />
      </QueryClientProvider>,
    );

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
});
