import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobFailedDocumentsDialog } from "@/components/job-failed-documents-dialog";
import type { JobExecutionRead, JobFailedDocumentPage, MatterQuestionAnsweringJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("JobFailedDocumentsDialog", () => {
  it("shows persisted reasons and retries only the selected retryable document", async () => {
    const job = {
      id: "run-1",
      review_batch_id: "batch-1",
      review_batch_name: "QC batch",
      task_name: "Issue review",
      failed_document_count: 2,
      retryable_failed_document_count: 1,
    } as MatterQuestionAnsweringJobRead;
    const page: JobFailedDocumentPage = {
      job_type: "ANALYSIS_TASK_BATCH_RUN",
      job_id: "run-1",
      total: 2,
      retryable_count: 1,
      offset: 0,
      limit: 100,
      items: [
        {
          matter_document_id: "document-1",
          source_collection_id: "collection-1",
          collection_item_id: "item-1",
          failure_code: "OPENAI_DECISIONS_HTTP_503",
          failure_message: "Provider temporarily unavailable",
          failure_retriable: true,
          attempt_number: 1,
          failed_at: "2026-10-13T12:00:00Z",
        },
        {
          matter_document_id: "document-2",
          source_collection_id: "collection-1",
          collection_item_id: "item-2",
          failure_code: "ANALYSIS_DOCUMENT_CONFLICT",
          failure_message: "Document has no faithful text source",
          failure_retriable: false,
          attempt_number: 1,
          failed_at: "2026-10-13T12:00:00Z",
        },
      ],
    };
    const execution = { id: "execution-2", status: "QUEUED", progress_total: 1 } as JobExecutionRead;
    vi.mocked(coreApi).mockImplementation(async (_path, init) => (init?.method === "POST" ? execution : page) as never);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

    render(<QueryClientProvider client={queryClient}><JobFailedDocumentsDialog matterId="matter-1" job={job} /></QueryClientProvider>);
    await userEvent.click(screen.getByRole("button", { name: "2 failed" }));

    expect(await screen.findByText("Provider temporarily unavailable")).toBeInTheDocument();
    expect(screen.getByText("Document has no faithful text source")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Retry document document-2" })).toBeDisabled();

    await userEvent.click(screen.getByRole("checkbox", { name: "Retry document document-1" }));
    await userEvent.click(screen.getByRole("button", { name: "Retry selected (1)" }));

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith(
      "/v1/jobs/ANALYSIS_TASK_BATCH_RUN/run-1/failed-documents/retry",
      { method: "POST", body: JSON.stringify({ document_ids: ["document-1"] }) },
    ));
  });
});
