import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuditLogView } from "@/components/views/audit-log-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/workspace-context", () => ({
  useWorkspace: () => ({
    user: {
      id: "admin-1",
      tenant_id: "tenant-1",
      display_name: "Tenant Admin",
      email: "admin@example.com",
      status: "ACTIVE",
      tenant_role: "ADMIN",
      is_superuser: false,
      created_at: "2026-10-01T00:00:00Z",
    },
    tenant: { id: "tenant-1", name: "Tenant One", slug: "tenant-one" },
    selectedTenantId: "tenant-1",
  }),
}));

function renderView() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><AuditLogView /></QueryClientProvider>);
}

const record = {
  id: "audit-1",
  tenant_id: "tenant-1",
  actor_user_id: "admin-1",
  actor_display_name: "Tenant Admin",
  actor_email: "admin@example.com",
  action: "matter.created",
  target_type: "matter",
  target_id: "matter-1",
  details: { client_id: "client-1" },
  created_at: "2026-10-08T18:30:00Z",
};

beforeEach(() => {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/tenants/tenant-1/users") {
      return [{
        id: "admin-1",
        tenant_id: "tenant-1",
        display_name: "Tenant Admin",
        email: "admin@example.com",
        status: "ACTIVE",
        tenant_role: "ADMIN",
        is_superuser: false,
        created_at: "2026-10-01T00:00:00Z",
      }] as never;
    }
    if (path.startsWith("/v1/tenants/tenant-1/audit-records")) {
      return { items: [record], next_cursor: "older-cursor" } as never;
    }
    throw new Error(`Unexpected request: ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AuditLogView", () => {
  it("loads the newest 50 actions and opens read-only details", async () => {
    const user = userEvent.setup();
    renderView();

    expect(await screen.findByText("Matter Created")).toBeInTheDocument();
    expect(coreApi).toHaveBeenCalledWith("/v1/tenants/tenant-1/audit-records?limit=50");
    expect(screen.getAllByText("Tenant Admin").length).toBeGreaterThan(0);

    await user.click(screen.getByText("Matter Created"));
    expect(await screen.findByRole("dialog")).toHaveTextContent("client-1");
    expect(screen.getByRole("dialog")).toHaveTextContent("matter.created");
  });

  it("applies date, user, and action filters and resets to the newest page", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("Matter Created");

    fireEvent.change(screen.getByLabelText("From date"), { target: { value: "2026-10-01" } });
    fireEvent.change(screen.getByLabelText("To date"), { target: { value: "2026-10-08" } });
    await user.click(screen.getByRole("combobox", { name: "User" }));
    await user.click(screen.getByRole("option", { name: "System" }));
    await user.type(screen.getByLabelText("Action"), "classification.");
    await user.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const call = vi.mocked(coreApi).mock.calls.find(([path]) => (
        path.startsWith("/v1/tenants/tenant-1/audit-records?")
        && path.includes("action_prefix=classification.")
      ));
      expect(call).toBeDefined();
      const path = call?.[0] ?? "";
      expect(path).toContain("created_from=");
      expect(path).toContain("created_to=");
      expect(path).toContain("system_actor=true");
      expect(path).not.toContain("cursor=");
    });
  });

  it("uses the opaque cursor only when loading older actions", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("Matter Created");

    await user.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/tenants/tenant-1/audit-records?limit=50&cursor=older-cursor",
    ));
  });
});
