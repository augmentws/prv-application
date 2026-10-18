import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterSectionMenu } from "@/components/matter/matter-section-menu";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const base = "/app/clients/client-1/matters/matter-1";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mockPermissions(permissionKeys: string[]) {
  vi.mocked(coreApi).mockImplementation(async (path) => {
    if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { permission_keys: permissionKeys } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

function renderMenu(current: "search-and-review" | "batches") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MatterSectionMenu clientId="client-1" matterId="matter-1" matterName="Trec-JBush" clientName="TREC" current={current} /></QueryClientProvider>);
}

describe("MatterSectionMenu", () => {
  it("names the matter and links every visible section from the review workspace", async () => {
    mockPermissions(["fact.view", "entity.view"]);
    const user = userEvent.setup();
    renderMenu("search-and-review");

    expect(screen.getByText("TREC")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Matter: Trec-JBush. Open matter menu" }));

    expect(await screen.findByRole("menuitem", { name: "Search and review" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("menuitem", { name: "Overview" })).toHaveAttribute("href", base);
    expect(screen.getByRole("menuitem", { name: "Facts" })).toHaveAttribute("href", `${base}/facts`);
    expect(screen.getByRole("menuitem", { name: "Jobs" })).toHaveAttribute("href", `${base}/jobs/imports`);
    expect(screen.getByRole("menuitem", { name: "Batches" })).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("menuitem")).toHaveLength(12);
  });

  it("hides sections the user cannot view and marks batch review under Batches", async () => {
    mockPermissions([]);
    const user = userEvent.setup();
    renderMenu("batches");

    await user.click(screen.getByRole("button", { name: "Matter: Trec-JBush. Open matter menu" }));

    expect(await screen.findByRole("menuitem", { name: "Batches" })).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("menuitem", { name: "Facts" })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Entities" })).not.toBeInTheDocument();
    expect(screen.queryByText("Findings")).not.toBeInTheDocument();
  });
});
