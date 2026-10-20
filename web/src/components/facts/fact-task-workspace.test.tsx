import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FactTaskWorkspace } from "@/components/facts/fact-task-workspace";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/job-outcomes-dialog", () => ({ JobOutcomesDialog: ({ jobId }: { jobId: string }) => <span>Outcomes for {jobId}</span> }));

const specification = {
  schema_version: "fact-development-specification-v1",
  summary: "Build dated facts about pricing.",
  date_rules: "Use the event date.",
  owner_rules: "The decision maker owns the fact.",
  issues: [{
    key: "pricing",
    name: "Pricing decisions",
    description: null,
    fact_guidance: "One price change per fact.",
    source_refs: [{ source: "REVIEW_GUIDANCE", heading: "Pricing", excerpt_hash: "a".repeat(64) }],
    coding_mappings: [{ metadata_definition_key: "issue_coding", match_type: "ENUM_VALUE", value: "pricing", include_descendants: true }],
    renamed_from: null,
  }],
  limits: { recalled_facts_per_issue: 20, max_operations_per_document: 25 },
};

function version(overrides: Record<string, unknown> = {}) {
  return {
    id: "version-2",
    matter_analysis_task_id: "task-1",
    version: 2,
    status: "PUBLISHED",
    compilation_status: "READY",
    definition_markdown: "# Facts\n\nOne event per fact.",
    definition_content_hash: "b".repeat(64),
    review_guidance_id: "guidance-1",
    review_guidance_revision_id: "guidance-rev-1",
    review_guidance_revision: 1,
    review_guidance_content_hash: "c".repeat(64),
    review_guidance_pin_stale: true,
    fact_development_specification: specification,
    validation_report: { status: "VALID", warnings: [] },
    compilation_progress: {},
    ...overrides,
  };
}

const task = { id: "task-1", matter_id: "matter-1", key: "facts", name: "Facts", task_type: "FACT_DEVELOPMENT", current_version: 2, published_version: 2, status: "ACTIVE" };

function run(overrides: Record<string, unknown> = {}) {
  return {
    id: "run-1",
    matter_id: "matter-1",
    matter_analysis_task_id: "task-1",
    matter_analysis_task_version_id: "version-2",
    run_kind: "FACT_DEVELOPMENT",
    scope_type: "REVIEW_BATCH",
    scope_id: "batch-1",
    status: "COMPLETED",
    current_stage: "COMPLETED",
    progress_completed: 10,
    progress_total: 10,
    population_document_count: 10,
    configuration_snapshot: {},
    report: { counts: { facts_created: 4, merged_into_existing: 1, suggestions_created: 0, reused: 2, invalid_operations: 0 }, qc: { entities_awaiting_confirmation: 3 } },
    failure_code: null,
    failure_summary: null,
    failure_retriable: false,
    created_by_user_id: "user-1",
    started_at: null,
    completed_at: null,
    canceled_at: null,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-01T12:00:00Z",
    execution: { available_actions: [] },
    ...overrides,
  };
}

let runs: unknown[] = [run()];
let batchApiAvailable = false;
const onTaskUpdated = vi.fn();

function renderWorkspace(versionOverrides: Record<string, unknown> = {}, taskOverrides: Record<string, unknown> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FactTaskWorkspace clientId="client-1" matterId="matter-1" task={{ ...task, ...taskOverrides } as never} version={version(versionOverrides) as never} onTaskUpdated={onTaskUpdated} />
    </QueryClientProvider>,
  );
}

