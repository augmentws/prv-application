import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchExportDialog } from "@/components/search-export-dialog";
import type { MatterSearchExportRead, MatterSearchRequest, ReviewBatchSearchRequest, SearchExportFieldRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

const search: MatterSearchRequest = {
  query: "price fixing",
  search_mode: "KEYWORD",
  filters: [{ field: "responsiveness", operator: "IN", values: ["responsive"] }],
  facets: ["responsiveness"],
  offset: 50,
  size: 50,
};

const fields: SearchExportFieldRead[] = [
  { id: "document_id", stable_key: "document_id", display_name: "Matter document ID", group_display_name: "Document fields", source_path: "document_id", value_type: "TEXT", cardinality: "SINGLE", selectable: true, large_value: false, enum_values: [] },
  { id: "original_filename", stable_key: "original_filename", display_name: "Original filename", group_display_name: "Document fields", source_path: "original_filename", value_type: "TEXT", cardinality: "SINGLE", selectable: true, large_value: false, enum_values: [] },
  { id: "body_text", stable_key: "body_text", display_name: "Document text", group_display_name: "Document fields", source_path: "body_text", value_type: "LONG_TEXT", cardinality: "SINGLE", selectable: true, large_value: true, enum_values: [] },
  { id: "metadata-1", stable_key: "responsiveness", display_name: "Responsiveness", group_display_name: "Review", source_path: "metadata.responsiveness", value_type: "ENUM", cardinality: "SINGLE", selectable: true, large_value: false, enum_values: [{ key: "responsive", label: "Responsive" }] },
];

const queuedJob: MatterSearchExportRead = {
  id: "export-1", matter_id: "matter-1", search_index_generation_id: "generation-1", requested_filename: "review.csv",
  status: "QUEUED", current_stage: "QUEUED", matched_count: 0, exported_count: 0, page_count: 0, selected_field_count: 3,
  output_artifact_id: null, output_content_hash: null, output_byte_length: null, output_filename: null, snapshot_started_at: null,
  expires_at: null, failure_code: null, failure_summary: null, failure_retriable: false, created_by_user_id: "user-1",
  created_by_display_name: "Review User", created_by_email: "reviewer@example.com", started_at: null, completed_at: null,
  created_at: "2026-10-13T12:00:00Z", updated_at: "2026-10-13T12:00:00Z", execution: null,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(onCreated = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    onCreated,
    ...render(<QueryClientProvider client={queryClient}><SearchExportDialog matterId="matter-1" search={search} resultCount={126} onCreated={onCreated} /></QueryClientProvider>),
  };
}

describe("SearchExportDialog", () => {
  it("submits the current search with ordered selectable fields", async () => {
    let submitted: unknown;
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/search-export-fields") return fields as never;
      if (path === "/v1/matters/matter-1/search-exports" && init?.method === "POST") {
        submitted = JSON.parse(String(init.body));
        return queuedJob as never;
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    const { onCreated } = renderDialog();

    await user.click(screen.getByRole("button", { name: "Export results" }));
    const dialog = screen.getByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("Matter document ID")).toBeInTheDocument());
    await user.click(within(dialog).getByRole("button", { name: /Responsiveness/ }));
    await user.clear(within(dialog).getByLabelText("CSV filename"));
    await user.type(within(dialog).getByLabelText("CSV filename"), "review.csv");
    await user.click(within(dialog).getByRole("button", { name: "Create export" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(queuedJob));
    expect(submitted).toEqual(expect.objectContaining({
      requested_filename: "review.csv",
      spreadsheet_safe: true,
      field_ids: ["document_id", "original_filename", "metadata-1"],
      search: expect.objectContaining({ query: "price fixing", offset: 0, size: 1, facets: [] }),
    }));
  });

  it("warns when document text is selected", async () => {
    vi.mocked(coreApi).mockResolvedValue(fields as never);
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByRole("button", { name: "Export results" }));
    const dialog = screen.getByRole("dialog");
    await user.click(await within(dialog).findByRole("button", { name: /Document text/ }));

    expect(within(dialog).getByText(/export may be substantially larger/i)).toBeInTheDocument();
  });

  it("submits batch-only search fields to a batch export endpoint", async () => {
    let submitted: unknown;
    const batchSearch: ReviewBatchSearchRequest = {
      ...search,
      coding_filters: [{ field: "responsiveness", values: ["responsive"], minimum_confidence: 0.8 }],
    };
    vi.mocked(coreApi).mockImplementation(async (path, init) => {
      if (path === "/v1/matters/matter-1/search-export-fields") return fields as never;
      if (path === "/v1/matters/matter-1/review-batches/batch-1/search-exports?topic_key=topic-1" && init?.method === "POST") {
        submitted = JSON.parse(String(init.body));
        return queuedJob as never;
      }
      throw new Error(`Unexpected API request: ${path}`);
    });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(<QueryClientProvider client={queryClient}><SearchExportDialog matterId="matter-1" search={batchSearch} resultCount={12} createPath="/v1/matters/matter-1/review-batches/batch-1/search-exports?topic_key=topic-1" scopeLabel="batch “Sample”" /></QueryClientProvider>);

    await user.click(screen.getByRole("button", { name: "Export results" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/matched by the current search in batch “Sample”/)).toBeInTheDocument();
    await within(dialog).findByText("Matter document ID");
    await user.click(within(dialog).getByRole("button", { name: "Create export" }));

    await waitFor(() => expect(submitted).toEqual(expect.objectContaining({
      search: expect.objectContaining({
        coding_filters: [{ field: "responsiveness", values: ["responsive"], minimum_confidence: 0.8 }],
      }),
    })));
  });
});
