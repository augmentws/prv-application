import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { FactDetail } from "@/components/facts/fact-detail";
import { ApiError, coreApi } from "@/lib/api-client";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock("@/components/document-viewer-dialog", () => ({
  DocumentViewerSurface: ({ highlightTerms = [] }: { highlightTerms?: string[] }) => <div data-testid="viewer">{highlightTerms.join("|")}</div>,
}));

const evidence = {
  id: "evidence-1",
  matter_document_id: "document-1",
  control_number: "ENRON-0001",
  source_artifact_id: "artifact-1",
  source_content_hash: "a".repeat(64),
  paragraph_map_version: "paragraph-v1",
  paragraph_id: "¶1",
  char_start: 0,
  char_end: 41,
  quote: "Jane Smith approved the 5% price increase",
  stance: "SUPPORTS",
  status: "ACTIVE",
  added_by_user_id: null,
  workflow_run_id: "run-1",
  created_at: "2026-10-01T12:00:00Z",
  removed_at: null,
};

function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: "fact-1",
    matter_id: "matter-1",
    status: "CONFIRMED",
    summary: "Jane Smith approved a 5% price increase.",
    event_date_start: "2021-03-03",
    event_date_end: null,
    date_precision: "DAY",
    date_basis: "STATED_IN_TEXT",
    owner: { entity_id: "entity-1", canonical_entity_id: "entity-1", display_name: "Jane Smith", canonical_display_name: "Jane Smith", entity_type: "PERSON", role: null },
    participants: [],
    issues: [{ matter_analysis_task_id: "task-1", issue_key: "pricing" }],
    key_fact: false,
    has_contradiction: false,
    evidence_count: 1,
    document_count: 1,
    pending_suggestion_count: 1,
    merged_into_fact_id: null,
    confirmed_by_user_id: "user-1",
    confirmed_at: "2026-10-02T12:00:00Z",
    created_by_user_id: null,
    origin_workflow_run_id: "run-1",
    current_revision: 1,
    lock_version: 7,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-02T12:00:00Z",
    evidence: [evidence],
    revisions: [],
    events: [{ id: "event-1", event_type: "CONFIRMED", payload: {}, actor_type: "HUMAN", actor_user_id: "user-1", workflow_run_id: null, created_at: "2026-10-02T12:00:00Z" }],
    ...overrides,
  };
}

const revision = {
  id: "suggestion-1",
  fact_id: "fact-1",
  kind: "REVISION",
  status: "PENDING",
  base_revision: 1,
  changes: { summary: "Jane Smith approved a 5% increase effective March 3, 2021." },
  evidence: [evidence],
  target_fact_id: null,
  rationale: "The later email gives the effective date.",
  created_by_workflow_run_id: "run-1",
  resolved_by_user_id: null,
  resolved_at: null,
  resolution_note: null,
  created_at: "2026-10-03T12:00:00Z",
};

let suggestions: unknown[] = [revision];
let permissionKeys = ["fact.view", "fact.review", "entity.view"];

function renderDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FactDetail clientId="client-1" matterId="matter-1" factId="fact-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  suggestions = [revision];
  permissionKeys = ["fact.view", "fact.review", "entity.view"];
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    const method = init?.method ?? "GET";
    if (path === "/v1/clients/client-1") return { id: "client-1", name: "Acme" } as never;
    if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Pricing matter" } as never;
    if (path.startsWith("/v1/authorization/effective")) return { permission_keys: permissionKeys } as never;
    if (path === "/v1/matters/matter-1/facts/issue-catalog") return [{ matter_analysis_task_id: "task-1", task_name: "Facts", issue_key: "pricing", name: "Pricing decisions", description: null }] as never;
    if (path.startsWith("/v1/matters/matter-1/entities")) return { items: [{ id: "entity-1", display_name: "Jane Smith", status: "CONFIRMED" }], total: 1 } as never;
    if (path === "/v1/matters/matter-1/facts/fact-1/suggestions") return suggestions as never;
    if (path === "/v1/matters/matter-1/facts/fact-2") return detail({ id: "fact-2", summary: "Jane approved prices.", lock_version: 3, status: "WORKING" }) as never;
    if (path === "/v1/matters/matter-1/facts/fact-1" && method === "GET") return detail() as never;
    if (path === "/v1/matters/matter-1/facts/fact-1" && method === "PATCH") return detail({ summary: JSON.parse(String(init?.body)).summary, lock_version: 8 }) as never;
    if (path.includes("/suggestions/") || path.endsWith("/reject")) return detail({ lock_version: 8 }) as never;
    if (path === "/v1/matters/matter-1/documents/document-1/collection-item") return { id: "item-1", original_filename: "1.eml", email: { subject: "Prices" } } as never;
    throw new Error(`Unexpected request ${method} ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FactDetail", () => {
  it("saves only the changed fields with the loaded version", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const summary = screen.getByLabelText("Summary");
    await user.clear(summary);
    await user.type(summary, "A corrected summary.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const patch = vi.mocked(coreApi).mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(JSON.parse(String(patch?.[1]?.body))).toEqual({ lock_version: 7, summary: "A corrected summary." });
    });
  });

  it("explains a conflicting edit and reloads the fact", async () => {
    const user = userEvent.setup();
    const base = vi.mocked(coreApi).getMockImplementation();
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (init?.method === "PATCH") throw new ApiError("Fact changed since it was loaded (version 8, expected 7)", 409);
      return base!(path, init);
    });
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    await user.type(screen.getByLabelText("Summary"), " More.");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("The fact was reloaded")));
    const reads = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path === "/v1/matters/matter-1/facts/fact-1" && (init?.method ?? "GET") === "GET");
    expect(reads.length).toBeGreaterThan(1);
  });

  it("shows a revision suggestion as a field comparison and applies it", async () => {
    const user = userEvent.setup();
    renderDetail();
    expect(await screen.findByText("Jane Smith approved a 5% increase effective March 3, 2021.")).toBeInTheDocument();
    expect(screen.getByText(/later email gives the effective date/)).toBeInTheDocument();
    expect(screen.getByText(/Accepting also adds 1 supporting quote/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const accept = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/matters/matter-1/facts/fact-1/suggestions/suggestion-1/accept");
      expect(JSON.parse(String(accept?.[1]?.body))).toEqual({ lock_version: 7 });
    });
  });

  it("merges a suggested duplicate using both facts' versions", async () => {
    suggestions = [{ ...revision, id: "suggestion-2", kind: "MERGE", changes: {}, evidence: [], target_fact_id: "fact-2", rationale: null }];
    const user = userEvent.setup();
    renderDetail();
    expect(await screen.findByRole("link", { name: "Jane approved prices." })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts/fact-2");
    await user.click(screen.getByRole("button", { name: "Merge" }));

    await waitFor(() => {
      const accept = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/matters/matter-1/facts/fact-1/suggestions/suggestion-2/accept");
      expect(JSON.parse(String(accept?.[1]?.body))).toEqual({ lock_version: 7, target_lock_version: 3 });
    });
  });

  it("keeps the only quote of a confirmed fact and opens quotes highlighted in the document", async () => {
    const user = userEvent.setup();
    renderDetail();
    expect(await screen.findByText("“Jane Smith approved the 5% price increase”")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove quote/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "View in document" }));
    expect(await screen.findByTestId("viewer")).toHaveTextContent("Jane Smith approved the 5% price increase");
    expect(screen.getByRole("link", { name: "Open in Search & Review" })).toHaveAttribute("href", "/review/matters/matter-1?document=document-1");
  });

  it("shows no review actions to viewers", async () => {
    permissionKeys = ["fact.view"];
    renderDetail();
    expect(await screen.findByText("Agent suggestions")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();
  });
});
