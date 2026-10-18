import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BatchesSection } from "@/components/matter/sections/batches-section";
import { MATTER_BASE_PATH, renderInMatter } from "@/components/matter/test-utils";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
const navigation = vi.hoisted(() => ({ pathname: "", push: vi.fn(), params: new URLSearchParams() }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname, useRouter: () => ({ push: navigation.push, replace: vi.fn() }), useSearchParams: () => navigation.params }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  navigation.params = new URLSearchParams();
  window.localStorage.clear();
});

describe("Matter batches", () => {
  it("uses a split Open review action with Statistics in its dropdown", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", authorization_revision: 1, permission_keys: ["batch.delete"] } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/definition-assessments") return [] as never;
      if (path === "/v1/matters/matter-1/access-users?permission_key=batch.review") return [] as never;
      if (path === "/v1/matters/matter-1/review-batches") return [{
        id: "batch-1",
        matter_id: "matter-1",
        name: "Privilege validation",
        description: "First-pass privilege review",
        selection_type: "ALL_MATTER",
        selection_definition: {},
        document_count: 2500,
        coding_groups: [],
        assigned_user_id: null,
        status: "READY",
        execution: null,
        error_message: null,
        search_error_message: null,
        created_at: "2026-10-13T12:00:00Z",
      }] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1" && init?.method === "DELETE") return undefined as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    navigation.pathname = `${MATTER_BASE_PATH}/batches`;
    renderInMatter(<BatchesSection />);

    expect(await screen.findByText("Privilege validation")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open review" })).toHaveAttribute("href", "/review/matters/matter-1?batch=batch-1");
    expect(screen.queryByRole("button", { name: "Statistics" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "More actions for Privilege validation" }));
    expect(await screen.findByRole("menuitem", { name: "Delete batch" })).toBeInTheDocument();
    const statistics = await screen.findByRole("menuitem", { name: "Statistics" });
    expect(statistics).toHaveAttribute("href", "/review/matters/matter-1/batches/batch-1/statistics");
    expect(statistics).toHaveAttribute("target", "_blank");
    await user.click(await screen.findByRole("menuitem", { name: "Delete batch" }));
    expect(await screen.findByRole("dialog", { name: "Delete batch?" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete batch" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/review-batches/batch-1",
      { method: "DELETE" },
    ));
  });
});
