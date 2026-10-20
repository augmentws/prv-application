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
    searchable: true, facetable: true, hierarchy_separator: null, reviewable: true, ai_assignable: false, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z",
  },
  {
    id: "definition-responsive", matter_id: "matter-1", key: "responsiveness", display_name: "Responsiveness", description: null,
    type: "ENUM", cardinality: "SINGLE", allowed_values: [{ key: "responsive", label: "Responsive", active: true }], value_source: "ASSERTED", reference_target: null,
    template_key: "edrm-core", template_version: 1, assertion_policy: "IMMEDIATE", resolution_policy: "EXPLICIT_ONLY",
    searchable: true, facetable: true, hierarchy_separator: null, reviewable: true, ai_assignable: true, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z",
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
    id: "definition-tr2016",
    key: "tr2016_facets",
    display_name: "Trec2016",
    type: "TEXT",
    cardinality: "MULTIPLE",
    allowed_values: null,
    hierarchy_separator: "/",
    reviewable: true,
  },
  {
    ...baseDefinitions[1],
    id: "definition-category",
    key: "category",
    display_name: "Initiative",
    type: "TEXT",
    allowed_values: null,
    hierarchy_separator: "/",
    reviewable: false,
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
      metadata: { custodian: ["custodian-1"], responsiveness: null, tr2016_facets: ["space/253"], file_extension: "eml" },
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
  window.localStorage.clear();
  vi.clearAllMocks();
});

function renderWorkspace({ initialPage = 1 }: { initialPage?: number } = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><ReviewWorkspace matterId="matter-1" initialPage={initialPage} /></QueryClientProvider>);
}

