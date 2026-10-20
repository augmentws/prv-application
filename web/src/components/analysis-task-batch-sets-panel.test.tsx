import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisTaskBatchSetsPanel } from "@/components/analysis-task-batch-sets-panel";
import type { AnalysisTaskBatchSetRead, MatterAnalysisTaskRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const task = {
  id: "task-1",
  matter_id: "matter-1",
  key: "privilege",
  name: "Privilege review",
  task_type: "QUESTION_ANSWERING",
  workflow_key: "question_answering_v1",
  current_version: 1,
  published_version: 1,
  status: "ACTIVE",
  version: {},
  created_by_user_id: "user-1",
  created_at: "2026-10-20T12:00:00Z",
  updated_at: "2026-10-20T12:00:00Z",
} as MatterAnalysisTaskRead;

const createdSet = {
  id: "set-1",
  tenant_id: "tenant-1",
  matter_id: "matter-1",
  matter_analysis_task_id: "task-1",
  matter_analysis_task_version_id: "version-1",
  name: "Privilege wave",
  description: null,
  population_source_type: "ALL_MATTER",
  selection_definition: {},
  source_review_batch_id: null,
  search_index_generation_id: null,
  target_batch_size: 500,
  allocation_policy: { keep_together: ["FAMILY"] },
  random_seed: "seed",
  allocation_algorithm_version: "v1",
  status: "QUEUED",
  workflow_id: "workflow-1",
  population_count: 0,
  batch_count: 0,
  population_hash: null,
  warning_details: [],
  error_message: null,
  created_by_user_id: "user-1",
  created_at: "2026-10-20T12:00:00Z",
  updated_at: "2026-10-20T12:00:00Z",
  completed_at: null,
  children: [],
} as AnalysisTaskBatchSetRead;

describe("AnalysisTaskBatchSetsPanel", () => {
  it("requires a preview and creates a deterministic whole-matter partition", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path.endsWith("/batch-sets/preview") && init?.method === "POST") return {
        population_count: 1200,
        estimated_batch_count: 3,
        estimated_batch_sizes: [500, 500, 200],
        population_hash: "a".repeat(64),
        oversized_groups: [],
        warnings: [],
      } as never;
      if (path.endsWith("/batch-sets") && init?.method === "POST") return createdSet as never;
      if (path.endsWith("/batch-sets")) return [] as never;
      if (path.endsWith("/review-batches") || path.endsWith("/saved-searches")) return [] as never;
      throw new Error(`Unexpected API call: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><AnalysisTaskBatchSetsPanel matterId="matter-1" task={task} /></QueryClientProvider>);
    expect(await screen.findByText("No partitioned batch sets have been created for this task.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New partitioned set" }));
    await user.type(screen.getByLabelText("Name"), "Privilege wave");
    expect(screen.getByRole("button", { name: "Create set" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Preview allocation" }));

    expect(await screen.findByText("1,200 documents → 3 batches")).toBeInTheDocument();
    expect(screen.getByText(/Each document appears in exactly one child batch in this set/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create set" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/analysis-tasks/task-1/batch-sets",
      expect.objectContaining({ method: "POST" }),
    ));
    const createCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/batch-sets") && init?.method === "POST");
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual(expect.objectContaining({
      name: "Privilege wave",
      population_source_type: "ALL_MATTER",
      target_batch_size: 500,
      keep_together: ["FAMILY"],
    }));
  });
});
