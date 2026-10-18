import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MetadataSection } from "@/components/matter/sections/metadata-section";
import { OverviewSection } from "@/components/matter/sections/overview-section";
import { MATTER, MATTER_BASE_PATH, renderInMatter } from "@/components/matter/test-utils";
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

describe("Matter metadata navigation", () => {
  it("shows configuration actions only on the Overview tab", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/overview-counts") return { document_count: 0, custodian_count: 0 } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    navigation.pathname = `${MATTER_BASE_PATH}/metadata`;
    const view = renderInMatter(<MetadataSection tab="definitions" />);

    expect(await screen.findByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "true");
    // Sections are routes listed in the sidebar, so the page carries no section tabs; the header names the matter.
    expect(screen.queryByRole("tablist", { name: "Matter sections" })).not.toBeInTheDocument();
    expect(within(screen.getByRole("banner", { name: "Matter header" })).getByRole("heading", { level: 1, name: "Matter" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Metadata fields")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByRole("button", { name: "Clone configuration" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save as template" })).not.toBeInTheDocument();

    navigation.pathname = MATTER_BASE_PATH;
    view.rerender(<OverviewSection />);

    const overviewActions = await screen.findByLabelText("Overview actions");
    expect(within(overviewActions).getByRole("button", { name: "Clone configuration" })).toBeInTheDocument();
    expect(within(overviewActions).getByRole("button", { name: "Save as template" })).toBeInTheDocument();
  });

  it("does not expose a manual Email Domain update action", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    navigation.pathname = `${MATTER_BASE_PATH}/metadata`;
    renderInMatter(<MetadataSection tab="definitions" />);

    expect(await screen.findByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("button", { name: "Update email domains" })).not.toBeInTheDocument();
    const metadataSubtabs = screen.getByRole("tablist", { name: "Metadata sections" });
    expect(within(metadataSubtabs.parentElement!).getByRole("button", { name: "Add field" })).toBeInTheDocument();
  });

  it("shows a metadata field description on hover", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [{
        id: "definition-1",
        matter_id: "matter-1",
        key: "source_created_at",
        display_name: "Source created at",
        description: "Creation timestamp reported by the source system.",
        type: "DATETIME",
        cardinality: "SINGLE",
        allowed_values: null,
        value_source: "IMPORTED",
        reference_target: null,
        template_key: "edrm-core",
        template_version: 4,
        assertion_policy: "IMMEDIATE",
        resolution_policy: "EXPLICIT_ONLY",
        searchable: true,
        facetable: true,
        normalize_to_lowercase: false,
        hierarchy_separator: null,
        reviewable: true,
        ai_assignable: false,
        status: "ACTIVE",
      }] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    navigation.pathname = `${MATTER_BASE_PATH}/metadata`;
    renderInMatter(<MetadataSection tab="definitions" />);

    const field = await screen.findByText("Source created at");
    expect(field.parentElement).toHaveAttribute("title", "Creation timestamp reported by the source system.");
  });

  it("shows Metadata groups as the Groups subtab of Metadata fields", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });

    navigation.pathname = `${MATTER_BASE_PATH}/metadata/groups`;
    renderInMatter(<MetadataSection tab="groups" />);

    expect(await screen.findByRole("heading", { name: "Metadata groups" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Metadata fields")).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Groups" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Metadata definitions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Metadata groups" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add field" })).not.toBeInTheDocument();
  });

  it("allows an administrator to make an asserted field AI-assignable", async () => {
    const definition = {
      id: "definition-1",
      matter_id: "matter-1",
      key: "issue_coding",
      display_name: "Issue coding",
      description: "Matter issue labels",
      type: "ENUM",
      cardinality: "MULTIPLE",
      allowed_values: [{ key: "topic_401", label: "Olympics", description: null, active: true }],
      value_source: "ASSERTED",
      reference_target: null,
      template_key: null,
      template_version: null,
      assertion_policy: "IMMEDIATE",
      resolution_policy: "EXPLICIT_ONLY",
      searchable: true,
      facetable: true,
      normalize_to_lowercase: false,
      hierarchy_separator: null,
      reviewable: true,
      ai_assignable: false,
      status: "ACTIVE",
      created_at: "2026-10-07T12:00:00Z",
    } as const;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return MATTER as never;
      if (path === "/v1/matters/matter-1/metadata-definitions/definition-1" && init?.method === "PATCH") return { ...definition, ai_assignable: true } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [definition] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();

    navigation.pathname = `${MATTER_BASE_PATH}/metadata`;
    renderInMatter(<MetadataSection tab="definitions" />);

    await user.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByRole("dialog", { name: "Edit Issue coding" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Controlled values" })).toBeInTheDocument();
    await user.click(screen.getByLabelText("Agents may assign values"));
    await user.click(screen.getByRole("button", { name: "Save field settings" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/matters/matter-1/metadata-definitions/definition-1",
      { method: "PATCH", body: JSON.stringify({ ai_assignable: true }) },
    ));
    expect(await screen.findByText("agent")).toBeInTheDocument();
  });
});
