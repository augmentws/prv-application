import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/app-shell";

vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/app/clients",
  useRouter: () => ({ replace: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("@/components/workspace-context", () => ({
  WorkspaceProvider: ({ children }: { children: ReactNode }) => children,
  useWorkspace: () => ({
    user: {
      id: "user-1",
      display_name: "Test User",
      email: "test@example.com",
      tenant_id: "tenant-1",
      is_superuser: false,
    },
    tenant: { id: "tenant-1", name: "Tenant One", slug: "tenant-one" },
    tenants: [{ id: "tenant-1", name: "Tenant One", slug: "tenant-one" }],
    selectedTenantId: "tenant-1",
    setSelectedTenantId: vi.fn(),
  }),
}));

afterEach(() => cleanup());

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
});
