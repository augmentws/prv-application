import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SearchIndexPanel } from "@/components/search-index-panel";
import type { SearchIndexGenerationRead, SearchProjectionOperationRead } from "@/generated/models";

const activeIndex: SearchIndexGenerationRead = {
  id: "generation-2",
  matter_id: "matter-1",
  generation: 2,
  index_name: "matter-1-v2",
  alias_name: "matter-1",
  schema_hash: "1234567890abcdef",
  status: "ACTIVE",
  document_count: 12,
  error_message: null,
  activated_at: "2026-09-14T12:00:00Z",
  created_at: "2026-09-14T11:00:00Z",
  updated_at: "2026-09-14T12:00:00Z",
};

const completedOperation: SearchProjectionOperationRead = {
  id: "operation-1",
  matter_id: "matter-1",
  kind: "REBUILD",
  payload: {},
  status: "COMPLETED",
  workflow_id: "workflow-1",
  created_by_user_id: "user-1",
  attempt_count: 1,
  error_message: null,
  started_at: "2026-09-14T11:00:00Z",
  completed_at: "2026-09-14T12:00:00Z",
  created_at: "2026-09-14T11:00:00Z",
  updated_at: "2026-09-14T12:00:00Z",
};

afterEach(cleanup);

describe("SearchIndexPanel", () => {
  it("shows a ready active generation and matching document counts", () => {
    render(<SearchIndexPanel coreDocumentCount={12} indexes={[activeIndex]} operations={[completedOperation]} onRebuild={vi.fn()} rebuilding={false} onConfirmReindex={vi.fn()} confirmingReindex={false} onRetryFailed={vi.fn()} retryingFailed={false} />);

    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "#2" })).toBeInTheDocument();
    expect(screen.getByText("Index and Core counts match.")).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Full rebuild" })).toBeInTheDocument();
  });

  it("confirms before requesting a full rebuild", async () => {
    const user = userEvent.setup();
    const onRebuild = vi.fn().mockResolvedValue(undefined);
    render(<SearchIndexPanel coreDocumentCount={12} indexes={[activeIndex]} operations={[]} onRebuild={onRebuild} rebuilding={false} onConfirmReindex={vi.fn()} confirmingReindex={false} onRetryFailed={vi.fn()} retryingFailed={false} />);

    await user.click(screen.getByRole("button", { name: "Rebuild entire index" }));
    expect(screen.getByRole("heading", { name: "Rebuild the search index?" })).toBeInTheDocument();
    expect(screen.getByText(/current active index remains searchable/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start rebuild" }));
    await waitFor(() => expect(onRebuild).toHaveBeenCalledOnce());
  });

  it("asks for confirmation when a schema sync requires reindexing", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    const awaiting = {
      ...completedOperation,
      id: "operation-awaiting",
      kind: "SCHEMA_SYNC",
      status: "AWAITING_USER",
      payload: { schema_change: { action: "REINDEX_REQUIRED", reasons: ["Existing field mappings changed: metadata."] } },
      completed_at: null,
    } satisfies SearchProjectionOperationRead;
    render(<SearchIndexPanel coreDocumentCount={12} indexes={[activeIndex]} operations={[awaiting]} onRebuild={vi.fn()} rebuilding={false} onConfirmReindex={onConfirm} confirmingReindex={false} onRetryFailed={vi.fn()} retryingFailed={false} />);

    expect(screen.getAllByText("Action required")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Review and confirm" }));
    await user.click(screen.getByRole("button", { name: "Confirm full reindex" }));
    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("operation-awaiting"));
  });

  it("confirms before requeueing failed document-update jobs", async () => {
    const user = userEvent.setup();
    const onRetryFailed = vi.fn().mockResolvedValue(undefined);
    const failed = {
      ...completedOperation,
      id: "operation-failed",
      kind: "DOCUMENT_UPSERT",
      status: "FAILED",
      payload: { document_ids: ["document-1", "document-2"] },
      error_message: "unknown encoding: windows-3839",
      completed_at: null,
    } satisfies SearchProjectionOperationRead;
    render(<SearchIndexPanel coreDocumentCount={12} indexes={[activeIndex]} operations={[failed]} onRebuild={vi.fn()} rebuilding={false} onConfirmReindex={vi.fn()} confirmingReindex={false} onRetryFailed={onRetryFailed} retryingFailed={false} />);

    await user.click(screen.getByRole("button", { name: "Requeue failed jobs" }));
    expect(screen.getByText(/1 failed or interrupted job covering 2 documents/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm requeue" }));

    await waitFor(() => expect(onRetryFailed).toHaveBeenCalledOnce());
  });
});
