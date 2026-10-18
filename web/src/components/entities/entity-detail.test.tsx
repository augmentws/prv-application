import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EntityDetail } from "@/components/entities/entity-detail";
import { coreApi } from "@/lib/api-client";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...actual, coreApi: vi.fn() };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function entity(overrides: Record<string, unknown> = {}) {
  return {
    id: "entity-1",
    matter_id: "matter-1",
    entity_type: "PERSON",
    display_name: "Jane Smith",
    email: "jane@acme.com",
    title: "General Counsel",
    organization_id: "org-1",
    custodian_id: "custodian-1",
    description: null,
    status: "CONFIRMED",
    lock_version: 5,
    created_by_user_id: "user-1",
    created_by_workflow_run_id: null,
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-01T12:00:00Z",
    variants: [{ id: "variant-1", display_name: "J. Smith", email: "jsmith@gmail.com", lock_version: 3, created_at: "2026-10-01T12:00:00Z" }],
    fact_count: 2,
    ...overrides,
  };
}

let permissionKeys = ["entity.view", "entity.manage", "fact.view"];

function renderDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <EntityDetail clientId="client-1" matterId="matter-1" entityId="entity-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  permissionKeys = ["entity.view", "entity.manage", "fact.view"];
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    const method = init?.method ?? "GET";
    if (path === "/v1/clients/client-1") return { id: "client-1", name: "Acme" } as never;
    if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Pricing matter" } as never;
    if (path.startsWith("/v1/authorization/effective")) return { permission_keys: permissionKeys } as never;
    if (path === "/v1/matters/matter-1/custodians") return [{ id: "custodian-1", client_id: "client-1", display_name: "Smith, Jane", email_addresses: [], external_reference: null, status: "ACTIVE", created_at: "2026-10-01T12:00:00Z" }] as never;
    if (path.includes("entity_type=ORGANIZATION&limit=500")) return { items: [entity({ id: "org-1", entity_type: "ORGANIZATION", display_name: "Acme Corp", variants: [], title: null, organization_id: null })], total: 1 } as never;
    if (path.startsWith("/v1/matters/matter-1/entities?entity_type=PERSON")) return { items: [entity(), entity({ id: "entity-2", display_name: "Jane A. Smith", variants: [] })], total: 2 } as never;
    if (path === "/v1/matters/matter-1/entities/entity-1" && method === "GET") return entity() as never;
    if (path === "/v1/matters/matter-1/facts/search") return { items: [{ id: "fact-1", summary: "Jane approved prices.", status: "CONFIRMED", date_precision: "MONTH", event_date_start: "2021-03-01", event_date_end: null }], total: 2 } as never;
    if (path.endsWith("/merge")) return entity({ id: "entity-2", display_name: "Jane A. Smith" }) as never;
    if (method !== "GET") return entity({ lock_version: 6 }) as never;
    throw new Error(`Unexpected request ${method} ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function bodyOf(path: string) {
  const call = vi.mocked(coreApi).mock.calls.find(([candidate, init]) => candidate === path && init?.body !== undefined);
  return call ? JSON.parse(String(call[1]?.body)) : undefined;
}

describe("EntityDetail", () => {
  it("shows the entity's organization, custodian, variants and facts", async () => {
    renderDetail();
    expect(await screen.findByRole("link", { name: "Acme Corp" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/entities/org-1");
    expect(await screen.findByText("Smith, Jane")).toBeInTheDocument();
    expect(screen.getByText("jsmith@gmail.com")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Jane approved prices." })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts/fact-1");
    expect(screen.getByRole("link", { name: "View all 2 in Facts" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/facts?fentity=entity-1");
    expect(bodyOf("/v1/matters/matter-1/facts/search").filters).toEqual({ entity_id: "entity-1" });
  });

  it("saves only the changed fields with the loaded version", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const title = screen.getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Chief Legal Officer");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(bodyOf("/v1/matters/matter-1/entities/entity-1")).toEqual({ lock_version: 5, title: "Chief Legal Officer" }));
  });

  it("splits off a variant and adds a new address", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Split off jsmith@gmail.com" }));
    await waitFor(() => expect(bodyOf("/v1/matters/matter-1/entities/variant-1/unmerge")).toEqual({ lock_version: 3 }));

    await user.type(screen.getByLabelText("Email address"), "jane.smith@acme.com");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(bodyOf("/v1/matters/matter-1/entities/entity-1/variants")).toEqual({ lock_version: 5, email: "jane.smith@acme.com" }));
  });

  it("merges into another person of the same type and opens the one kept", async () => {
    const user = userEvent.setup();
    renderDetail();
    await user.click(await screen.findByRole("button", { name: "Merge into…" }));
    const keep = await screen.findByRole("button", { name: "Keep this one" });
    expect(screen.getByText("Jane A. Smith")).toBeInTheDocument();
    await user.click(keep);
    await waitFor(() => expect(bodyOf("/v1/matters/matter-1/entities/entity-1/merge")).toEqual({ lock_version: 5, target_entity_id: "entity-2" }));
    expect(push).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-1/entities/entity-2");
  });

  it("is read-only without entity management", async () => {
    permissionKeys = ["entity.view"];
    renderDetail();
    expect(await screen.findByText("jsmith@gmail.com")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Split off/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Facts" })).not.toBeInTheDocument();
  });
});