async function choose(user: ReturnType<typeof userEvent.setup>, section: string, label: string, option: string) {
  const card = (await screen.findByText(section)).closest("[data-slot='card'], .rounded-xl") as HTMLElement;
  await user.click(within(card).getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}

beforeEach(() => {
  runs = [run()];
  batchApiAvailable = false;
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/matters/matter-1/guidance") return [{ id: "guidance-1", name: "Second pass", status: "ACTIVE", published_revision: 3 }] as never;
    if (path === "/v1/matters/matter-1/review-batches") return [{ id: "batch-1", name: "Responsive", status: "READY", document_count: 10 }] as never;
    if (path === "/v1/matters/matter-1/review-batches/batch-1/comparison-options") return {
      sources: [
        { id: "coding-alice", source_type: "REVIEWER", label: "Alice Reviewer" },
        { id: "coding-bob", source_type: "REVIEWER", label: "Bob Reviewer" },
      ],
    } as never;
    if (path === "/v1/matters/matter-1/analysis-tasks/task-1/fact-runs" && !init?.method) return runs as never;
    if (path.endsWith("/fact-runs/estimate")) return { review_batch_id: "batch-1", task_version: 2, coding_source: "MATTER_METADATA", reviewer_coding_revision: null, batch_document_count: 10, matched_document_count: 8, excluded_document_count: 2, missing_text_document_count: 1, previously_extracted_count: 3, documents_to_extract: 4, text_bytes_to_extract: 4000, estimated_input_tokens: 17000, estimated_output_tokens: 3200, extraction_model: "test-model", batch_api_available: batchApiAvailable, chunk_count: 1, issue_document_counts: [] } as never;
    if (path.endsWith("/fact-runs") && init?.method === "POST") return run({ id: "run-2", status: "QUEUED" }) as never;
    if (path.endsWith("/issue-membership-preview")) return { review_batch_id: "batch-1", coding_source: "MATTER_METADATA", reviewer_coding_id: null, reviewer_coding_revision: null, batch_document_count: 10, matched_document_count: 8, issues: [{ issue_key: "pricing", name: "Pricing decisions", document_count: 8 }] } as never;
    if (path.endsWith("/retry")) return {} as never;
    if (path.endsWith("/versions") || path.endsWith("/fact-specification") || path.endsWith("/compile") || path.endsWith("/publish")) return task as never;
    throw new Error(`Unexpected request ${init?.method ?? "GET"} ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function bodyOf(predicate: (path: string) => boolean) {
  const call = vi.mocked(coreApi).mock.calls.find(([path, init]) => predicate(path) && init?.body !== undefined);
  return call ? JSON.parse(String(call[1]?.body)) : undefined;
}

describe("FactTaskWorkspace", () => {
  it("shows the pinned guidance, flags newer guidance, and adopts it as a new draft", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    expect(await screen.findByText("Second pass")).toBeInTheDocument();
    expect(screen.getByText("Newer guidance is published")).toBeInTheDocument();
    expect(screen.getByText("Pricing decisions")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Adopt revision 3" }));
    await waitFor(() => expect(bodyOf((path) => path.endsWith("/analysis-tasks/task-1/versions"))).toEqual({
      definition_markdown: "# Facts\n\nOne event per fact.",
      based_on_version: 2,
      review_guidance_revision: 3,
    }));
  });

  it("validates and saves an edited catalog on a draft, then can publish", async () => {
    const user = userEvent.setup();
    renderWorkspace({ status: "DRAFT", review_guidance_pin_stale: false }, { published_version: 1 });
    await user.click(await screen.findByRole("button", { name: "Edit JSON" }));
    await user.click(screen.getByRole("button", { name: /Validate & save/ }));
    await waitFor(() => expect(bodyOf((path) => path.endsWith("/versions/2/fact-specification"))).toEqual({ fact_development_specification: specification }));
    expect(screen.getByText("Publish this version to run fact development with it.")).toBeInTheDocument();
  });

  it("estimates a run before launching it for the documents left to read", async () => {
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    await choose(user, "Run fact development", "Ready batch", "Responsive · 10 docs");
    await user.click(screen.getByRole("button", { name: "Estimate" }));
    expect(await screen.findByText("Documents to read")).toBeInTheDocument();
    expect(screen.getByText(/17,000 in · 3,200 out/)).toBeInTheDocument();
    expect(screen.getByText("The extraction model has no Batch API support, so the run uses real-time calls.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Launch run for 4 documents" }));
    await waitFor(() => {
      const body = bodyOf((path) => path === "/v1/matters/matter-1/analysis-tasks/task-1/fact-runs");
      expect(body).toMatchObject({ review_batch_id: "batch-1", coding_source: "MATTER_METADATA", reviewer_coding_id: null, use_batch_api: false });
      expect(body.idempotency_key).toEqual(expect.any(String));
    });
  });

  it("invalidates an estimate when the selected reviewer changes", async () => {
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    await choose(user, "Run fact development", "Ready batch", "Responsive · 10 docs");
    await choose(user, "Run fact development", "Issue coding from", "One reviewer's batch coding");
    await choose(user, "Run fact development", "Reviewer", "Alice Reviewer");
    await user.click(screen.getByRole("button", { name: "Estimate" }));
    expect(await screen.findByLabelText("Run estimate")).toBeInTheDocument();

    await choose(user, "Run fact development", "Reviewer", "Bob Reviewer");
    expect(screen.queryByLabelText("Run estimate")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Launch run/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Estimate" }));
    await waitFor(() => {
      const estimateBodies = vi.mocked(coreApi).mock.calls
        .filter(([path]) => path.endsWith("/fact-runs/estimate"))
        .map(([, init]) => JSON.parse(String(init?.body)));
      expect(estimateBodies.at(-1)).toMatchObject({
        coding_source: "REVIEWER_CODING",
        reviewer_coding_id: "coding-bob",
      });
    });
  });

  it("retries a failed launch with the same idempotency key", async () => {
    let attempts = 0;
    const base = vi.mocked(coreApi).getMockImplementation()!;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/analysis-tasks/task-1/fact-runs" && init?.method === "POST") {
        attempts += 1;
        if (attempts === 1) throw new Error("Network error");
      }
      return base(path, init);
    });
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    await choose(user, "Run fact development", "Ready batch", "Responsive · 10 docs");
    await user.click(screen.getByRole("button", { name: "Estimate" }));
    const launch = await screen.findByRole("button", { name: "Launch run for 4 documents" });
    await user.click(launch);
    await waitFor(() => expect(attempts).toBe(1));
    await user.click(screen.getByRole("button", { name: "Launch run for 4 documents" }));
    await waitFor(() => expect(attempts).toBe(2));
    const keys = vi.mocked(coreApi).mock.calls
      .filter(([path, init]) => path === "/v1/matters/matter-1/analysis-tasks/task-1/fact-runs" && init?.method === "POST")
      .map(([, init]) => JSON.parse(String(init?.body)).idempotency_key);
    expect(keys[0]).toEqual(expect.any(String));
    expect(keys[1]).toBe(keys[0]);
  });

  it("launches through the Batch API when the model supports it and labels batch runs", async () => {
    batchApiAvailable = true;
    runs = [run({ configuration_snapshot: { use_batch_api: true }, report: { counts: { facts_created: 1, batch_fallbacks: 2 } } })];
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    expect(await screen.findByText("Batch API")).toBeInTheDocument();
    expect(screen.getByText("2 batch results retried in real time")).toBeInTheDocument();
    await choose(user, "Run fact development", "Ready batch", "Responsive · 10 docs");
    await user.click(screen.getByRole("button", { name: "Estimate" }));
    await user.click(await screen.findByRole("checkbox", { name: /Use the Batch API/ }));
    await user.click(screen.getByRole("button", { name: "Launch run for 4 documents" }));
    await waitFor(() => expect(bodyOf((path) => path === "/v1/matters/matter-1/analysis-tasks/task-1/fact-runs")).toMatchObject({ use_batch_api: true }));
  });

  it("summarises finished runs and resumes failed ones", async () => {
    runs = [run(), run({ id: "run-failed", status: "FAILED", failure_retriable: true, failure_summary: "Worker lost", report: {}, execution: { available_actions: ["RETRY"] } })];
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    expect(await screen.findByText("4 facts proposed")).toBeInTheDocument();
    expect(screen.getByText("3 entities to review")).toBeInTheDocument();
    expect(screen.getByText("Outcomes for run-1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review working facts" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts?ff_status=WORKING");
    expect(screen.getByText("Worker lost")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(vi.mocked(coreApi).mock.calls.some(([path]) => path === "/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/run-failed/retry")).toBe(true));
  });

  it("counts issue matches for a batch without starting a run", async () => {
    const user = userEvent.setup();
    renderWorkspace({ review_guidance_pin_stale: false });
    await choose(user, "Check issue matches", "Ready batch", "Responsive · 10 docs");
    await user.click(screen.getByRole("button", { name: "Count matches" }));
    expect(await screen.findByText("8 of 10 documents match at least one issue.")).toBeInTheDocument();
  });
});
