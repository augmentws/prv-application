import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MatterView } from "@/components/views/matter-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("MatterView jobs", () => {
  it("shows a snapshotted zero-progress bulk tag job as waiting", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [{
        id: "definition-1",
        display_name: "Responsiveness",
        type: "ENUM",
        allowed_values: [{ key: "responsive", label: "Responsive", active: true }],
      }] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/document-imports") return [] as never;
      if (path === "/v1/matters/matter-1/embedding-jobs") return [] as never;
      if (path === "/v1/matters/matter-1/saved-searches") return [] as never;
      if (path === "/v1/matters/matter-1/topic-jobs") return [{
        id: "topic-job-1",
        operating_mode: "AUTO",
        requested_topic_count: null,
        sample_size: 1000,
        assignment_mode: "APPEND",
        status: "AWAITING_REVIEW",
        document_count: 100,
        processed_document_count: 0,
        topic_count: 2,
        assigned_document_count: 0,
        failed_count: 0,
        created_at: "2026-09-24T11:00:00Z",
        clusters: [],
      }, {
        id: "topic-job-2",
        operating_mode: "AUTO",
        requested_topic_count: null,
        sample_size: 1000,
        assignment_mode: "APPEND",
        status: "COMPLETED",
        document_count: 100,
        processed_document_count: 100,
        topic_count: 1,
        assigned_document_count: 95,
        failed_count: 0,
        created_at: "2026-09-24T10:00:00Z",
        clusters: [{ id: "cluster-1", name: "Pricing" }],
      }] as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs?limit=100") return [{
        id: "bulk-1",
        metadata_definition_id: "definition-1",
        value: "responsive",
        assignments: [{ metadata_definition_id: "definition-1", value: "responsive" }],
        search_definition: { query: "price coordination", filters: [{ field: "custodian" }] },
        status: "RUNNING",
        processed_count: 0,
        matched_count: 100,
        tagged_count: 0,
        failed_count: 0,
        error_message: null,
        created_at: "2026-09-24T12:00:00Z",
      }] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="jobs" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Bulk tagging" })).toBeInTheDocument();
    expect(screen.getByText("Responsiveness: Responsive")).toBeInTheDocument();
    expect(screen.getByText("“price coordination” + 1 filter")).toBeInTheDocument();
    expect(screen.getByText("waiting")).toBeInTheDocument();
    expect(screen.getByText("Waiting for capacity")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Topic clustering" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "Job type" }));
    await user.click(screen.getByRole("option", { name: "Topic clustering" }));

    expect(screen.getByRole("heading", { name: "Topic clustering" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Bulk tagging" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-1");
    expect(screen.getByRole("link", { name: "View topics" })).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/topic-jobs/topic-job-2");
    expect(window.localStorage.getItem("priv-view:matter-jobs:selected-type")).toBe("TOPIC_CLUSTERING");
  });
});
