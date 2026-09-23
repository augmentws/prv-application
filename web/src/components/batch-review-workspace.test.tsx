import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BatchReviewWorkspace } from "@/components/batch-review-workspace";
import type { ReviewBatchRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("@/components/document-viewer-dialog", () => ({
  DocumentViewerSurface: ({ item, highlightedParagraphs = [] }: { item: { original_filename: string }; highlightedParagraphs?: number[] }) => <div>{item.original_filename}<span data-testid="highlighted-paragraphs">{highlightedParagraphs.join(",")}</span></div>,
}));

const citedDocumentId = "2290912f-9d6f-44a1-a133-069f8877a8cd";

const batch = {
  id: "batch-1",
  matter_id: "matter-1",
  name: "First-level review",
  description: "Review sample",
  selection_type: "RANDOM_MATTER",
  selection_definition: {},
  source_batch_id: null,
  search_index_generation_id: null,
  sample_size: 1,
  random_seed: "seed",
  assigned_user_id: "user-1",
  assigned_user: { id: "user-1", display_name: "Reviewer", email: "reviewer@example.com" },
  reviewer_value_visibility: "OWN_VALUES",
  status: "READY",
  search_status: "READY",
  search_error_message: null,
  workflow_id: "review-batch:batch-1",
  document_count: 1,
  error_message: null,
  created_by_user_id: "user-1",
  coding_groups: [{
    id: "group-1",
    source_metadata_group_id: "source-group-1",
    display_name: "Review decisions",
    description: null,
    sort_order: 0,
    fields: [{
      id: "field-1",
      metadata_definition_id: "definition-1",
      sort_order: 0,
      definition_snapshot: {
        key: "responsiveness",
        display_name: "Responsiveness",
        description: "Code whether the document is responsive.",
        type: "ENUM",
        cardinality: "SINGLE",
        allowed_values: [{ key: "responsive", label: "Responsive", active: true }],
      },
    }],
  }],
  created_at: "2026-09-15T12:00:00Z",
  updated_at: "2026-09-15T12:00:00Z",
  completed_at: "2026-09-15T12:00:00Z",
} satisfies ReviewBatchRead;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BatchReviewWorkspace", () => {
  it("starts the reviewer run and saves the coding panel as one document request", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1") return { id: "matter-1", client_id: "client-1", name: "Investigation", status: "ACTIVE", created_at: "2026-09-15T12:00:00Z" } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", name: "Batch Client" } as never;
      if (path === "/v1/clients/client-1/custodians") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-definitions") return [] as never;
      if (path === "/v1/matters/matter-1/metadata-groups") return [] as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1" && !init?.method) return batch as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/topic-taxonomy") return null as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/search" && init?.method === "POST") return {
        total: 1,
        took_ms: 2,
        timed_out: false,
        hits: [{
          document_id: "document-1",
          score: null,
          fields: {
            collection_item_id: "item-1",
            email_subject: "Contract review request",
            original_filename: "contract.eml",
            record_type: "EMAIL",
            source_path: "mailbox/contract.eml",
            metadata: {},
          },
          highlights: {},
          best_passage: null,
        }],
        facets: {},
      } as never;
      if (path.endsWith("/review-run") && init?.method === "POST") return { id: "run-1", review_batch_id: "batch-1", run_type: "HUMAN", purpose: "REVIEW", status: "RUNNING", result_policy: "ISOLATED", parent_run_id: null, actor_user_id: "user-1", agent_definition_version_id: null, configuration_snapshot: {}, initiated_by_user_id: "user-1", processed_document_count: 0, error_message: null, started_at: "2026-09-15T12:00:00Z", completed_at: null, created_at: "2026-09-15T12:00:00Z", updated_at: "2026-09-15T12:00:00Z" } as never;
      if (path.includes(`/documents?run_id=run-1&document_id=${citedDocumentId}`)) return [{ matter_document_id: citedDocumentId, source_collection_id: "collection-1", collection_item_id: "item-2", sequence_number: 2, review_status: "NOT_STARTED" }] as never;
      if (path.includes("/documents?run_id=run-1")) return [{ matter_document_id: "document-1", source_collection_id: "collection-1", collection_item_id: "item-1", sequence_number: 1, review_status: "NOT_STARTED" }] as never;
      if (path.endsWith("/runs/run-1/progress")) return { review_batch_run_id: "run-1", document_count: 1, not_started_count: 1, in_progress_count: 0, completed_count: 0, skipped_count: 0 } as never;
      if (path === "/v1/collection-items/item-1") return { original_filename: "contract.eml" } as never;
      if (path === "/v1/collection-items/item-2") return { original_filename: "cited-document.eml" } as never;
      if (path.endsWith("/runs/run-1/documents/document-1") && !init?.method) return { matter_document_id: "document-1", review_status: "NOT_STARTED", values: [], reviewer_values: [] } as never;
      if (path.endsWith(`/runs/run-1/documents/${citedDocumentId}`) && !init?.method) return { matter_document_id: citedDocumentId, review_status: "NOT_STARTED", values: [], reviewer_values: [] } as never;
      if (path.endsWith("/runs/run-1/documents/document-1/values") && init?.method === "PUT") return [] as never;
      if (path === "/v1/matters/matter-1/agents?workflow_type=BATCH_CHAT") return [{ id: "batch-agent-1", name: "Batch Chat Agent" }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=BATCH_CHAT&review_batch_id=batch-1") return [] as never;
      if (path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST") return {
        id: "conversation-1",
        tenant_id: "tenant-1",
        client_id: "client-1",
        matter_id: "matter-1",
        review_batch_id: "batch-1",
        agent_definition_id: "batch-agent-1",
        agent_definition_version_id: "batch-agent-version-1",
        title: "Renewal questions",
        workflow_type: "BATCH_CHAT",
        status: "ACTIVE",
        initiated_by_user_id: "user-1",
        created_at: "2026-09-15T12:00:00Z",
        updated_at: "2026-09-15T12:00:00Z",
      } as never;
      if (path === "/v1/agent-conversations/conversation-1/messages") return [{ id: "message-1", conversation_id: "conversation-1", turn_id: "turn-1", sequence: 1, role: "ASSISTANT", content: `See [Document ${citedDocumentId} ¶1-5].`, agent_run_id: "agent-run-1", created_by_user_id: null, created_at: "2026-09-15T12:00:00Z" }] as never;
      if (path === "/v1/agent-conversations/conversation-1/runs") return [] as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={queryClient}><BatchReviewWorkspace matterId="matter-1" batchId="batch-1" /></QueryClientProvider>);

    expect(await screen.findByText("Contract review request")).toBeInTheDocument();
    expect(await screen.findByText("contract.eml")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveClass("h-dvh", "min-h-0", "max-h-dvh", "overflow-hidden");
    expect(screen.getByLabelText("Batch review layout")).toHaveStyle({
      gridTemplateColumns: "24rem minmax(0, 1fr) 25rem",
      gridTemplateRows: "minmax(0, 1fr)",
    });
    expect(screen.getByLabelText("Batch documents")).toHaveClass("min-h-0", "overflow-hidden");
    expect(screen.getByLabelText("Batch analysis, coding, and chat")).toHaveClass("min-h-0", "overflow-hidden");
    await user.click(screen.getByRole("combobox", { name: "Search mode" }));
    await user.click(screen.getByRole("option", { name: "Semantic" }));
    await user.type(screen.getByRole("spinbutton", { name: "Minimum semantic similarity" }), "0.8");
    await user.type(screen.getByRole("textbox", { name: "Search batch documents" }), "contract termination risk");
    await user.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { search_mode: string; minimum_similarity: number };
      expect(lastRequest).toMatchObject({ search_mode: "SEMANTIC", minimum_similarity: 0.8 });
    });

    await user.click(screen.getByRole("button", { name: "Coding" }));
    await user.click(screen.getByRole("combobox", { name: "Responsiveness" }));
    await user.click(screen.getByRole("option", { name: "Responsive" }));
    await user.click(screen.getByRole("button", { name: "Save & next" }));

    await waitFor(() => {
      const saveCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path.endsWith("/values") && init?.method === "PUT");
      expect(saveCall).toBeDefined();
      expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
        matter_document_id: "document-1",
        fields: [{ metadata_definition_id: "definition-1", values: ["responsive"], confidence: null }],
      });
    });

    await user.click(screen.getByRole("button", { name: "Chat" }));
    expect(screen.getByLabelText("Batch review layout")).toHaveStyle({ gridTemplateColumns: "24rem minmax(0, 1fr) 30rem" });
    expect(await screen.findByText("Chat with this batch")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Batch chat name" }), "Renewal questions");
    await user.click(screen.getByRole("button", { name: "Start batch chat" }));
    await waitFor(() => {
      const createCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST");
      expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
        agent_definition_id: "batch-agent-1",
        title: "Renewal questions",
        workflow_type: "BATCH_CHAT",
        review_batch_id: "batch-1",
      });
    });
    await user.click(await screen.findByRole("button", { name: `[Document ${citedDocumentId} ¶1-5]` }));
    expect(await screen.findByText("cited-document.eml")).toBeInTheDocument();
    expect(screen.getByTestId("highlighted-paragraphs")).toHaveTextContent("1,2,3,4,5");
  });
});
