import { cleanup, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UsageSection } from "@/components/matter/sections/usage-section";
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

describe("Matter token usage", () => {
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

    navigation.pathname = `${MATTER_BASE_PATH}/usage`;
    renderInMatter(<UsageSection />);

    expect(await screen.findByRole("heading", { name: "Token usage" })).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Token usage")).toHaveAttribute("aria-current", "page");
    expect(screen.getAllByText("1,545").length).toBeGreaterThan(0);
    expect(screen.getAllByText("gemini-test").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Question answering").length).toBeGreaterThan(0);
    expect(screen.getByText("Review Manager")).toBeInTheDocument();
    expect(screen.getByText(/does not yet preserve a versioned provider price snapshot/i)).toBeInTheDocument();
  });
});
