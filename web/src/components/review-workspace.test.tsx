import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReviewWorkspace } from "@/components/review-workspace";
import type { CollectionItemRead, MatterSearchFilter, MatterSearchResponse, MetadataDefinitionRead, MetadataGroupRead } from "@/generated/models";
import { coreApi, coreApiContent } from "@/lib/api-client";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));
vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn(), coreApiContent: vi.fn() }));

const baseDefinitions: MetadataDefinitionRead[] = [
  {
    id: "definition-custodian", matter_id: "matter-1", key: "custodian", display_name: "Custodian", description: null,
    type: "TEXT", cardinality: "MULTIPLE", allowed_values: null, value_source: "SYSTEM", reference_target: "CUSTODIAN",
    template_key: "edrm-core", template_version: 1, assertion_policy: "IMMEDIATE", resolution_policy: "EXPLICIT_ONLY",
    searchable: true, facetable: true, reviewable: true, ai_assignable: false, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z",
  },
  {
    id: "definition-responsive", matter_id: "matter-1", key: "responsiveness", display_name: "Responsiveness", description: null,
    type: "ENUM", cardinality: "SINGLE", allowed_values: [{ key: "responsive", label: "Responsive", active: true }], value_source: "ASSERTED", reference_target: null,
    template_key: "edrm-core", template_version: 1, assertion_policy: "IMMEDIATE", resolution_policy: "EXPLICIT_ONLY",
    searchable: true, facetable: true, reviewable: true, ai_assignable: true, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z",
  },
];

const definitions: MetadataDefinitionRead[] = [
  ...baseDefinitions,
  ...Array.from({ length: 9 }, (_, index) => ({
    ...baseDefinitions[1],
    id: `definition-facet-${index}`,
    key: `facet_${index}`,
    display_name: `Facet ${index}`,
    allowed_values: [{ key: `value_${index}`, label: `Value ${index}`, active: true }],
    reviewable: false,
  })),
  {
    ...baseDefinitions[1],
    id: "definition-topics",
    key: "topics",
    display_name: "Topics",
    cardinality: "MULTIPLE",
    allowed_values: [{ key: "topic_1", label: "Topic one", active: true }, { key: "topic_2", label: "Topic two", active: true }],
    reviewable: true,
  },
  {
    ...baseDefinitions[1],
    id: "definition-document-date",
    key: "document_date",
    display_name: "Document date",
    type: "DATE",
    allowed_values: null,
    facetable: false,
    reviewable: false,
    ai_assignable: false,
  },
  {
    ...baseDefinitions[1],
    id: "definition-email-sent",
    key: "email_sent",
    display_name: "Email sent",
    type: "DATETIME",
    allowed_values: null,
    facetable: false,
    reviewable: false,
    ai_assignable: false,
  },
];

const groups: MetadataGroupRead[] = [{
  id: "group-1", matter_id: "matter-1", scope: "SYSTEM", owner_user_id: null, created_by_user_id: "user-1", key: "review",
  display_name: "Review", description: null, sort_order: 10, default_table_visible: true, default_document_visible: true,
  table_visible: true, document_visible: true, definition_ids: definitions.map((definition) => definition.id), status: "ACTIVE",
  template_key: "edrm-core", template_version: 1, created_at: "2026-09-14T12:00:00Z",
}];

const searchResponse: MatterSearchResponse = {
  total: 126,
  took_ms: 8,
  timed_out: false,
  hits: [{
    document_id: "document-1",
    score: 1,
    fields: {
      collection_item_id: "item-1",
      original_filename: "budget-update.eml",
      email_subject: "Budget update",
      source_path: "/mail/inbox/budget-update.eml",
      custodian_names: ["Alice Adams"],
      record_type: "EMAIL",
      metadata: { custodian: ["custodian-1"], responsiveness: null, file_extension: "eml" },
    },
    highlights: { body_text: ["The <mark>budget</mark> discussion was moved to a private communications channel."] },
    best_passage: {
      chunk_id: "chunk-1", ordinal: 0, char_start: 0, char_end: 58,
      text: "The budget discussion was moved to a private communications channel.", score: 0.92,
    },
  }],
  facets: {
    custodian: [{ value: "custodian-1", count: 1 }],
    responsiveness: [{ value: "responsive", count: 1 }],
  },
};

