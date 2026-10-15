"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, FileSearch } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useMemo, useState } from "react";

import { QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { JobOutcomeItemPage, JobOutcomeManifestRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

const PAGE_SIZE = 100;
const STATUS_LABELS: Record<string, string> = {
  SUCCEEDED: "Generated",
  REUSED: "Reused",
  SKIPPED: "Skipped",
  FAILED: "Failed",
  EXCLUDED: "Excluded",
  CANCELED: "Canceled",
};

export function JobOutcomesDialog({
  matterId,
  jobType,
  jobId,
  jobName,
  trigger,
  initialStatus = "ALL",
  open: controlledOpen,
  onOpenChange,
}: {
  matterId: string;
  jobType: string;
  jobId: string;
  jobName: string;
  trigger?: ReactNode | null;
  initialStatus?: string;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [statusFilter, setStatusFilter] = useState(initialStatus);
  const [reasonFilter, setReasonFilter] = useState("ALL");
  const [offset, setOffset] = useState(0);
  const open = controlledOpen ?? internalOpen;
  const basePath = `/v1/matters/${matterId}/job-outcomes/${jobType}/${jobId}`;
  const summary = useQuery({
    queryKey: ["job-outcomes", jobType, jobId],
    queryFn: () => coreApi<JobOutcomeManifestRead>(basePath),
    enabled: open,
  });
  const query = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE) });
  if (statusFilter !== "ALL") query.set("status", statusFilter);
  if (reasonFilter !== "ALL") query.set("reason_code", reasonFilter);
  const items = useQuery({
    queryKey: ["job-outcome-items", jobType, jobId, statusFilter, reasonFilter, offset],
    queryFn: () => coreApi<JobOutcomeItemPage>(`${basePath}/items?${query}`),
    enabled: open && summary.data?.item_details_available === true,
  });
  const reasons = useMemo(
    () => Object.entries(summary.data?.reason_counts ?? {}).sort((left, right) => right[1] - left[1]),
    [summary.data?.reason_counts],
  );

  const changeOpen = (nextOpen: boolean) => {
    if (controlledOpen === undefined) setInternalOpen(nextOpen);
    onOpenChange?.(nextOpen);
    if (!nextOpen) {
      setStatusFilter(initialStatus);
      setReasonFilter("ALL");
      setOffset(0);
    }
  };
  const changeStatus = (value: string) => {
    setStatusFilter(value);
    setOffset(0);
  };
  const changeReason = (value: string) => {
    setReasonFilter(value);
    setOffset(0);
  };

  return <Dialog open={open} onOpenChange={changeOpen}>
    {trigger === undefined ? <DialogTrigger asChild><Button type="button" size="sm" variant="outline"><FileSearch />Outcomes</Button></DialogTrigger> : trigger ? <DialogTrigger asChild>{trigger}</DialogTrigger> : null}
    <DialogContent className="max-w-6xl">
      <DialogHeader>
        <DialogTitle>Document outcomes</DialogTitle>
        <DialogDescription>{jobName}. Complete item diagnostics are stored as a compressed Artifact-backed Parquet manifest.</DialogDescription>
      </DialogHeader>
      {summary.isPending ? <TableLoading /> : null}
      {summary.error ? <QueryError message={summary.error.message} /> : null}
      {summary.data ? <>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
          {Object.entries(summary.data.status_counts).map(([status, count]) => <button
            key={status}
            type="button"
            className={`rounded-lg border p-3 text-left transition hover:border-primary ${statusFilter === status ? "border-primary bg-primary/5" : ""}`}
            onClick={() => changeStatus(statusFilter === status ? "ALL" : status)}
            disabled={!summary.data.item_details_available}
          >
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{STATUS_LABELS[status] ?? status}</p>
            <p className="mt-1 text-2xl font-semibold tabular-nums">{count.toLocaleString()}</p>
          </button>)}
        </div>
        {summary.data.legacy ? <p className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">This historical job retains aggregate counts and bounded errors only. Exact document-level outcomes are unavailable.</p> : null}
        {summary.data.failure_summary ? <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{summary.data.failure_summary}</p> : null}
        {summary.data.item_details_available ? <>
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div className="flex flex-wrap gap-3">
              <div className="w-44"><p className="mb-1 text-xs font-medium text-muted-foreground">Outcome</p><Select value={statusFilter} onValueChange={changeStatus}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ALL">All outcomes</SelectItem>{Object.keys(summary.data.status_counts).map((value) => <SelectItem key={value} value={value}>{STATUS_LABELS[value] ?? value}</SelectItem>)}</SelectContent></Select></div>
              <div className="w-72"><p className="mb-1 text-xs font-medium text-muted-foreground">Reason</p><Select value={reasonFilter} onValueChange={changeReason}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ALL">All reasons</SelectItem>{reasons.map(([value, count]) => <SelectItem key={value} value={value}>{value} · {count.toLocaleString()}</SelectItem>)}</SelectContent></Select></div>
            </div>
            <div className="flex gap-2"><Button asChild size="sm" variant="outline"><a href={`/api/core${basePath}/download?format=csv`}><Download />CSV</a></Button><Button asChild size="sm" variant="outline"><a href={`/api/core${basePath}/download?format=parquet`}><Download />Parquet</a></Button></div>
          </div>
          {items.isPending ? <TableLoading /> : null}
          {items.error ? <QueryError message={items.error.message} /> : null}
          {items.data ? <>
            <Table>
              <TableHeader><TableRow><TableHead>Document</TableHead><TableHead>Outcome</TableHead><TableHead>Reason</TableHead><TableHead>Details</TableHead><TableHead>Attempt</TableHead></TableRow></TableHeader>
              <TableBody>{items.data.items.map((item) => <TableRow key={`${item.ordinal}-${item.item_id}`}>
                <TableCell><Link className="font-medium text-primary hover:underline" href={`/review/matters/${matterId}?document=${item.item_id}`}>Open document</Link><p className="max-w-48 truncate font-mono text-xs text-muted-foreground" title={item.item_id}>{item.item_id}</p></TableCell>
                <TableCell><Badge variant="outline">{STATUS_LABELS[item.outcome_status] ?? item.outcome_status}</Badge></TableCell>
                <TableCell><code className="text-xs">{item.reason_code}</code></TableCell>
                <TableCell><p className="max-w-md text-sm">{item.message ?? `${Number(item.metrics.chunk_count ?? 0).toLocaleString()} chunks`}</p></TableCell>
                <TableCell className="tabular-nums">{item.attempt_number}</TableCell>
              </TableRow>)}</TableBody>
            </Table>
            {!items.data.items.length ? <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">No documents match these filters.</p> : null}
            <div className="flex items-center justify-between"><p className="text-sm text-muted-foreground">{items.data.total.toLocaleString()} matching documents</p><div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</Button><Button type="button" size="sm" variant="outline" disabled={offset + PAGE_SIZE >= items.data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button></div></div>
          </> : null}
        </> : null}
      </> : null}
    </DialogContent>
  </Dialog>;
}
