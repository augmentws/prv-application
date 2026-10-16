"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FileDown, LoaderCircle } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { FactFilters, MatterFactExportCreate, MatterFactExportRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

const RECENT_EXPORTS = 5;

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function downloadHref(matterId: string, exportId: string, file: "facts" | "evidence") {
  return `/api/core/v1/matters/${matterId}/fact-exports/${exportId}/download?file=${file}`;
}

function ExportDownloads({ matterId, item, now }: { matterId: string; item: MatterFactExportRead; now: number }) {
  if (Date.parse(item.expires_at) <= now) return <span className="text-xs text-muted-foreground">Expired</span>;
  return (
    <div className="flex flex-wrap gap-2">
      <Button asChild size="sm" variant="outline">
        <a href={downloadHref(matterId, item.id, "facts")} aria-label={`Download facts CSV: ${item.facts_filename}`}><Download />Facts</a>
      </Button>
      <Button asChild size="sm" variant="outline">
        <a href={downloadHref(matterId, item.id, "evidence")} aria-label={`Download evidence CSV: ${item.evidence_filename}`}><Download />Evidence</a>
      </Button>
    </div>
  );
}

/**
 * Export the facts matched by the current filters as two CSV files. The files are written when the export is created,
 * so the dialog offers the downloads straight away; recent exports stay downloadable until they expire.
 */
export function FactExportDialog({ matterId, filters, factCount, defaultFilename = "facts", disabled }: {
  matterId: string;
  filters: FactFilters;
  factCount: number | undefined;
  defaultFilename?: string;
  disabled?: boolean;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [filename, setFilename] = useState(defaultFilename);
  const [spreadsheetSafe, setSpreadsheetSafe] = useState(true);
  // One key per opened dialog, so retrying after a lost response returns the export already written.
  const [idempotencyKey, setIdempotencyKey] = useState("");
  // Expiry is judged against when the dialog was opened.
  const [openedAt, setOpenedAt] = useState(0);
  const recent = useQuery({
    queryKey: ["matter-fact-exports", matterId],
    queryFn: () => coreApi<MatterFactExportRead[]>(`/v1/matters/${matterId}/fact-exports?limit=${RECENT_EXPORTS}`),
    enabled: open,
  });
  const create = useMutation({
    mutationFn: (payload: MatterFactExportCreate) =>
      coreApi<MatterFactExportRead>(`/v1/matters/${matterId}/fact-exports`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["matter-fact-exports", matterId] }),
  });

  function changeOpen(next: boolean) {
    setOpen(next);
    if (next) {
      setFilename(defaultFilename);
      setSpreadsheetSafe(true);
      setIdempotencyKey(crypto.randomUUID());
      setOpenedAt(Date.now());
    } else {
      create.reset();
    }
  }

  function changeOption(apply: () => void) {
    apply();
    // Different options are a different export; a new key keeps the server from treating them as a repeat.
    setIdempotencyKey(crypto.randomUUID());
    create.reset();
  }

  const created = create.data;
  const earlier = (recent.data ?? []).filter((item) => item.id !== created?.id);

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" disabled={disabled}><FileDown />Export CSV</Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Export facts</DialogTitle>
          <DialogDescription>
            {factCount === undefined ? "Export" : `Export the ${factCount.toLocaleString()} ${factCount === 1 ? "fact" : "facts"}`} matched by the current
            filters as two CSV files: one row per fact, and one row per supporting quote. File names record when the export was taken.
          </DialogDescription>
        </DialogHeader>
        {created ? (
          <div className="space-y-3 rounded-lg border border-confirmed/40 bg-confirmed/10 p-3 text-sm" role="status">
            <p className="font-medium">
              Exported {created.fact_count.toLocaleString()} {created.fact_count === 1 ? "fact" : "facts"} and {created.evidence_count.toLocaleString()}{" "}
              {created.evidence_count === 1 ? "quote" : "quotes"}, as of {formatTimestamp(created.snapshot_started_at)}.
            </p>
            <ExportDownloads matterId={matterId} item={created} now={openedAt} />
          </div>
        ) : (
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="fact-export-filename">File name</Label>
              <Input id="fact-export-filename" value={filename} maxLength={150} onChange={(event) => changeOption(() => setFilename(event.target.value))} />
            </div>
            <label className="flex cursor-pointer items-start gap-2 text-sm">
              <input type="checkbox" className="mt-0.5 size-4 accent-primary" checked={spreadsheetSafe} onChange={(event) => changeOption(() => setSpreadsheetSafe(event.target.checked))} />
              <span>
                <span className="font-medium">Protect against spreadsheet formulas</span>
                <span className="block text-muted-foreground">Cells that start with =, +, - or @ are prefixed with an apostrophe so spreadsheets show them as text.</span>
              </span>
            </label>
          </div>
        )}
        {create.error ? <p role="alert" className="text-sm text-destructive">{create.error.message}</p> : null}
        {earlier.length ? (
          <section className="space-y-2" aria-label="Recent exports">
            <h3 className="text-sm font-semibold">Recent exports</h3>
            <ul className="divide-y rounded-lg border text-sm">
              {earlier.map((item) => (
                <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                  <div className="min-w-0">
                    <p className="truncate font-medium">{item.requested_filename}</p>
                    <p className="text-xs text-muted-foreground">{item.fact_count.toLocaleString()} facts · as of {formatTimestamp(item.snapshot_started_at)}</p>
                  </div>
                  <ExportDownloads matterId={matterId} item={item} now={openedAt} />
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {recent.error ? <p role="alert" className="text-sm text-destructive">Recent exports could not be loaded: {recent.error.message}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => changeOpen(false)}>Close</Button>
          {created ? null : (
            <Button
              type="button"
              disabled={!filename.trim() || !idempotencyKey || create.isPending}
              onClick={() => create.mutate({ filters, requested_filename: filename.trim(), spreadsheet_safe: spreadsheetSafe, idempotency_key: idempotencyKey })}
            >
              {create.isPending ? <LoaderCircle className="animate-spin" /> : <FileDown />}
              {create.isPending ? "Writing files…" : "Create export"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
