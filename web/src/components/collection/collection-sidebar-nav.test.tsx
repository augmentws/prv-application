import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CollectionSidebarNav } from "@/components/collection/collection-sidebar-nav";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "/app/clients/client-1/collections/collection-1", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push }) }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const base = "/app/clients/client-1/collections/collection-1";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.pathname = base;
});

function mockApi() {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path === "/v1/collections/collection-1") return { id: "collection-1", client_id: "client-1", name: "Enron mailboxes" } as never;
    if (path === "/v1/tenants/tenant-1/clients/client-1/collections") return [{ id: "collection-1", name: "Enron mailboxes" }, { id: "collection-2", name: "Shared drives" }] as never;
    if (path === "/v1/authorization/effective?resource_type=COLLECTION&resource_id=collection-1") return { permission_keys: ["collection.view"] } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

function renderNav() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><CollectionSidebarNav clientId="client-1" collectionId="collection-1" collapsed={false} onNavigate={vi.fn()} /></QueryClientProvider>);
}

describe("CollectionSidebarNav", () => {
  it("groups the collection's sections and links each to its route", async () => {
    mockApi();
    renderNav();

    // The client's name replaces the "Client" placeholder once it loads.
    expect(await screen.findByRole("link", { name: "TREC" })).toHaveAttribute("href", "/app/clients/client-1");
    expect(screen.getByRole("navigation", { name: "Collection" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("href", base);
    expect(within(screen.getByRole("group", { name: "Explore" })).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([base, `${base}/custodians`, `${base}/file-dates`]);
    expect(screen.getByRole("link", { name: "Text processing" })).toHaveAttribute("href", `${base}/processing`);
    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute("aria-current", "page");
  });

  it.each([
    [`${base}/custodians`, "Custodians"],
    [`${base}/file-dates`, "File dates"],
    [`${base}/processing/`, "Text processing"],
  ])("marks the section current on %s", async (pathname, label) => {
    mockApi();
    navigation.pathname = pathname;
    renderNav();

    expect(await screen.findByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Documents" })).not.toHaveAttribute("aria-current");
  });

  it("switches collection and keeps the section", async () => {
    mockApi();
    navigation.pathname = `${base}/file-dates`;
    const user = userEvent.setup();
    renderNav();

    await user.click(await screen.findByRole("button", { name: "Collection: Enron mailboxes. Switch collection" }));
    await user.click(await screen.findByRole("menuitem", { name: "Shared drives" }));

    expect(navigation.push).toHaveBeenCalledWith("/app/clients/client-1/collections/collection-2/file-dates");
  });
});
