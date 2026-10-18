import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkspaceProvider, useWorkspace } from "@/components/workspace-context";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "/app/clients" }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.pathname = "/app/clients";
});

function mockApi() {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/auth/me") return { id: "user-1", display_name: "Admin", email: "admin@example.com", tenant_id: "tenant-root", is_superuser: true } as never;
    if (path === "/v1/tenants") return [{ id: "tenant-root", name: "Root" }, { id: "tenant-1", name: "Tenant One" }] as never;
    if (path === "/v1/tenants/tenant-root") return { id: "tenant-root", name: "Root" } as never;
    if (path === "/v1/tenants/tenant-1") return { id: "tenant-1", name: "Tenant One" } as never;
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path.startsWith("/v1/authorization/effective?resource_type=TENANT")) return { permission_keys: [] } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

function TenantProbe() {
  const { tenant, selectedTenantId, tenantFromRoute } = useWorkspace();
  return <p>{tenant.name} · {selectedTenantId} · {tenantFromRoute ? "from route" : "selected"}</p>;
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const tree = () => (
    <QueryClientProvider client={client}>
      <WorkspaceProvider><TenantProbe /></WorkspaceProvider>
    </QueryClientProvider>
  );
  const view = render(tree());
  return { rerender: () => view.rerender(tree()) };
}

describe("WorkspaceProvider tenant", () => {
  it("uses the signed-in user's tenant outside client routes", async () => {
    mockApi();
    renderWorkspace();

    expect(await screen.findByText("Root · tenant-root · selected")).toBeInTheDocument();
    expect(coreApi).not.toHaveBeenCalledWith("/v1/clients/client-1");
  });

  it("uses the client's tenant when a matter URL is opened directly", async () => {
    mockApi();
    navigation.pathname = "/app/clients/client-1/matters/matter-1";
    renderWorkspace();

    expect(await screen.findByText("Tenant One · tenant-1 · from route")).toBeInTheDocument();
    expect(screen.queryByText(/Root/)).not.toBeInTheDocument();
  });

  it("stays in the client's tenant after leaving the client", async () => {
    mockApi();
    navigation.pathname = "/app/clients/client-1";
    const view = renderWorkspace();
    expect(await screen.findByText("Tenant One · tenant-1 · from route")).toBeInTheDocument();

    navigation.pathname = "/app/clients";
    view.rerender();
    expect(await screen.findByText("Tenant One · tenant-1 · selected")).toBeInTheDocument();
  });
});
