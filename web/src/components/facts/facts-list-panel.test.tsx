import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FactsListPanel } from "@/components/facts/facts-list-panel";
import { coreApi } from "@/lib/api-client";

const replace = vi.fn();
let currentParams = new URLSearchParams("tab=facts");

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/app/clients/client-1/matters/matter-1",
  useSearchParams: () => currentParams,
}));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function fact(overrides: Record<string, unknown> = {}) {
  return {
    id: "fact-1",
    matter_id: "matter-1",
    status: "WORKING",
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
    evidence_count: 2,
    document_count: 1,
    pending_suggestion_count: 1,
    merged_into_fact_id: null,
    confirmed_by_user_id: null,
    confirmed_at: null,
    created_by_user_id: null,
    origin_workflow_run_id: "run-1",
    current_revision: 1,
    lock_version: 4,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-02T12:00:00Z",
    ...overrides,
  };
}

function renderPanel(canReview = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FactsListPanel clientId="client-1" matterId="matter-1" canReview={canReview} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  currentParams = new URLSearchParams("tab=facts");
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/matters/matter-1/facts/issue-catalog") return [{ matter_analysis_task_id: "task-1", task_name: "Facts", issue_key: "pricing", name: "Pricing decisions", description: null }] as never;
    if (path === "/v1/matters/matter-1/facts/search") return { items: [fact(), fact({ id: "fact-2", summary: "Merged duplicate.", status: "MERGED", pending_suggestion_count: 0, origin_workflow_run_id: null })], total: 2 } as never;
    if (path === "/v1/matters/matter-1/facts/facets") {
      const body = JSON.parse(String(init?.body));
      return { facet: body.facet, buckets: [{ value: "WORKING", label: "WORKING", count: 7 }] } as never;
    }
    if (path.endsWith("/confirm")) return fact({ status: "CONFIRMED" }) as never;
    throw new Error(`Unexpected request ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FactsListPanel", () => {
  it("lists facts oldest first with issue names, agent and suggestion markers, and a detail link", async () => {
    renderPanel();
    const link = await screen.findByRole("link", { name: "Jane Smith approved a 5% price increase." });
    expect(link).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts/fact-1");
    expect(screen.getAllByText("Pricing decisions")).toHaveLength(2);
    expect(screen.getByText("1 suggestion")).toBeInTheDocument();
    expect(screen.getByText("2 facts")).toBeInTheDocument();
    const search = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/matters/matter-1/facts/search");
    expect(JSON.parse(String(search?.[1]?.body))).toEqual({ filters: {}, sort: "DATE_ASC", limit: 50, offset: 0 });
  });

  it("puts search and filters in the URL and asks facets for self-excluding counts", async () => {
    currentParams = new URLSearchParams("tab=facts&ff_status=WORKING&ff_issue_key=pricing");
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("2 facts");

    await user.type(screen.getByRole("textbox", { name: "Search facts" }), "approval");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(replace).toHaveBeenCalledWith(
      "/app/clients/client-1/matters/matter-1?tab=facts&fq=approval&ff_status=WORKING&ff_issue_key=pricing",
      { scroll: false },
    );

    await waitFor(() => {
      const facet = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/facts/facets" && JSON.parse(String(init?.body)).facet === "status");
      expect(JSON.parse(String(facet?.[1]?.body)).filters).toEqual({ issue_keys: ["pricing"] });
    });
    // The opened status facet lists its values with counts.
    expect(await screen.findByRole("checkbox", { name: /Working/ })).toBeChecked();
  });

  it("confirms the selected working facts with their loaded versions", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText("2 facts");
    expect(screen.getByRole("checkbox", { name: "Select fact: Merged duplicate." })).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "Select fact: Jane Smith approved a 5% price increase." }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      const confirm = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/matters/matter-1/facts/fact-1/confirm");
      expect(JSON.parse(String(confirm?.[1]?.body))).toEqual({ lock_version: 4 });
    });
  });

  it("hides review controls from viewers", async () => {
    renderPanel(false);
    await screen.findByText("2 facts");
    expect(screen.queryByRole("checkbox", { name: /Select fact/ })).not.toBeInTheDocument();
  });
});
