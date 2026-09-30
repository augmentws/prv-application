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

describe("MatterView metadata navigation", () => {
  it("shows one Metadata tab with Definitions and Groups subtabs and preserves the legacy groups link", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="groups" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Metadata groups" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Metadata" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Definitions" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "Groups" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Metadata definitions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Metadata groups" })).not.toBeInTheDocument();
  });
});

describe("MatterView token usage", () => {
  it("shows matter totals, model and workflow breakdowns, and the usage ledger", async () => {
    vi.mocked(coreApi).mockImplementation(async (path) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/provider-usage?offset=0&limit=100") return {
        matter_id: "matter-1",
        totals: { record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 },
        by_model: [{ provider: "google", model: "gemini-test", record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        by_job_type: [{ job_type: "MATTER_ANALYSIS_TASK_BATCH", record_count: 1, request_count: 2, input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        entries: [{ id: "usage-1", job_id: "job-1", job_type: "MATTER_ANALYSIS_TASK_BATCH", job_created_at: "2026-09-26T12:00:00Z", provider: "google", model: "gemini-test", started_by_display_name: "Review Manager", started_by_email: "manager@example.com", input_tokens: 1200, cached_input_tokens: 700, cache_write_tokens: 80, output_tokens: 345, total_tokens: 1545 }],
        entries_offset: 0,
        entries_limit: 100,
      } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={client}><MatterView clientId="client-1" matterId="matter-1" requestedTab="usage" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Token usage" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Token usage" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getAllByText("1,545").length).toBeGreaterThan(0);
    expect(screen.getAllByText("gemini-test").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Matter Analysis Task Batch").length).toBeGreaterThan(0);
    expect(screen.getByText("Review Manager")).toBeInTheDocument();
    expect(screen.getByText(/does not yet preserve a versioned provider price snapshot/i)).toBeInTheDocument();
  });
});
