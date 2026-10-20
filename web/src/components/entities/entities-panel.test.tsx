import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EntitiesPanel, type EntitiesTab } from "@/components/entities/entities-panel";
import { coreApi } from "@/lib/api-client";

const replace = vi.fn();
let currentParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  usePathname: () => "/app/clients/client-1/matters/matter-1/entities",
  useSearchParams: () => currentParams,
}));
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
    custodian_id: null,
    description: null,
    status: "PROPOSED",
    lock_version: 2,
    created_by_user_id: null,
    created_by_workflow_run_id: "run-1",
    created_at: "2026-10-01T12:00:00Z",
    updated_at: "2026-10-01T12:00:00Z",
    variants: [{ id: "variant-1", display_name: "J. Smith", email: "jsmith@gmail.com", lock_version: 1, created_at: "2026-10-01T12:00:00Z" }],
    fact_count: 3,
    ...overrides,
  };
}

function renderPanel(tab: EntitiesTab, canManage = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <EntitiesPanel clientId="client-1" matterId="matter-1" tab={tab} canManage={canManage} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  currentParams = new URLSearchParams();
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path.includes("entity_type=ORGANIZATION&limit=500")) return { items: [entity({ id: "org-1", entity_type: "ORGANIZATION", display_name: "Acme Corp", variants: [] })], total: 1 } as never;
    if (path.startsWith("/v1/matters/matter-1/entities?")) return { items: [entity()], total: 1 } as never;
    if (path === "/v1/matters/matter-1/entities" && init?.method === "POST") return entity({ display_name: null, email: "unknown@example.com" }) as never;
    if (path.endsWith("/confirm") || path.endsWith("/reject")) return entity({ status: "CONFIRMED" }) as never;
    throw new Error(`Unexpected request ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function listRequest() {
  const call = vi.mocked(coreApi).mock.calls.find(([path]) => path.startsWith("/v1/matters/matter-1/entities?status="));
  return new URLSearchParams(String(call?.[0]).split("?")[1]);
}

describe("EntitiesPanel", () => {
  it("lists confirmed entities in the directory with variants, organization and fact counts", async () => {
    renderPanel("directory");
    expect(await screen.findByRole("link", { name: "Jane Smith" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/entities/entity-1");
    expect(screen.getByText("Also: jsmith@gmail.com")).toBeInTheDocument();
    expect(await screen.findByText("General Counsel, Acme Corp")).toBeInTheDocument();
    expect(listRequest().get("status")).toBe("CONFIRMED");
    expect(screen.queryByRole("button", { name: "Confirm Jane Smith" })).not.toBeInTheDocument();
  });

  it("puts search and type in the URL", async () => {
    const user = userEvent.setup();
    renderPanel("directory");
    await screen.findByRole("link", { name: "Jane Smith" });
    await user.type(screen.getByRole("textbox", { name: "Search entities" }), "smith");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(replace).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-1/entities?eq=smith", { scroll: false });
  });

  it("reviews proposed entities, warning before a rejection removes them from facts", async () => {
    const user = userEvent.setup();
    renderPanel("proposed");
    await screen.findByRole("link", { name: "Jane Smith" });
    expect(listRequest().get("status")).toBe("PROPOSED");

    await user.click(screen.getByRole("button", { name: "Confirm Jane Smith" }));
    await waitFor(() => {
      const confirm = vi.mocked(coreApi).mock.calls.find(([path]) => path === "/v1/matters/matter-1/entities/entity-1/confirm");
      expect(JSON.parse(String(confirm?.[1]?.body))).toEqual({ lock_version: 2 });
    });

    await user.click(screen.getByRole("button", { name: "Reject Jane Smith" }));
    expect(await screen.findByText(/removed as owner or participant from 3 facts/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(vi.mocked(coreApi).mock.calls.some(([path]) => path === "/v1/matters/matter-1/entities/entity-1/reject")).toBe(true));
  });

  it("creates an email-only entity without inventing a display name", async () => {
    const user = userEvent.setup();
    renderPanel("directory");
    await screen.findByRole("link", { name: "Jane Smith" });
    await user.click(screen.getByRole("button", { name: "New entity" }));
    await user.type(screen.getByLabelText("Email address"), "unknown@example.com");
    await user.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => {
      const call = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/entities" && init?.method === "POST");
      expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
        display_name: null,
        email: "unknown@example.com",
      });
    });
  });

  it("offers no management actions to viewers", async () => {
    renderPanel("proposed", false);
    await screen.findByRole("link", { name: "Jane Smith" });
    expect(screen.queryByRole("button", { name: "New entity" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm Jane Smith" })).not.toBeInTheDocument();
  });
});
