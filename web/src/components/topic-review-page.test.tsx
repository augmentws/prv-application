import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TopicReviewPage } from "@/components/topic-review-page";
import { coreApi } from "@/lib/api-client";

const push = vi.fn();
vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const job = {
  id: "topic-job-1",
  matter_id: "matter-1",
  status: "AWAITING_REVIEW",
  topic_count: 1,
  clusters: [{
    id: "cluster-1",
    ordinal: 1,
    topic_key: "pricing",
    name: "Pricing",
    description: "Pricing discussions",
    keywords: ["price", "rate"],
    representative_excerpts: ["We should align the proposed prices."],
    included: true,
    sampled_chunk_count: 42,
    assigned_chunk_count: 0,
    assigned_document_count: 0,
  }],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TopicReviewPage", () => {
  it("edits and applies topic proposals on a dedicated page", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/clients/client-1") return { id: "client-1", name: "Client" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", name: "Matter" } as never;
      if (path === "/v1/matters/matter-1/topic-jobs/topic-job-1" && !init?.method) return job as never;
      if (path === "/v1/matters/matter-1/topic-jobs/topic-job-1/apply" && init?.method === "POST") return { ...job, status: "PUBLISHING" } as never;
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={queryClient}><TopicReviewPage clientId="client-1" matterId="matter-1" jobId="topic-job-1" /></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "Review proposed topics" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const name = screen.getByLabelText("Topic name");
    await user.clear(name);
    await user.type(name, "Coordinated pricing");
    await user.click(screen.getByRole("button", { name: "Apply 1 topic" }));

    await waitFor(() => {
      const applyCall = vi.mocked(coreApi).mock.calls.find(([path]) => path.endsWith("/apply"));
      expect(JSON.parse(String(applyCall?.[1]?.body))).toEqual({ topics: [{ id: "cluster-1", name: "Coordinated pricing", description: "Pricing discussions", included: true }] });
      expect(push).toHaveBeenCalledWith("/app/clients/client-1/matters/matter-1?tab=jobs");
    });
  });
});
