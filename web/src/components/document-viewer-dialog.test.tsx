import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import type { CollectionItemRead } from "@/generated/models";
import { coreApiContent } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApiContent: vi.fn() }));

const collectionItem: CollectionItemRead = {
  id: "item-1",
  tenant_id: "tenant-1",
  client_id: "client-1",
  collection_id: "collection-1",
  source_item_id: "athome4:000001",
  record_type: "EMAIL",
  original_filename: "000001",
  original_extension: null,
  original_source_path: "000/000001",
  source_created_at: null,
  source_modified_at: null,
  file_date: null,
  family_id: null,
  parent_collection_item_id: null,
  processing_status: "READY",
  custodian_ids: ["custodian-1"],
  raw_metadata: {},
  unmapped_metadata: {
    CONTROL_NUMBER: "000001",
    docid_2016: "000001",
    tr2016_labels: ["George W. Bush", "2000 Recount"],
    tr2016_important: [],
    nested_source_value: { source: "TREC", ordinal: 1 },
  },
  created_at: "2026-09-29T12:00:00Z",
  email: {
    sender: "sender@example.com",
    subject: "TREC email",
    sent_at: null,
    received_at: null,
    message_id: null,
    recipients: [],
  },
  native_artifact: {
    id: "artifact-1",
    tenant_id: "tenant-1",
    client_id: "client-1",
    artifact_class: "SOURCE",
    artifact_type: "NATIVE",
    role: "NATIVE",
    original_filename: "000001",
    media_type: "message/rfc822",
    byte_length: 64,
    sha256: "abc",
    status: "FINALIZED",
    created_at: "2026-09-29T12:00:00Z",
    finalized_at: "2026-09-29T12:00:00Z",
    collection_id: "collection-1",
    collection_item_id: "item-1",
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderViewer(showUnmappedMetadata = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DocumentViewerSurface item={collectionItem} showUnmappedMetadata={showUnmappedMetadata} className="h-[40rem]" />
    </QueryClientProvider>,
  );
}

describe("DocumentViewerSurface", () => {
  it("shows unmapped collection metadata in a readable metadata view", async () => {
    vi.mocked(coreApiContent).mockResolvedValue({
      bytes: new TextEncoder().encode("Subject: TREC email\n\nBody"),
      mediaType: "message/rfc822",
    });
    const user = userEvent.setup();
    renderViewer();

    const metadataTab = screen.getByRole("tab", { name: "Metadata" });
    expect(metadataTab).toHaveAttribute("aria-selected", "false");
    await user.click(metadataTab);

    expect(metadataTab).toHaveAttribute("aria-selected", "true");
    const panel = screen.getByRole("region", { name: "Unmapped metadata" });
    expect(within(panel).getByText("docid_2016")).toBeInTheDocument();
    expect(within(panel).getByText("George W. Bush")).toBeInTheDocument();
    expect(within(panel).getByText("2000 Recount")).toBeInTheDocument();
    expect(within(panel).getByText("Empty list")).toBeInTheDocument();
    expect(within(panel).getByText(/"source": "TREC"/)).toBeInTheDocument();
  });

  it("does not add collection metadata controls to other document viewers", () => {
    vi.mocked(coreApiContent).mockResolvedValue({
      bytes: new TextEncoder().encode("Subject: TREC email\n\nBody"),
      mediaType: "message/rfc822",
    });
    renderViewer(false);

    expect(screen.queryByRole("tab", { name: "Metadata" })).not.toBeInTheDocument();
  });
});