describe("ReviewWorkspace", () => {
  it("searches, facets, opens a document, and keeps coding available", async () => {
    let tr2016Values: string[] = [];
    let tr2016EventSequence = 0;
    let latestSearchRequest: Record<string, unknown> | undefined;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/auth/me") return { id: "user-1", tenant_id: "tenant-1", email: "reviewer@example.com", display_name: "Review User", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/matters/matter-1") return { id: "matter-1", client_id: "client-1", name: "Acme Investigation", status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "Acme", status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" } as never;
      if (path === "/v1/authorization/effective?resource_type=MATTER&resource_id=matter-1") return { resource_type: "MATTER", resource_id: "matter-1", permission_keys: ["matter.view", "matter.configure", "metadata.view", "document.view", "document.review", "document.view_history", "document.bulk_code"] } as never;
      if (path === "/v1/matters/matter-1/access-users?permission_key=document.view") return [{ id: "user-1", tenant_id: "tenant-1", email: "reviewer@example.com", display_name: "Review User", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" }] as never;
      if (path === "/v1/matters/matter-1/saved-searches" && !init?.method) return [] as never;
      if (path === "/v1/matters/matter-1/agents?workflow_type=SEARCH_CHAT") return [{ id: "agent-search-chat", name: "Batch Chat Agent" }] as never;
      if (path === "/v1/matters/matter-1/agent-conversations?workflow_type=SEARCH_CHAT" && !init?.method) {
        const currentSearch = latestSearchRequest ?? {};
        const currentScope = {
          ...currentSearch,
          filters: ((currentSearch.filters as Array<Record<string, unknown>> | undefined) ?? []).map((filter) => ({
            ...filter,
            value: filter.value ?? null,
            values: filter.values ?? null,
            from: filter.from ?? null,
            to: filter.to ?? null,
            include_missing: filter.include_missing ?? false,
          })),
          facets: [], sort: [], offset: 0, size: 500,
        };
        return [
          { id: "conversation-current", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1", matter_definition_id: null, review_batch_id: null, search_definition: currentScope, agent_definition_id: "agent-search-chat", agent_definition_version_id: "agent-version-1", title: "Current pricing chat", workflow_type: "SEARCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1", created_at: "2026-09-14T12:02:00Z", updated_at: "2026-09-14T12:02:00Z" },
          { id: "conversation-legacy", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1", matter_definition_id: null, review_batch_id: null, search_definition: { query: "legacy pricing", search_mode: "KEYWORD", minimum_similarity: null, candidate_limit: null, query_fields: [], filters: [], facets: [], sort: [], offset: 0, size: 500 }, agent_definition_id: "agent-search-chat", agent_definition_version_id: "agent-version-1", title: "Legacy search chat", workflow_type: "SEARCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1", created_at: "2026-09-13T12:02:00Z", updated_at: "2026-09-13T12:02:00Z" },
        ] as never;
      }
      if (path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST") {
        const payload = JSON.parse(String(init.body));
        return {
          id: "conversation-search-1", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1",
          matter_definition_id: null, review_batch_id: null,
          search_definition: {
            ...payload.search_definition,
            filters: payload.search_definition.filters.map((filter: Record<string, unknown>) => ({
              ...filter,
              value: filter.value ?? null,
              values: filter.values ?? null,
              from: filter.from ?? null,
              to: filter.to ?? null,
              include_missing: filter.include_missing ?? false,
            })),
          },
          agent_definition_id: "agent-search-chat", agent_definition_version_id: "agent-version-1", title: payload.title,
          workflow_type: "SEARCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1",
          created_at: "2026-09-14T12:00:00Z", updated_at: "2026-09-14T12:00:00Z",
        } as never;
      }
      if (path === "/v1/agent-conversations/conversation-search-1/messages") return [] as never;
      if (path === "/v1/agent-conversations/conversation-search-1/runs") return [] as never;
      if (["/v1/agent-conversations/conversation-current/messages", "/v1/agent-conversations/conversation-current/runs", "/v1/agent-conversations/conversation-legacy/messages", "/v1/agent-conversations/conversation-legacy/runs"].includes(path)) return [] as never;
      if (path === "/v1/agent-conversations/conversation-current" && init?.method === "PATCH") {
        const payload = JSON.parse(String(init.body));
        return { id: "conversation-current", tenant_id: "tenant-1", client_id: "client-1", matter_id: "matter-1", matter_definition_id: null, review_batch_id: null, search_definition: latestSearchRequest, agent_definition_id: "agent-search-chat", agent_definition_version_id: "agent-version-1", title: payload.title, workflow_type: "SEARCH_CHAT", status: "ACTIVE", initiated_by_user_id: "user-1", created_at: "2026-09-14T12:02:00Z", updated_at: "2026-09-14T12:04:00Z" } as never;
      }
      if (path === "/v1/agent-conversations/conversation-search-1/turns" && init?.method === "POST") {
        const payload = JSON.parse(String(init.body));
        return {
          turn: {
            id: "turn-search-1", conversation_id: "conversation-search-1", sequence: 1, status: "QUEUED", created_by_user_id: "user-1",
            completed_at: null, error_message: null, created_at: "2026-09-14T12:03:00Z", updated_at: "2026-09-14T12:03:00Z",
          },
          message: {
            id: "message-search-1", conversation_id: "conversation-search-1", turn_id: "turn-search-1", sequence: 1, role: "USER",
            content: payload.message, message_data: {}, created_by_user_id: "user-1", agent_run_id: null,
            created_at: "2026-09-14T12:03:00Z",
          },
          run: {
            id: "run-search-1", conversation_id: "conversation-search-1", turn_id: "turn-search-1", sequence: 1, workflow_id: "agent-run:run-search-1",
            parent_run_id: null, deferred_from_run_id: null, agent_definition_version_id: "agent-version-1", actor_user_id: "user-1",
            model_key: "configured-default", status: "QUEUED", output_text: null, request_count: 0, tool_call_count: 0,
            input_tokens: 0, cached_input_tokens: 0, cache_write_tokens: 0, output_tokens: 0, error_message: null,
            started_at: null, completed_at: null, created_at: "2026-09-14T12:03:00Z",
          },
        } as never;
      }
      if (path.endsWith("/metadata-definitions")) return definitions as never;
      if (path.endsWith("/metadata-groups")) return groups as never;
      if (path === "/v1/matters/matter-1/custodians") return [{ id: "custodian-1", client_id: "client-1", display_name: "Alice Adams", email_addresses: [], external_reference: null, status: "ACTIVE", created_at: "2026-09-14T12:00:00Z" }] as never;
      if (path.endsWith("/search") && init?.method === "POST") {
        latestSearchRequest = JSON.parse(String(init.body));
        return searchResponse as never;
      }
      if (path.endsWith("/facets/coded_by/values") && init?.method === "POST") return {
        field: "coded_by",
        values: [{ value: "user:reviewer@example.com", label: "Review User (reviewer@example.com)", count: 12 }],
        missing_count: 4,
        total_count: 16,
        has_more: false,
      } as never;
      if (path.endsWith("/bulk-tag-jobs/preview") && init?.method === "POST") return { candidate_count: 126, matched_count: 42 } as never;
      if (path.endsWith("/facets/custodian/values") && init?.method === "POST") {
        const request = JSON.parse(String(init.body)) as { size: number };
        const facetValues = Array.from({ length: 9 }, (_, index) => ({ value: `custodian-${index + 1}`, count: 9 - index }));
        return { field: "custodian", values: facetValues.slice(0, request.size), missing_count: 7, total_count: 16, has_more: request.size < facetValues.length } as never;
      }
      if (path.endsWith("/facets/category/values") && init?.method === "POST") {
        const request = JSON.parse(String(init.body)) as { parent: string | null };
        if (request.parent === "Initiatives") return { field: "category", values: [{ value: "Initiatives/344", label: "344", parent: "Initiatives", has_children: false, count: 1 }] } as never;
        return { field: "category", values: [{ value: "Initiatives", label: "Initiatives", parent: null, has_children: true, count: 1 }] } as never;
      }
      if (path.includes("/date-histogram") && init?.method === "POST") {
        const field = path.includes("document_date") ? "document_date" : "email_sent";
        return { field, interval: "month", buckets: [{ start: "2024-01-01T00:00:00Z", count: 1 }] } as never;
      }
      if (path === "/v1/matters/matter-1/documents/document-1/collection-item") return collectionItem as never;
      if (path.endsWith("/documents/document-1/coding-history")) return {
        direct: [{
          metadata_event_id: "event-direct-1", metadata_definition_id: "definition-responsive", field_key: "responsiveness", field_display_name: "Responsiveness",
          operation: "SET", value: "responsive", value_label: "Responsive", recorded_at: "2026-09-14T12:01:00Z", source_kind: "HUMAN",
          source_label: "reviewer@example.com", source_detail: "Review User", score: null, effective_status: "ACTIVE", confirmation_state: "UNREVIEWED",
        }],
        batches: [{
          review_batch_id: "batch-1", batch_name: "Jev first pass", batch_status: "READY", entries: [{
            review_batch_run_id: "run-1", run_type: "WORKFLOW", purpose: "ASSESSMENT", metadata_definition_id: "definition-responsive",
            field_key: "responsiveness", field_display_name: "Responsiveness", value: "responsive", value_label: "Responsive", recorded_at: "2026-09-14T11:00:00Z",
            source_kind: "JEV", source_label: "Jev", source_detail: "gpt-test", score: 0.91, score_kind: "PROVIDER_CONFIDENCE", question_key: "responsive", review_decision_result_id: "decision-1",
          }],
        }],
      } as never;
      if (path.endsWith("/documents/document-1/metadata-values")) return [
        { matter_document_id: "document-1", metadata_definition_id: "definition-responsive", key: "responsiveness", display_name: "Responsiveness", type: "ENUM", cardinality: "SINGLE", resolution_state: "EMPTY", values: [], pending_event_ids: [], conflicting_event_ids: [], updated_at: null },
        { matter_document_id: "document-1", metadata_definition_id: "definition-tr2016", key: "tr2016_facets", display_name: "Trec2016", type: "TEXT", cardinality: "MULTIPLE", resolution_state: "EMPTY", values: [], pending_event_ids: [], conflicting_event_ids: [], updated_at: null },
      ] as never;
      if (path.endsWith("/metadata-values/definition-tr2016/events") && init?.method === "POST") {
        const payload = JSON.parse(String(init.body)) as { operation: "CLEAR" | "ADD"; value?: string };
        tr2016EventSequence += 1;
        if (payload.operation === "CLEAR") tr2016Values = [];
        if (payload.operation === "ADD" && payload.value) tr2016Values.push(payload.value);
        const eventId = `event-tr2016-${tr2016EventSequence}`;
        return {
          event: { id: eventId, matter_id: "matter-1", matter_document_id: "document-1", metadata_definition_id: "definition-tr2016", operation: payload.operation, value: payload.value ?? null, source_type: "HUMAN", source_id: null, actor_id: "user-1", agent_run_id: null, confidence: null, target_event_id: null, supersedes_id: null, effective_status: "ACTIVE", confirmation_state: "UNREVIEWED", created_at: "2026-09-14T12:01:00Z" },
          current: { matter_document_id: "document-1", metadata_definition_id: "definition-tr2016", key: "tr2016_facets", display_name: "Trec2016", type: "TEXT", cardinality: "MULTIPLE", resolution_state: tr2016Values.length ? "VALUE" : "EMPTY", values: tr2016Values.map((value, index) => ({ value, source_event_id: `event-tr2016-${index + 1}`, supporting_event_ids: [`event-tr2016-${index + 1}`] })), pending_event_ids: [], conflicting_event_ids: [], updated_at: "2026-09-14T12:01:00Z" },
          search_operation_id: null,
        } as never;
      }
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
    vi.mocked(coreApiContent).mockResolvedValue({ bytes: new TextEncoder().encode("From: alice@example.com\nSubject: Budget update\n\nHello about the budget discussion from the document."), mediaType: "message/rfc822" });

    const user = userEvent.setup();
    renderWorkspace({ initialPage: 2 });

    expect(await screen.findByText("Budget update")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveClass("fixed", "inset-0", "overscroll-none");
    expect(screen.getByRole("main")).toHaveStyle({ overflow: "clip" });
    const searchFilters = screen.getByRole("complementary", { name: "Search filters" });
    expect(searchFilters.querySelector(".overflow-y-auto")).toHaveClass("overscroll-contain");
    const documentFilters = within(searchFilters).getByRole("region", { name: "Document fields filters" });
    const documentGroupToggle = within(documentFilters).getByRole("button", { name: "Document fields filter group" });
    expect(documentGroupToggle).toHaveAttribute("aria-expanded", "false");
    await user.click(documentGroupToggle);
    expect(documentFilters).toHaveTextContent("Coded by");
    expect(window.localStorage.getItem("priv-view:metadata-filter-groups:search:matter-1:document-fields")).toBe("open");
    const reviewFilters = within(searchFilters).getByRole("region", { name: "Review filters" });
    const reviewGroupToggle = within(reviewFilters).getByRole("button", { name: "Review filter group" });
    expect(reviewGroupToggle).toHaveAttribute("aria-expanded", "false");
    expect(within(reviewFilters).queryByRole("button", { name: "Custodian" })).not.toBeInTheDocument();
    await user.click(reviewGroupToggle);
    expect(reviewGroupToggle).toHaveAttribute("aria-expanded", "true");
    expect(reviewFilters).toHaveTextContent("Custodian");
    expect(window.localStorage.getItem("priv-view:metadata-filter-groups:search:matter-1:group-1")).toBe("open");
    await user.click(reviewGroupToggle);
    expect(window.localStorage.getItem("priv-view:metadata-filter-groups:search:matter-1:group-1")).toBe("closed");
    await user.click(reviewGroupToggle);
    expect(within(screen.getByRole("list", { name: "Document search results" })).getByLabelText("Result 51")).toHaveTextContent("51");
    expect(screen.getByPlaceholderText("Search document body, filenames, paths, email headers, and metadata")).toBeInTheDocument();
    expect(screen.getByText(/moved to a private communications channel/)).toBeInTheDocument();
    expect(document.querySelector("mark")).toHaveTextContent("budget");
    const selectedDocument = screen.getByRole("region", { name: "Selected document" });
    await waitFor(() => expect(selectedDocument.querySelector("pre")).toHaveTextContent("Hello about the budget discussion"));
    expect(selectedDocument.querySelector("[data-search-highlight]")).toHaveTextContent("budget");
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
    expect(within(details).getByText("space/253")).toBeInTheDocument();

    const tr2016Input = within(details).getByLabelText("Trec2016");
    await user.type(tr2016Input, "space/999");
    await user.click(within(tr2016Input.parentElement!).getByRole("button", { name: "Add" }));
    await user.click(within(details).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      const tr2016Calls = vi.mocked(coreApi).mock.calls.filter(([path]) => path.endsWith("/metadata-values/definition-tr2016/events"));
      expect(tr2016Calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
        { operation: "CLEAR" },
        { operation: "ADD", value: "space/253" },
        { operation: "ADD", value: "space/999" },
      ]);
    });

    await user.click(within(details).getByRole("tab", { name: "Metadata" }));
    expect(within(details).getByText("Custodian")).toBeInTheDocument();
    await user.click(within(details).getByRole("tab", { name: "History" }));
    expect(await within(details).findByText("Direct (no batch)")).toBeInTheDocument();
    expect(within(details).getByText("Jev first pass")).toBeInTheDocument();
    expect(within(details).getByText("91% provider confidence")).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Coded by" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Coded by" }));
    const reviewerFilter = await screen.findByRole("checkbox", { name: /Review User \(reviewer@example\.com\)/ });
    await user.click(reviewerFilter);
    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: MatterSearchFilter[] };
      expect(lastRequest.filters).toContainEqual({ field: "coded_by", operator: "IN", values: ["user:reviewer@example.com"] });
    });
    await user.click(reviewerFilter);
    await user.click(screen.getByRole("button", { name: "Coded by" }));
    expect(screen.queryByRole("checkbox", { name: /Alice Adams/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Custodian" }));
    const moreCustodians = await screen.findByRole("button", { name: "Show more Custodian values" });
    expect(screen.queryByRole("checkbox", { name: /custodian-9/ })).not.toBeInTheDocument();
    await user.click(moreCustodians);
    expect(await screen.findByRole("checkbox", { name: /custodian-9/ })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "Show more Custodian values" })).not.toBeInTheDocument());
    await user.click(screen.getByRole("checkbox", { name: /Has value/ }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: MatterSearchFilter[] };
      expect(lastRequest.filters).toContainEqual({ field: "custodian", operator: "EXISTS" });
    });

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

    const aliceFilter = screen.getByRole("checkbox", { name: /Alice Adams/ });
    await user.click(screen.getByRole("button", { name: "Negate" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: { field: string; values: string[] }[] };
      expect(lastRequest.filters).toContainEqual({ field: "custodian", operator: "NOT_IN", values: ["custodian-1"] });
      expect(aliceFilter.nextElementSibling).toHaveAttribute("data-selection-mode", "excluded");
    });

    await user.click(screen.getByRole("button", { name: "Include" }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: { field: string; values: string[] }[] };
      expect(lastRequest.filters).toContainEqual({ field: "custodian", operator: "IN", values: ["custodian-1"] });
      expect(aliceFilter.nextElementSibling).toHaveAttribute("data-selection-mode", "included");
    });

    const openFilterDialog = screen.getByRole("dialog", { name: "Filter documents" });
    await user.click(within(openFilterDialog).getByRole("button", { name: "Initiative" }));
    await user.click(await within(openFilterDialog).findByRole("button", { name: "Show children of Initiatives" }));
    await user.click(await within(openFilterDialog).findByRole("checkbox", { name: /344/ }));

    await waitFor(() => {
      const searchCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path.endsWith("/search") && init?.method === "POST");
      const lastRequest = JSON.parse(String(searchCalls.at(-1)?.[1]?.body)) as { filters: { field: string; values: string[] }[] };
      expect(lastRequest.filters).toContainEqual({ field: "category", operator: "IN", values: ["Initiatives/344"] });
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

    expect(screen.queryByRole("button", { name: "Chat results" })).not.toBeInTheDocument();
    await user.click(within(details).getByRole("tab", { name: "Chat" }));
    const chatHistory = within(details).getByRole("combobox", { name: "All search chats" });
    await user.click(chatHistory);
    await user.click(screen.getByRole("option", { name: "Legacy search chat" }));
    await waitFor(() => expect(latestSearchRequest).toMatchObject({ query: "legacy pricing", search_mode: "KEYWORD" }));
    await user.click(within(details).getByRole("combobox", { name: "All search chats" }));
    await user.click(screen.getByRole("option", { name: "Current pricing chat" }));
    await waitFor(() => expect(latestSearchRequest).toMatchObject({ query: "concealed pricing discussion", search_mode: "SEMANTIC", minimum_similarity: 0.035 }));
    await user.click(within(details).getByRole("button", { name: "Rename search chat" }));
    const renameDialog = screen.getByRole("dialog", { name: "Rename search chat" });
    await user.clear(within(renameDialog).getByRole("textbox", { name: "Search chat title" }));
    await user.type(within(renameDialog).getByRole("textbox", { name: "Search chat title" }), "Pricing evidence review");
    await user.click(within(renameDialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(coreApi).toHaveBeenCalledWith("/v1/agent-conversations/conversation-current", expect.objectContaining({ method: "PATCH", body: JSON.stringify({ title: "Pricing evidence review" }) })));
    await user.click(within(details).getByRole("button", { name: "New search chat" }));
    await user.click(within(details).getByRole("button", { name: "Start chat and send" }));
    expect(within(details).getByRole("alert")).toHaveTextContent("Enter your first question before starting the chat.");
    expect(vi.mocked(coreApi).mock.calls.filter(([path, init]) => path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST")).toHaveLength(0);
    const firstQuestion = "What concealed pricing discussions mention discounts and timing?";
    await user.type(within(details).getByRole("textbox", { name: "Starting message Search Chat" }), firstQuestion);
    await user.click(within(details).getByRole("button", { name: "Start chat and send" }));
    await waitFor(() => {
      const createCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/matters/matter-1/agent-conversations" && init?.method === "POST");
      const payload = JSON.parse(String(createCall?.[1]?.body));
      expect(payload).toMatchObject({
        workflow_type: "SEARCH_CHAT",
        title: firstQuestion.slice(0, 50),
        search_definition: {
          query: "concealed pricing discussion",
          search_mode: "SEMANTIC",
          minimum_similarity: 0.035,
          candidate_limit: 1600,
          offset: 0,
          size: 500,
        },
      });
      expect(payload.search_definition.filters).toEqual(expect.arrayContaining([
        expect.objectContaining({ field: "custodian" }),
        expect.objectContaining({ field: "document_date" }),
        expect.objectContaining({ field: "email_sent" }),
      ]));
      const turnCall = vi.mocked(coreApi).mock.calls.find(([path, init]) => path === "/v1/agent-conversations/conversation-search-1/turns" && init?.method === "POST");
      expect(JSON.parse(String(turnCall?.[1]?.body))).toEqual({ message: firstQuestion });
    });
    expect(within(details).getByRole("combobox", { name: "All search chats" })).toBeInTheDocument();
    expect(within(details).queryByRole("textbox", { name: "Starting message Search Chat" })).not.toBeInTheDocument();
    await user.click(within(details).getByRole("tab", { name: "Coding" }));

    expect(within(details).getByRole("button", { name: "Bulk tag results" })).toBeInTheDocument();
    await user.click(within(details).getByRole("button", { name: "Bulk tag results" }));
    expect(screen.getByText(/Active filters narrow this ranked candidate set/i)).toBeInTheDocument();
    const bulkFilterSummary = screen.getByText(/Current filters:/).parentElement;
    expect(bulkFilterSummary).toHaveTextContent("Custodian: 1 selected");
    expect(bulkFilterSummary).toHaveTextContent("Email sent: Range");
    const semanticBulkTagDialog = screen.getByRole("dialog", { name: "Bulk tag search results" });
    await user.click(within(semanticBulkTagDialog).getByRole("combobox", { name: "Value" }));
    await user.click(screen.getByRole("option", { name: "Responsive" }));
    await user.click(within(semanticBulkTagDialog).getByRole("button", { name: "Start bulk tag job" }));
    await waitFor(() => {
      const createCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path === "/v1/matters/matter-1/bulk-tag-jobs" && init?.method === "POST");
      expect(JSON.parse(String(createCalls.at(-1)?.[1]?.body))).toMatchObject({
        candidate_limit: 1600,
        search: { query: "concealed pricing discussion", search_mode: "SEMANTIC", minimum_similarity: 0.035 },
      });
    });
    await user.click(within(screen.getByRole("dialog", { name: "Bulk tag search results" })).getAllByRole("button", { name: /Run in background|Close/ })[0]);

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
      const createCalls = vi.mocked(coreApi).mock.calls.filter(([path, init]) => path === "/v1/matters/matter-1/bulk-tag-jobs" && init?.method === "POST");
      const createCall = createCalls.at(-1);
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
  }, 20_000);
});
