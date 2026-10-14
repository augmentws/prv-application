import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AccessControlView } from "@/components/views/access-control-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/workspace-context", () => ({
  useWorkspace: () => ({
    user: { id: "admin-1", tenant_id: "tenant-1", tenant_role: "ADMIN", is_superuser: false },
    tenant: { id: "tenant-1", name: "Tenant One", slug: "tenant-one" },
    selectedTenantId: "tenant-1",
  }),
}));

const systemRole = {
  id: "role-admin",
  tenant_id: null,
  key: "TENANT_ADMINISTRATOR",
  display_name: "Tenant Administrator",
  description: "Full tenant access",
  role_kind: "SYSTEM",
  status: "ACTIVE",
  row_version: 1,
  permission_keys: ["tenant.manage_roles"],
  grant_count: 1,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

function renderView() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><AccessControlView /></QueryClientProvider>);
}

beforeEach(() => {
  vi.mocked(coreApi).mockImplementation(async (path, request) => {
    if (path === "/v1/permissions") return [{ id: "permission-1", key: "matter.view", display_name: "View", description: "View matters", category: "Matter", resource_type: "MATTER", dangerous: false, status: "ACTIVE" }] as never;
    if (path === "/v1/tenants/tenant-1/roles" && request?.method === "POST") return { ...systemRole, id: "custom-role", tenant_id: "tenant-1", role_kind: "CUSTOM", key: "CASE_ANALYST", display_name: "Case analyst" } as never;
    if (path === "/v1/tenants/tenant-1/roles") return [systemRole] as never;
    if (path === "/v1/tenants/tenant-1/access-groups") return [{ id: "group-1", tenant_id: "tenant-1", key: "review-team", display_name: "Review team", description: null, group_kind: "LOCAL", status: "ACTIVE", row_version: 1, member_ids: ["user-1"], grant_count: 1, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" }] as never;
    if (path === "/v1/tenants/tenant-1/authorization-grants") return [{ id: "grant-1", tenant_id: "tenant-1", subject_type: "ACCESS_GROUP", subject_id: "group-1", subject_display_name: "Review team", role_id: "role-admin", role_key: "TENANT_ADMINISTRATOR", role_display_name: "Tenant Administrator", scope_type: "TENANT", scope_id: "tenant-1", scope_display_name: "Tenant One", row_version: 1, created_by_user_id: "admin-1", updated_by_user_id: "admin-1", created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z" }] as never;
    if (path === "/v1/tenants/tenant-1/users" && request?.method === "POST") return { id: "user-2", tenant_id: "tenant-1", display_name: "Reviewer Two", email: "reviewer2@example.com", status: "ACTIVE", tenant_role: "USER", is_superuser: false, created_at: "2026-10-02T00:00:00Z" } as never;
    if (path === "/v1/tenants/tenant-1/users") return [{ id: "user-1", tenant_id: "tenant-1", display_name: "Reviewer One", email: "reviewer@example.com", status: "ACTIVE", tenant_role: "USER", is_superuser: false, created_at: "2026-10-01T00:00:00Z" }] as never;
    if (path === "/v1/tenants/tenant-1/clients") return [] as never;
    throw new Error(`Unexpected request: ${path}`);
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AccessControlView", () => {
  it("creates users from the Users section", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("Reviewer One");
    await user.click(screen.getByRole("button", { name: "Add user" }));
    await user.type(screen.getByLabelText("Name"), "Reviewer Two");
    await user.type(screen.getByLabelText("Email"), "reviewer2@example.com");
    await user.type(screen.getByLabelText("Temporary password"), "temporary-pass-123");
    await user.click(screen.getByRole("button", { name: "Create user" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/tenants/tenant-1/users",
      expect.objectContaining({ method: "POST" }),
    ));
  });

  it("shows roles, groups, and scoped assignments", async () => {
    const user = userEvent.setup();
    renderView();
    expect(await screen.findByText("Reviewer One")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Roles" }));
    expect(await screen.findByText("Tenant Administrator")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Groups" }));
    expect(await screen.findByText("Review team")).toBeInTheDocument();
    expect(screen.getByText("Reviewer One")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Assignments" }));
    expect((await screen.findAllByText("Tenant One")).length).toBeGreaterThan(1);
    expect(screen.getByText("Group")).toBeInTheDocument();
  });

  it("creates a custom role with selected permissions", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("Reviewer One");
    await user.click(screen.getByRole("tab", { name: "Roles" }));
    await screen.findByText("Tenant Administrator");
    await user.click(screen.getByRole("button", { name: "New role" }));
    await user.type(screen.getByLabelText("Name"), "Case analyst");
    await user.type(screen.getByLabelText("Key"), "CASE_ANALYST");
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "Create role" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/tenants/tenant-1/roles",
      expect.objectContaining({ method: "POST" }),
    ));
    const request = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/roles") && init?.method === "POST");
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({
      key: "CASE_ANALYST",
      display_name: "Case analyst",
      permission_keys: ["matter.view"],
    });
  });
});
