import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FactsSection } from "@/components/matter/sections/facts-section";
import { MATTER_BASE_PATH, renderInMatter } from "@/components/matter/test-utils";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "", push: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push, replace: vi.fn() }), useSearchParams: () => navigation.params }));
vi.mock("@/components/facts/facts-list-panel", () => ({ FactsListPanel: ({ canExport }: { canExport: boolean }) => <p>Fact list{canExport ? " with export" : ""}</p> }));
vi.mock("@/components/facts/facts-timeline-panel", () => ({ FactsTimelinePanel: ({ canExport }: { canExport: boolean }) => <p>Fact timeline{canExport ? " with export" : ""}</p> }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.params = new URLSearchParams();
  window.localStorage.clear();
});

describe("Matter facts", () => {
  function mockFactsMatter(permissionKeys: string[]) {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: permissionKeys } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
  }

  it("switches between the List and Timeline sub-tabs keeping the filters", async () => {
    mockFactsMatter(["fact.view", "fact.export"]);
    navigation.params = new URLSearchParams("ff_issue_key=pricing&fpage=2");
    navigation.pathname = `${MATTER_BASE_PATH}/facts`;
    renderInMatter(<FactsSection tab="list" />);

    expect(await screen.findByText("Fact list with export")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "List" })).toHaveAttribute("aria-selected", "true");
    await userEvent.setup().click(screen.getByRole("tab", { name: "Timeline" }));
    expect(navigation.push).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-1/facts/timeline?ff_issue_key=pricing");
  });

  it("shows the timeline without export for viewers", async () => {
    mockFactsMatter(["fact.view"]);
    navigation.pathname = `${MATTER_BASE_PATH}/facts/timeline`;
    renderInMatter(<FactsSection tab="timeline" />);

    expect(await screen.findByText("Fact timeline")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Timeline" })).toHaveAttribute("aria-selected", "true");
  });
});
