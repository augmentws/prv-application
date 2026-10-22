import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterSidebarNav } from "@/components/matter/matter-sidebar-nav";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "/app/clients/client-1/matters/matter-1", push: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push }) }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const base = "/app/clients/client-1/matters/matter-1";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.pathname = base;
});

function mockApi(permissionKeys: string[]) {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Trec-JBush" } as never;
    if (path === "/v1/clients/client-1/matters") return [{ id: "matter-1", name: "Trec-JBush" }, { id: "matter-2", name: "Trec-Second" }] as never;
    if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { permission_keys: permissionKeys } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

function renderNav(collapsed = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MatterSidebarNav clientId="client-1" matterId="matter-1" collapsed={collapsed} onNavigate={vi.fn()} /></QueryClientProvider>);
}

describe("MatterSidebarNav", () => {
  it("groups the matter's sections and links each to its route", async () => {
    mockApi(["fact.view", "entity.view"]);
    renderNav();

    expect(await screen.findByRole("link", { name: "Facts" })).toHaveAttribute("href", `${base}/facts`);
    expect(screen.getByRole("link", { name: "TREC" })).toHaveAttribute("href", "/app/clients/client-1");
    expect(screen.getByRole("link", { name: "Search and review" })).toHaveAttribute("href", "/review/matters/matter-1");
    expect(within(screen.getByRole("group", { name: "Setup" })).getAllByRole("link").map((link) => link.textContent)).toEqual(["Metadata fields", "Search index", "Token usage"]);
    expect(within(screen.getByRole("group", { name: "Agents and models" })).getAllByRole("link").map((link) => link.textContent)).toEqual(["Matter definition", "Analysis tasks", "Classifiers", "Jobs"]);
    expect(screen.getByRole("link", { name: "Overview" })).toHaveAttribute("aria-current", "page");
  });

  it("hides Facts and Entities without permission to view them", async () => {
    mockApi([]);
    renderNav();

    expect(await screen.findByRole("link", { name: "Batches" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Facts" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Entities" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Findings" })).not.toBeInTheDocument();
  });

  it("shows Review Programs only with its view permission", async () => {
    mockApi(["review_program.view"]);
    renderNav();

    expect(await screen.findByRole("link", { name: "Review programs" })).toHaveAttribute("href", `${base}/review-programs`);
  });

  it.each([
    [`${base}/facts/fact-1`, "Facts"],
    [`${base}/facts/timeline`, "Facts"],
    [`${base}/metadata/groups`, "Metadata fields"],
    [`${base}/topic-jobs/job-1`, "Jobs"],
    [`${base}/classification-models/model-1`, "Classifiers"],
  ])("marks the owning section current on %s", async (pathname, label) => {
    mockApi(["fact.view", "entity.view"]);
    navigation.pathname = pathname;
    renderNav();

    expect(await screen.findByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
  });

  it("switches matter and keeps the section, leaving detail pages for their list", async () => {
    mockApi(["fact.view"]);
    navigation.pathname = `${base}/facts/fact-1`;
    const user = userEvent.setup();
    renderNav();

    await user.click(await screen.findByRole("button", { name: "Matter: Trec-JBush. Switch matter" }));
    await user.click(await screen.findByRole("menuitem", { name: "Trec-Second" }));

    expect(navigation.push).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-2/facts");
  });

  it("keeps accessible names when the sidebar is collapsed", async () => {
    mockApi([]);
    renderNav(true);

    expect(await screen.findByRole("link", { name: "Batches" })).toHaveAttribute("title", "Batches");
    expect(screen.queryByText("Setup")).not.toBeInTheDocument();
  });
});
