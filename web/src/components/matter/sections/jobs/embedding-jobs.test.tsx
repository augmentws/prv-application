import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EmbeddingJobs } from "@/components/matter/sections/jobs/embedding-jobs";
import type { MatterEmbeddingJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/matter/matter-context", () => ({
  useMatter: () => ({ matterId: "matter-1", permissionKeys: new Set(["job.delete"]) }),
}));

const stalledIndexJob: MatterEmbeddingJobRead = {
  id: "embedding-job-1",
  matter_id: "matter-1",
  status: "RUNNING",
  workflow_id: "matter-embedding:embedding-job-1",
  configuration_hash: "a".repeat(64),
  embedding_model: "voyage-4-lite",
  embedding_model_revision: null,
  embedding_dimensions: 1024,
  embedding_normalized: true,
  total_count: 20365,
  batch_count: 82,
  processed_count: 20365,
  embedded_count: 20139,
  reused_count: 0,
  skipped_count: 226,
  failed_count: 0,
  search_projection_status: "FAILED",
  search_projection_error_message: "OpenSearch returned 413",
  search_projection_retry_available: true,
  chunk_count: 87279,
  provider_request_count: 350,
  input_tokens: 1000000,
  output_tokens: 0,
  total_tokens: 1000000,
  error_message: "Search indexing failed: OpenSearch returned 413",
  created_by_user_id: "user-1",
  started_at: "2026-11-12T22:21:35Z",
  completed_at: "2026-11-12T23:32:54Z",
  canceled_at: null,
  created_at: "2026-11-12T22:21:35Z",
  updated_at: "2026-11-12T23:32:54Z",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("EmbeddingJobs", () => {
  it("fits the consolidated table to its container and exposes retry indexing", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/embedding-jobs" && !init) return [stalledIndexJob] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs/embedding-job-1/retry-index" && init?.method === "POST") {
        return { ...stalledIndexJob, status: "QUEUED", error_message: null } as never;
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={queryClient}><EmbeddingJobs /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Document embeddings" })).toBeInTheDocument();
    expect(screen.getByRole("table")).toHaveStyle({ width: "100%" });
    expect(screen.getByRole("columnheader", { name: "Outcomes" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Usage" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Generated" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Actions for voyage-4-lite embedding job" }));
    expect(screen.getByRole("menuitem", { name: "View outcomes" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Cancel job" })).toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: "Retry indexing" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/embedding-jobs/embedding-job-1/retry-index",
      { method: "POST" },
    ));
  });
});