const collectionItem: CollectionItemRead = {
  id: "item-1", tenant_id: "tenant-1", client_id: "client-1", collection_id: "collection-1", source_item_id: "source-1",
  record_type: "EMAIL", original_filename: "budget-update.eml", original_extension: "eml", original_source_path: "/mail/inbox/budget-update.eml",
  source_created_at: null, source_modified_at: null, file_date: null, family_id: null, parent_collection_item_id: null, processing_status: "READY",
  custodian_ids: ["custodian-1"], raw_metadata: {}, unmapped_metadata: {}, created_at: "2026-09-14T12:00:00Z",
  email: { sender: "alice@example.com", subject: "Budget update", sent_at: null, received_at: null, message_id: null, recipients: [] },
  native_artifact: {
    id: "artifact-1", tenant_id: "tenant-1", client_id: "client-1", artifact_class: "SOURCE", artifact_type: "NATIVE", role: "NATIVE",
    original_filename: "budget-update.eml", media_type: "message/rfc822", byte_length: 64, sha256: "abc", status: "FINALIZED",
    created_at: "2026-09-14T12:00:00Z", finalized_at: "2026-09-14T12:00:00Z", collection_id: "collection-1", collection_item_id: "item-1",
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderWorkspace({ initialPage = 1 }: { initialPage?: number } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><ReviewWorkspace matterId="matter-1" initialPage={initialPage} /></QueryClientProvider>);
}

describe("ReviewWorkspace", () => {
  it("searches, facets, opens a document, and keeps coding available", async () => {
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/auth/me") return { id: "user-1", tenant_id: "tenant-1", email: "reviewer@example.com", display_name: "Review User", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", client_id: "client-1", name: "Acme Investigation", status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Acme", status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/tenants/tenant-1/users") return [{ id: "user-1", tenant_id: "tenant-1", email: "reviewer@example.com", display_name: "Review User", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" }] as never;
      if (path === "/v1/matters/matter-1/saved-searches" && !init?.method) return [] as never;
      if (path.endsWith("/metadata-definitions")) return definitions as never;
      if (path.endsWith("/metadata-groups")) return groups as never;
      if (path === "/v1/clients/client-1/custodians") return [{ id: "custodian-1", client_id: "client-1", display_name: "Alice Adams", email_addresses: [], external_reference: null, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" }] as never;
      if (path.endsWith("/search") && init?.method === "POST") return searchResponse as never;
      if (path.endsWith("/facets/custodian/values") && init?.method === "POST") return { field: "custodian", values: [{ value: "custodian-1", count: 1 }], missing_count: 7 } as never;
      if (path.includes("/date-histogram") && init?.method === "POST") {
        const field = path.includes("document_date") ? "document_date" : "email_sent";
        return { field, interval: "month", buckets: [{ start: "2024-01-01T00:00:00Z", count: 1 }] } as never;
      }
      if (path === "/v1/collection-items/item-1") return collectionItem as never;
      if (path.endsWith("/documents/document-1/metadata-values")) return [{ matter_document_id: "document-1", metadata_definition_id: "definition-responsive", key: "responsiveness", display_name: "Responsiveness", type: "ENUM", cardinality: "SINGLE", resolution_state: "EMPTY", values: [], pending_event_ids: [], conflicting_event_ids: [], updated_at: null }] as never;
      if (path.endsWith("/metadata-values/definition-responsive/events") && init?.method === "POST") return {
        event: { id: "event-1", matter_id: "matter-1", matter_document_id: "document-1", metadata_definition_id: "definition-responsive", operation: "SET", value: "responsive", source_type: "HUMAN", source_id: null, actor_id: "user-1", agent_run_id: null, confidence: null, target_event_id: null, supersedes_id: null, effective_status: "ACTIVE", confirmation_state: "UNREVIEWED", created_at: "2026-09-14T12:01:00Z" },
        current: { matter_document_id: "document-1", metadata_definition_id: "definition-responsive", key: "responsiveness", display_name: "Responsiveness", type: "ENUM", cardinality: "SINGLE", resolution_state: "VALUE", values: [{ value: "responsive", source_event_id: "event-1", supporting_event_ids: ["event-1"] }], pending_event_ids: [], conflicting_event_ids: [], updated_at: "2026-09-14T12:01:00Z" },
      } as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs" && init?.method === "POST") return {
        id: "bulk-job-1", matter_id: "matter-1", metadata_definition_id: "definition-responsive", search_index_generation_id: "generation-1",
        search_definition: JSON.parse(String(init.body)).search, value: JSON.parse(String(init.body)).assignments[0].value,
        assignments: JSON.parse(String(init.body)).assignments, status: "QUEUED", matched_count: 0, batch_count: 0,
        processed_count: 0, tagged_count: 0, failed_count: 0, error_message: null, created_by_user_id: "user-1", started_at: null,
        completed_at: null, created_at: "2026-09-14T12:02:00Z", updated_at: "2026-09-14T12:02:00Z",
      } as never;
      if (path === "/v1/matters/matter-1/bulk-tag-jobs/bulk-job-1") return {
        id: "bulk-job-1", matter_id: "matter-1", metadata_definition_id: "definition-responsive", search_index_generation_id: "generation-1",
        search_definition: {}, value: "responsive", assignments: [{ metadata_definition_id: "definition-responsive", value: "responsive" }], status: "COMPLETED", matched_count: 126, batch_count: 1,
        processed_count: 126, tagged_count: 126, failed_count: 0, error_message: null, created_by_user_id: "user-1", started_at: "2026-09-14T12:02:00Z",
        completed_at: "2026-09-14T12:03:00Z", created_at: "2026-09-14T12:02:00Z", updated_at: "2026-09-14T12:03:00Z",
      } as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.mocked(coreApiContent).mockResolvedValue({ bytes: new TextEncoder().encode("From: alice@example.com\nSubject: Budget update\n\nHello from the document."), mediaType: "message/rfc822" });

    const user = userEvent.setup();
    renderWorkspace({ initialPage: 2 });

    expect(await screen.findByText("Budget update")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Document search results" })).getByLabelText("Result 51")).toHaveTextContent("51");
    expect(screen.getByPlaceholderText("Search document body, filenames, paths, email headers, and metadata")).toBeInTheDocument();
    expect(screen.getByText(/moved to a private communications channel/)).toBeInTheDocument();
    expect(document.querySelector("mark")).toHaveTextContent("budget");
    expect(await screen.findByText("Hello from the document.")).toBeInTheDocument();
    expect(screen.getAllByText("Responsiveness").length).toBeGreaterThan(0);

    const emailMetadataResize = screen.getByRole("separator", { name: "Resize email metadata panel" });
    expect(emailMetadataResize).toHaveAttribute("aria-valuenow", "168");
    emailMetadataResize.focus();
    await user.keyboard("{ArrowUp}");
    expect(emailMetadataResize).toHaveAttribute("aria-valuenow", "152");

    const details = screen.getByRole("complementary", { name: "Document details" });
    expect(within(details).queryByText("Custodian")).not.toBeInTheDocument();
    expect(within(details).getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(within(details).queryByRole("button", { name: "Bulk tag results" })).not.toBeInTheDocument();

    await user.click(within(details).getByRole("tab", { name: "Metadata" }));
    expect(within(details).getByText("Custodian")).toBeInTheDocument();
    await user.click(within(details).getByRole("tab", { name: "Coding" }));
    await user.click(within(details).getByRole("combobox", { name: "Responsiveness" }));
    await user.click(screen.getByRole("option", { name: "Responsive" }));
    await user.click(within(details).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const saveCalls = vi.mocked(coreApi).mock.calls.filter(([path]) => path.endsWith("/metadata-values/definition-responsive/events"));
      expect(saveCalls).toHaveLength(1);
      expect(JSON.parse(String(saveCalls[0][1]?.body))).toEqual({ operation: "SET", value: "responsive" });
    });

    await user.click(screen.getByRole("button", { name: /^Filters/ }));
    expect(screen.getByRole("button", { name: "Topics" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Alice Adams/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Custodian" }));
    await user.click(await screen.findByRole("checkbox", { name: /No value/ }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: MatterSearchFilter[] };
      expect(lastRequest.filters).toContainEqual({ field: "custodian", operator: "NOT_EXISTS" });
    });

    await user.click(screen.getByRole("checkbox", { name: /No value/ }));
    await user.click(await screen.findByRole("checkbox", { name: /Alice Adams/ }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: { field: string; values: string[] }[] };
      expect(lastRequest.filters).toContainEqual({ field: "custodian", operator: "IN", values: ["custodian-1"] });
    });

    await user.click(screen.getByRole("button", { name: "Close" }));

    await user.click(screen.getByRole("button", { name: /^Filters/ }));
    const filterDialog = screen.getByRole("dialog", { name: "Filter documents" });
    await user.click(within(filterDialog).getByText("Document date"));
    fireEvent.change(within(filterDialog).getByLabelText("Document date from"), { target: { value: "2024-01-01" } });
    fireEvent.change(within(filterDialog).getByLabelText("Document date to"), { target: { value: "2024-01-31" } });

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: MatterSearchFilter[] };
      expect(lastRequest.filters).toContainEqual({
        field: "document_date",
        operator: "RANGE",
        from: "2024-01-01",
        to: "2024-01-31",
      });
    });

    await user.click(within(filterDialog).getByText("Email sent"));
    fireEvent.change(within(filterDialog).getByLabelText("Email sent from"), { target: { value: "2024-02-01" } });
    fireEvent.change(within(filterDialog).getByLabelText("Email sent to"), { target: { value: "2024-02-02" } });

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: MatterSearchFilter[] };
      expect(lastRequest.filters).toContainEqual({
        field: "email_sent",
        operator: "RANGE",
        from: "2024-02-01T00:00:00.000Z",
        to: "2024-02-02T23:59:59.999Z",
      });
    });

    await user.click(within(filterDialog).getByRole("button", { name: "Close" }));
    await user.click(screen.getByRole("combobox", { name: "Search mode" }));
    await user.click(screen.getByRole("option", { name: "Semantic" }));
    expect(screen.getByPlaceholderText("Find documents by concept or meaning, not only exact words")).toBeInTheDocument();
    const similarity = screen.getByRole("spinbutton", { name: "Minimum semantic similarity" });
    expect(similarity).toHaveAttribute("aria-describedby", "minimum-semantic-similarity-help");
    expect(screen.getByText(/Lower values include more loosely related documents/)).toHaveAttribute("role", "tooltip");
    await user.type(similarity, ".035");
    await user.clear(screen.getByRole("textbox", { name: "Search matter documents" }));
    await user.type(screen.getByRole("textbox", { name: "Search matter documents" }), "concealed pricing discussion");
    await user.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { query: string; search_mode: string; minimum_similarity: number };
      expect(lastRequest).toMatchObject({ query: "concealed pricing discussion", search_mode: "SEMANTIC", minimum_similarity: 0.035 });
    });

    expect(within(details).getByRole("button", { name: "Bulk tag results" })).toBeInTheDocument();
    await user.click(within(details).getByRole("button", { name: "Bulk tag results" }));
    expect(screen.getByRole("alert")).toHaveTextContent("requires a keyword search");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("combobox", { name: "Search mode" }));
    await user.click(screen.getByRole("option", { name: "Keyword" }));
    await user.click(within(details).getByRole("button", { name: "Bulk tag results" }));
    const bulkTagDialog = screen.getByRole("dialog", { name: "Bulk tag search results" });
    await user.click(within(bulkTagDialog).getByRole("combobox", { name: "Field 1" }));
    await user.click(screen.getByRole("option", { name: "Topics" }));
    await user.click(within(bulkTagDialog).getByRole("combobox", { name: "Values" }));
    await user.click(screen.getByRole("option", { name: "Topic one" }));
    await user.click(within(bulkTagDialog).getByRole("button", { name: "Add" }));
    await user.click(within(bulkTagDialog).getByRole("combobox", { name: "Values" }));
    await user.click(screen.getByRole("option", { name: "Topic two" }));
    await user.click(within(bulkTagDialog).getByRole("button", { name: "Add" }));
    await user.click(within(bulkTagDialog).getByRole("button", { name: "Add another field" }));
    await user.click(within(bulkTagDialog).getByRole("combobox", { name: "Value" }));
    await user.click(screen.getByRole("option", { name: "Responsive" }));
    await user.click(screen.getByRole("button", { name: "Start bulk tag job" }));

    await waitFor(() => {
      const createCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/bulk-tag-jobs" && init?.method === "POST");
      expect(createCall).toBeDefined();
      expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ assignments: [
        { metadata_definition_id: "definition-topics", value: ["topic_1", "topic_2"] },
        { metadata_definition_id: "definition-responsive", value: "responsive" },
      ], search: { query: "concealed pricing discussion", search_mode: "KEYWORD" } });
    });
    await user.click(within(screen.getByRole("dialog", { name: "Bulk tag search results" })).getAllByRole("button", { name: /Run in background|Close/ })[0]);

    await user.click(screen.getByRole("combobox", { name: "Search mode" }));
    await user.click(screen.getByRole("option", { name: "Hybrid" }));
    expect(screen.getByPlaceholderText("Combine exact words with conceptually related results")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: "Minimum semantic similarity" })).not.toBeInTheDocument();
  }, 10_000);
});
