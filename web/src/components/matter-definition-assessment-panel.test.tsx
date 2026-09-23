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
});
