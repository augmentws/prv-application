import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobOutcomesDialog } from "@/components/job-outcomes-dialog";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

describe("JobOutcomesDialog", () => {
  beforeEach(() => vi.clearAllMocks());

  it("distinguishes reused and skipped documents and filters details", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path.endsWith("/job-outcomes/MATTER_EMBEDDING_JOB/job-1")) return {
        id: "manifest-1",
        job_type: "MATTER_EMBEDDING_JOB",
        job_id: "job-1",
        manifest_status: "FINALIZED",
        outcome_schema: "priv-view.job-item-outcomes",
        outcome_schema_version: 1,
        item_count: 3,
        status_counts: { SUCCEEDED: 1, REUSED: 1, SKIPPED: 1 },
        reason_counts: { "embedding.generated": 1, "embedding.reused_existing_vector": 1, "embedding.no_text_source": 1 },
        artifact_id: "artifact-1",
        artifact_content_hash: "a".repeat(64),
        artifact_byte_length: 100,
        finalized_at: "2026-10-15T12:00:00Z",
        failure_summary: null,
        item_details_available: true,
        legacy: false,
        created_at: "2026-10-15T12:00:00Z",
        updated_at: "2026-10-15T12:00:00Z",
      } as never;
      if (path.includes("/items?")) return {
        total: 1,
        offset: 0,
        limit: 100,
        items: [{
          ordinal: 1,
          item_kind: "MATTER_DOCUMENT",
          item_id: "document-1",
          outcome_status: "REUSED",
          reason_code: "embedding.reused_existing_vector",
          message: null,
          batch_id: "batch-1",
          attempt_number: 1,
          output_references: [],
          metrics: { chunk_count: 4 },
          payload: {},
          started_at: null,
          completed_at: null,
        }],
      } as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={queryClient}><JobOutcomesDialog matterId="matter-1" jobType="MATTER_EMBEDDING_JOB" jobId="job-1" jobName="Embeddings" initialStatus="REUSED" /></QueryClientProvider>);

    await user.click(screen.getByRole("button", { name: "Outcomes" }));

    expect(await screen.findByText("Generated")).toBeInTheDocument();
    expect(screen.getAllByText("Reused").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Skipped").length).toBeGreaterThan(0);
    expect(await screen.findByText("embedding.reused_existing_vector")).toBeInTheDocument();
    expect(coreApi).toHaveBeenCalledWith(expect.stringContaining("status=REUSED"));
    expect(screen.getByRole("link", { name: "Open document" })).toHaveAttribute("href", "/review/matters/matter-1?document=document-1");
  });
});
