"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RotateCcw } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import type { JobExecutionRead, JobFailedDocumentPage, MatterQuestionAnsweringJobRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const PAGE_SIZE = 100;

export function JobFailedDocumentsDialog({
  matterId,
  job,
  trigger,
  open: controlledOpen,
  onOpenChange,
}: {
  matterId: string;
  job: MatterQuestionAnsweringJobRead;
  trigger?: ReactNode | null;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const failures = useQuery({
    queryKey: ["job-failed-documents", job.id, offset],
    queryFn: () => coreApi<JobFailedDocumentPage>(
      `/v1/jobs/ANALYSIS_TASK_BATCH_RUN/${job.id}/failed-documents?offset=${offset}&limit=${PAGE_SIZE}`,
    ),
    enabled: open,
  });
  const retry = useMutation({
    mutationFn: (documentIds: string[] | null) => coreApi<JobExecutionRead>(
      `/v1/jobs/ANALYSIS_TASK_BATCH_RUN/${job.id}/failed-documents/retry`,
      { method: "POST", body: JSON.stringify({ document_ids: documentIds }) },
    ),
    onSuccess: async (execution) => {
      setSelected(new Set());
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["job-failed-documents", job.id] }),
        queryClient.invalidateQueries({ queryKey: ["matter-question-answering-jobs", matterId] }),
      ]);
      toast.success(`${execution.progress_total?.toLocaleString() ?? "Selected"} failed document${execution.progress_total === 1 ? "" : "s"} queued for retry.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The failed documents could not be retried."),
  });
  const retryableOnPage = useMemo(
    () => failures.data?.items.filter((item) => item.failure_retriable).map((item) => item.matter_document_id) ?? [],
    [failures.data],
  );
  const allPageSelected = retryableOnPage.length > 0 && retryableOnPage.every((id) => selected.has(id));

  const toggleDocument = (documentId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(documentId)) next.delete(documentId);
      else next.add(documentId);
      return next;
    });
  };
  const togglePage = () => setSelected(allPageSelected ? new Set() : new Set(retryableOnPage));
  const total = failures.data?.total ?? job.failed_document_count ?? 0;
  const retryableCount = failures.data?.retryable_count ?? job.retryable_failed_document_count ?? 0;

  const changePage = (nextOffset: number) => {
    setSelected(new Set());
    setOffset(nextOffset);
  };
  const changeOpen = (nextOpen: boolean) => {
    if (controlledOpen === undefined) setInternalOpen(nextOpen);
    onOpenChange?.(nextOpen);
    if (!nextOpen) {
      setSelected(new Set());
      setOffset(0);
    }
  };

  return <Dialog open={open} onOpenChange={changeOpen}>
    {trigger === undefined ? <DialogTrigger asChild>
      <Button type="button" size="sm" variant="outline">
        <AlertTriangle />{(job.failed_document_count ?? 0).toLocaleString()} failed
      </Button>
    </DialogTrigger> : trigger ? <DialogTrigger asChild>{trigger}</DialogTrigger> : null}
    <DialogContent className="max-w-5xl">
      <DialogHeader>
        <DialogTitle>Failed documents</DialogTitle>
        <DialogDescription>
          {job.task_name} on {job.review_batch_name}. Successful documents will not run again.
        </DialogDescription>
      </DialogHeader>

      {failures.isPending ? <p className="py-8 text-center text-sm text-muted-foreground">Loading document failures…</p> : null}
      {failures.error ? <p className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{failures.error.message}</p> : null}
      {failures.data ? <>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground">
            <span className="font-semibold text-foreground">{total.toLocaleString()}</span> failed · <span className="font-semibold text-foreground">{retryableCount.toLocaleString()}</span> retryable
          </p>
          <div className="flex flex-wrap gap-2">
            {retryableOnPage.length ? <Button type="button" size="sm" variant="outline" onClick={togglePage}>{allPageSelected ? "Clear page" : "Select retryable on page"}</Button> : null}
            {retryableCount ? <Button type="button" size="sm" variant="outline" disabled={retry.isPending} onClick={() => retry.mutate(null)}><RotateCcw />Retry all retryable</Button> : null}
          </div>
        </div>

        {failures.data.items.length ? <div className="max-h-[56vh] overflow-auto rounded-md border">
          <table className="w-full text-left text-sm">
            <thead className="sticky top-0 bg-muted text-xs text-muted-foreground">
              <tr><th className="w-10 px-3 py-2"><span className="sr-only">Select</span></th><th className="px-3 py-2">Document</th><th className="px-3 py-2">Reason</th><th className="px-3 py-2">Attempt</th><th className="px-3 py-2">Failed</th></tr>
            </thead>
            <tbody className="divide-y">
              {failures.data.items.map((item) => <tr key={item.matter_document_id} className="align-top">
                <td className="px-3 py-3"><input type="checkbox" aria-label={`Retry document ${item.matter_document_id}`} checked={selected.has(item.matter_document_id)} disabled={!item.failure_retriable || retry.isPending} onChange={() => toggleDocument(item.matter_document_id)} className="size-4 accent-primary" /></td>
                <td className="px-3 py-3"><Link className="font-medium text-primary hover:underline" href={`/review/matters/${matterId}?batch=${job.review_batch_id}&document=${item.matter_document_id}`}>Open document</Link><p className="mt-1 max-w-52 truncate font-mono text-xs text-muted-foreground" title={item.collection_item_id}>{item.collection_item_id}</p></td>
                <td className="px-3 py-3"><div className="flex flex-wrap items-center gap-2"><Badge variant={item.failure_retriable ? "default" : "outline"}>{item.failure_retriable ? "Retryable" : "Not retryable"}</Badge><span className="font-mono text-xs text-muted-foreground">{item.failure_code}</span></div><p className="mt-1 max-w-xl whitespace-pre-wrap break-words">{item.failure_message}</p></td>
                <td className="px-3 py-3 tabular-nums">{item.attempt_number ?? "—"}</td>
                <td className="whitespace-nowrap px-3 py-3 text-muted-foreground">{item.failed_at ? formatDate(item.failed_at) : "—"}</td>
              </tr>)}
            </tbody>
          </table>
        </div> : <p className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">No failed documents remain for this job.</p>}

        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">Showing {failures.data.items.length ? offset + 1 : 0}–{Math.min(offset + failures.data.items.length, total)} of {total.toLocaleString()}</p>
          <div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={offset === 0} onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}>Previous</Button><Button type="button" size="sm" variant="outline" disabled={offset + PAGE_SIZE >= total} onClick={() => changePage(offset + PAGE_SIZE)}>Next</Button></div>
        </div>
      </> : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={() => changeOpen(false)}>Close</Button>
        <Button type="button" disabled={!selected.size || retry.isPending} onClick={() => retry.mutate([...selected])}><RotateCcw />Retry selected ({selected.size})</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
