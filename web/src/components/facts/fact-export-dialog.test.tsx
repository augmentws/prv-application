import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FactExportDialog } from "@/components/facts/fact-export-dialog";
import { ApiError, coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});

function exportRead(overrides: Record<string, unknown> = {}) {
  return {
    id: "export-1",
    matter_id: "matter-1",
    filter_snapshot: {},
    csv_policy_snapshot: {},
    requested_filename: "timeline",
    fact_count: 12,
    evidence_count: 30,
    facts_filename: "timeline-facts-as-of-20261016T120000Z.csv",
    facts_byte_length: 100,
    evidence_filename: "timeline-evidence-as-of-20261016T120000Z.csv",
    evidence_byte_length: 200,
    snapshot_started_at: "2026-10-16T12:00:00Z",
    expires_at: "2999-01-01T00:00:00Z",
    created_by_user_id: "user-1",
    created_at: "2026-10-16T12:00:00Z",
    ...overrides,
  };
}

let recent: unknown[] = [];

function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FactExportDialog matterId="matter-1" filters={{ statuses: ["CONFIRMED"], issue_keys: ["pricing"] }} factCount={12} defaultFilename="timeline" />
    </QueryClientProvider>,
  );
}

function createBodies() {
  return vi.mocked(coreApi).mock.calls.filter(([path, init]) => path === "/v1/matters/matter-1/fact-exports" && init?.method === "POST").map(([, init]) => JSON.parse(String(init?.body)));
}

beforeEach(() => {
  recent = [exportRead({ id: "export-0", requested_filename: "older", expires_at: "2000-01-01T00:00:00Z" })];
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path.startsWith("/v1/matters/matter-1/fact-exports?limit=")) return recent as never;
    if (path === "/v1/matters/matter-1/fact-exports" && init?.method === "POST") return exportRead() as never;
    throw new Error(`Unexpected request ${init?.method ?? "GET"} ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FactExportDialog", () => {
  it("writes the export for the current filters and offers both downloads", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(screen.getByText(/Export the 12 facts matched by the current filters/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create export" }));
    expect(await screen.findByText(/Exported 12 facts and 30 quotes/)).toBeInTheDocument();
    const body = createBodies()[0];
    expect(body).toMatchObject({ filters: { statuses: ["CONFIRMED"], issue_keys: ["pricing"] }, requested_filename: "timeline", spreadsheet_safe: true });
    expect(body.idempotency_key).toEqual(expect.any(String));
    expect(screen.getByRole("link", { name: /Download facts CSV/ })).toHaveAttribute("href", "/api/core/v1/matters/matter-1/fact-exports/export-1/download?file=facts");
    expect(screen.getByRole("link", { name: /Download evidence CSV/ })).toHaveAttribute("href", "/api/core/v1/matters/matter-1/fact-exports/export-1/download?file=evidence");
  });

  it("lists recent exports and marks expired ones", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    expect(await screen.findByText("older")).toBeInTheDocument();
    expect(screen.getByText("Expired")).toBeInTheDocument();
  });

  it("reuses the key when retrying the same export and changes it when the options change", async () => {
    let attempts = 0;
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path.startsWith("/v1/matters/matter-1/fact-exports?limit=")) return [] as never;
      attempts += 1;
      if (attempts < 3) throw new ApiError("The export files could not be stored. Try again.", 503);
      return exportRead() as never;
    });
    const user = userEvent.setup();
    renderDialog();
    await user.click(screen.getByRole("button", { name: "Export CSV" }));
    await user.click(screen.getByRole("button", { name: "Create export" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be stored");
    await user.click(screen.getByRole("button", { name: "Create export" }));
    await waitFor(() => expect(createBodies()).toHaveLength(2));
    await user.click(screen.getByRole("checkbox", { name: /Protect against spreadsheet formulas/ }));
    await user.click(screen.getByRole("button", { name: "Create export" }));
    expect(await screen.findByText(/Exported 12 facts/)).toBeInTheDocument();
    const [first, retry, changed] = createBodies();
    expect(retry.idempotency_key).toBe(first.idempotency_key);
    expect(changed.spreadsheet_safe).toBe(false);
    expect(changed.idempotency_key).not.toBe(first.idempotency_key);
  });
});
