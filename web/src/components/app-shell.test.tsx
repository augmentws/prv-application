import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/app-shell";

vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const route = vi.hoisted(() => ({ pathname: "/app/clients" }));

vi.mock("next/navigation", () => ({
  usePathname: () => route.pathname,
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

const workspace = vi.hoisted(() => ({ isSuperuser: false, tenantFromRoute: false }));

vi.mock("@/components/matter/matter-sidebar-nav", () => ({
  MatterSidebarNav: ({ clientId, matterId }: { clientId: string; matterId: string }) => <nav aria-label="Matter">{clientId}/{matterId}</nav>,
}));

vi.mock("@/components/collection/collection-sidebar-nav", () => ({
  CollectionSidebarNav: ({ clientId, collectionId }: { clientId: string; collectionId: string }) => <nav aria-label="Collection">{clientId}/{collectionId}</nav>,
}));

vi.mock("@/components/workspace-context", () => ({
  WorkspaceProvider: ({ children }: { children: ReactNode }) => children,
  useWorkspace: () => ({
    user: {
      id: "user-1",
      display_name: "Test User",
      email: "test@example.com",
      tenant_id: "tenant-1",
      is_superuser: workspace.isSuperuser,
    },
    tenant: { id: "tenant-1", name: "Tenant One", slug: "tenant-one" },
    tenants: [{ id: "tenant-1", name: "Tenant One", slug: "tenant-one" }, { id: "tenant-2", name: "Tenant Two", slug: "tenant-two" }],
    selectedTenantId: "tenant-1",
    setSelectedTenantId: vi.fn(),
    tenantFromRoute: workspace.tenantFromRoute,
  }),
}));

afterEach(() => {
  cleanup();
  workspace.isSuperuser = false;
  workspace.tenantFromRoute = false;
  route.pathname = "/app/clients";
});

function renderShell() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AppShell><div>Page content</div></AppShell>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("uses the full available width when the desktop sidebar is collapsed", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <AppShell><div>Page content</div></AppShell>
      </QueryClientProvider>,
    );

    const main = document.getElementById("main-content");
    expect(main).toHaveClass("max-w-[96rem]");
    expect(main).not.toHaveClass("max-w-none");

    await user.click(screen.getAllByRole("button", { name: "Collapse sidebar" })[0]);
    expect(main).toHaveClass("max-w-none");
    expect(main).not.toHaveClass("max-w-[96rem]");

    await user.click(screen.getAllByRole("button", { name: "Expand sidebar" })[0]);
    expect(main).toHaveClass("max-w-[96rem]");
  });

  it("lets a superuser switch tenants outside client routes", () => {
    workspace.isSuperuser = true;
    renderShell();

    expect(screen.getAllByRole("combobox").length).toBeGreaterThan(0);
  });

  it("shows the route's tenant without a switcher on client and matter routes", () => {
    workspace.isSuperuser = true;
    workspace.tenantFromRoute = true;
    renderShell();

    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getAllByText("tenant-one").length).toBeGreaterThan(0);
  });

  it("shows workspace links outside a matter", () => {
    renderShell();

    expect(screen.getAllByRole("navigation", { name: "Primary" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("navigation", { name: "Matter" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Workspace menu/ })).not.toBeInTheDocument();
  });

  it("replaces workspace links with the matter menu inside a matter", async () => {
    route.pathname = "/app/clients/client-1/matters/matter-1/facts/fact-1";
    const user = userEvent.setup();
    renderShell();

    expect(screen.getAllByRole("navigation", { name: "Matter" })[0]).toHaveTextContent("client-1/matter-1");
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Tenant One. Workspace menu" })[0]);
    expect(await screen.findByRole("menuitem", { name: "Clients" })).toHaveAttribute("href", "/app/clients");
    expect(screen.getByRole("menuitem", { name: "Documentation" })).toHaveAttribute("href", "/docs");
    // The tenant card is the only way in; there is no separate Workspace button at the bottom.
    expect(screen.queryByRole("button", { name: "Workspace" })).not.toBeInTheDocument();
  });

  it("replaces workspace links with the collection menu inside a collection", async () => {
    route.pathname = "/app/clients/client-1/collections/collection-1/file-dates";
    const user = userEvent.setup();
    renderShell();

    expect(screen.getAllByRole("navigation", { name: "Collection" })[0]).toHaveTextContent("client-1/collection-1");
    expect(screen.queryByRole("navigation", { name: "Matter" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Primary" })).not.toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "Tenant One. Workspace menu" })[0]);
    expect(await screen.findByRole("menuitem", { name: "Clients" })).toHaveAttribute("href", "/app/clients");
  });

  it("keeps workspace links on the client page", () => {
    route.pathname = "/app/clients/client-1";
    renderShell();

    expect(screen.getAllByRole("navigation", { name: "Primary" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("navigation", { name: "Collection" })).not.toBeInTheDocument();
  });
});
