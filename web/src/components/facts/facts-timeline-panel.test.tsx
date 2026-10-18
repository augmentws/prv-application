import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FactsTimelinePanel } from "@/components/facts/facts-timeline-panel";
import { coreApi } from "@/lib/api-client";

const replace = vi.fn();
let currentParams = new URLSearchParams("tab=facts&facts=timeline");

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/app/clients/client-1/matters/matter-1",
  useSearchParams: () => currentParams,
}));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});
vi.mock("@/components/date-histogram", () => ({
  DateHistogram: ({ buckets, onSelect, unit }: { buckets: unknown[]; onSelect: (range: { from: string; to: string }) => void; unit: { plural: string } }) => (
    <button type="button" onClick={() => onSelect({ from: "2021-01-01", to: "2021-12-31" })}>{buckets.length} {unit.plural} buckets</button>
  ),
}));

function fact(overrides: Record<string, unknown> = {}) {
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
    evidence_count: 2,
    document_count: 1,
    pending_suggestion_count: 0,
    merged_into_fact_id: null,
    confirmed_by_user_id: "user-1",
    confirmed_at: "2026-10-01T12:00:00Z",
    created_by_user_id: null,
    origin_workflow_run_id: null,
    current_revision: 1,
    lock_version: 4,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-02T12:00:00Z",
    ...overrides,
  };
}

let searchTotal = 3;

function renderPanel(canExport = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FactsTimelinePanel clientId="client-1" matterId="matter-1" canExport={canExport} />
    </QueryClientProvider>,
  );
}

function searchBodies() {
  return vi.mocked(coreApi).mock.calls.filter(([path]) => path === "/v1/matters/matter-1/facts/search").map(([, init]) => JSON.parse(String(init?.body)));
}

beforeEach(() => {
  currentParams = new URLSearchParams("tab=facts&facts=timeline");
  searchTotal = 3;
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/matters/matter-1/facts/issue-catalog") return [{ matter_analysis_task_id: "task-1", task_name: "Facts", issue_key: "pricing", name: "Pricing decisions", description: null }] as never;
    if (path === "/v1/matters/matter-1/facts/date-histogram") return { interval: "YEAR", buckets: [{ start: "2020-01-01", count: 1 }, { start: "2021-01-01", count: 1 }], undated_count: 1 } as never;
    if (path === "/v1/matters/matter-1/facts/search") {
      const body = JSON.parse(String(init?.body));
      if (body.offset > 0) return { items: [fact({ id: "fact-4", summary: "A later page.", event_date_start: null, date_precision: "UNKNOWN" })], total: searchTotal } as never;
      return {
        items: [
          fact({ id: "fact-0", summary: "The 2020 plan was drafted.", event_date_start: "2020-01-01", date_precision: "YEAR" }),
          fact(),
          fact({ id: "fact-2", summary: "Pricing memo circulated.", status: "WORKING", event_date_start: "2021-03-20", confirmed_by_user_id: null }),
        ],
        total: searchTotal,
      } as never;
    }
    throw new Error(`Unexpected request ${init?.method ?? "GET"} ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FactsTimelinePanel", () => {
  it("reads confirmed facts oldest first, grouped by year and month, marking working facts", async () => {
    renderPanel();
    const year2021 = await screen.findByRole("region", { name: "2021" });
    expect(within(year2021).getByRole("link", { name: "Jane Smith approved a 5% price increase." })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts/fact-1");
    expect(within(year2021).getByText("Unconfirmed")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "2020" })).getByText("2020, month unknown")).toBeInTheDocument();
    expect(screen.getAllByText("Pricing decisions").length).toBeGreaterThan(0);
    expect(screen.getByText("2 facts buckets")).toBeInTheDocument();
    expect(screen.getByText(/1 undated fact is listed at the end/)).toBeInTheDocument();
    expect(searchBodies()[0]).toEqual({ filters: { statuses: ["CONFIRMED"] }, sort: "DATE_ASC", limit: 100, offset: 0 });
  });

  it("includes working facts and narrows dates through the URL", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole("region", { name: "2021" });
    await user.click(screen.getByRole("checkbox", { name: "Include unconfirmed" }));
    expect(replace).toHaveBeenLastCalledWith("/app/clients/client-1/matters/matter-1?tab=facts&facts=timeline&fworking=1", { scroll: false });
    await user.click(screen.getByRole("button", { name: "2 facts buckets" }));
    expect(replace).toHaveBeenLastCalledWith("/app/clients/client-1/matters/matter-1?tab=facts&facts=timeline&ffrom=2021-01-01&fto=2021-12-31", { scroll: false });
  });

  it("asks for confirmed and working facts when unconfirmed facts are included", async () => {
    currentParams = new URLSearchParams("tab=facts&facts=timeline&fworking=1&ff_status=REJECTED");
    renderPanel();
    await screen.findByRole("region", { name: "2021" });
    expect(searchBodies()[0].filters).toEqual({ statuses: ["CONFIRMED", "WORKING"] });
  });

  it("loads more facts on request and lists undated facts last", async () => {
    searchTotal = 4;
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    const undated = await screen.findByRole("region", { name: "Undated" });
    expect(within(undated).getByText("A later page.")).toBeInTheDocument();
    expect(searchBodies().at(-1).offset).toBe(3);
    expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
  });

  it("offers export only with export permission", async () => {
    renderPanel(false);
    await screen.findByRole("region", { name: "2021" });
    expect(screen.queryByRole("button", { name: "Export CSV" })).not.toBeInTheDocument();
  });
});
