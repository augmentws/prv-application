"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, FileDown, LoaderCircle, Search, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { MatterSearchExportCreate, MatterSearchExportRead, MatterSearchRequest, ReviewBatchSearchRequest, SearchExportFieldRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

const DEFAULT_FIELD_IDS = ["document_id", "original_filename", "record_type", "custodian_names", "email_subject"];

export function SearchExportDialog({ matterId, search, resultCount, disabled, createPath, scopeLabel, onCreated }: {
  matterId: string;
  search: MatterSearchRequest | ReviewBatchSearchRequest;
  resultCount: number;
  disabled?: boolean;
  createPath?: string;
  scopeLabel?: string;
  onCreated?: (job: MatterSearchExportRead) => void;
}) {
  const [open, setOpen] = useState(false);
  const [fieldQuery, setFieldQuery] = useState("");
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [filename, setFilename] = useState("search-results.csv");
  const initializedForOpen = useRef(false);
  const fields = useQuery({
    queryKey: ["search-export-fields", matterId],
    queryFn: () => coreApi<SearchExportFieldRead[]>(`/v1/matters/${matterId}/search-export-fields`),
    enabled: open,
  });
  const selected = selectedIds.map((id) => fields.data?.find((field) => field.id === id)).filter((field): field is SearchExportFieldRead => Boolean(field));
  const available = useMemo(() => {
    const needle = fieldQuery.trim().toLocaleLowerCase();
    return (fields.data ?? []).filter((field) => field.selectable && !selectedIds.includes(field.id) && (
      !needle || field.display_name.toLocaleLowerCase().includes(needle) || field.stable_key.toLocaleLowerCase().includes(needle)
    ));
  }, [fieldQuery, fields.data, selectedIds]);
  const grouped = useMemo(() => {
    const result = new Map<string, SearchExportFieldRead[]>();
    for (const field of available) {
      const group = field.group_display_name ?? "Matter metadata";
      result.set(group, [...(result.get(group) ?? []), field]);
    }
    return result;
  }, [available]);
  const mutation = useMutation({
    mutationFn: (payload: MatterSearchExportCreate) => coreApi<MatterSearchExportRead>(createPath ?? `/v1/matters/${matterId}/search-exports`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
    onSuccess: (job) => {
      toast.success("Search export queued.");
      setOpen(false);
      onCreated?.(job);
    },
  });

  useEffect(() => {
    if (!open || !fields.data || initializedForOpen.current) return;
    const selectable = new Set(fields.data.filter((field) => field.selectable).map((field) => field.id));
    setSelectedIds(DEFAULT_FIELD_IDS.filter((id) => selectable.has(id)));
    initializedForOpen.current = true;
  }, [fields.data, open]);

  const initialize = (next: boolean) => {
    setOpen(next);
    if (!next) {
      setFieldQuery("");
      initializedForOpen.current = false;
      mutation.reset();
    }
  };
  const toggle = (id: string) => setSelectedIds((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id]);
  const move = (index: number, direction: -1 | 1) => setSelectedIds((current) => {
    const target = index + direction;
    if (target < 0 || target >= current.length) return current;
    const next = [...current];
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  });
  const create = () => mutation.mutate({
    search: { ...search, offset: 0, size: 1, facets: [] },
    field_ids: selectedIds,
    spreadsheet_safe: true,
    requested_filename: filename,
    idempotency_key: crypto.randomUUID(),
  });
  const searchMode = search.search_mode ?? "KEYWORD";
  const bounded = searchMode !== "KEYWORD";
  const hasLargeValue = selected.some((field) => field.large_value);

  return (
    <Dialog open={open} onOpenChange={initialize}>
      <DialogTrigger asChild><Button variant="outline" size="sm" disabled={disabled}><FileDown />Export results</Button></DialogTrigger>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Export search results</DialogTitle>
          <DialogDescription>{bounded
            ? `Export the current bounded ${searchMode.toLocaleLowerCase()} result set${scopeLabel ? ` from ${scopeLabel}` : ""} of up to ${(search.candidate_limit ?? resultCount).toLocaleString()} candidates.`
            : `Export all ${resultCount.toLocaleString()} documents matched by the current search${scopeLabel ? ` in ${scopeLabel}` : ""}.`}</DialogDescription>
        </DialogHeader>
        <div className="grid min-h-0 gap-5 md:grid-cols-2">
          <section className="space-y-3">
            <div className="space-y-2"><Label htmlFor="search-export-filename">CSV filename</Label><Input id="search-export-filename" value={filename} onChange={(event) => setFilename(event.target.value)} maxLength={255} /></div>
            <div className="space-y-2">
              <Label htmlFor="search-export-field-query">Available fields</Label>
              <div className="relative"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" /><Input id="search-export-field-query" value={fieldQuery} onChange={(event) => setFieldQuery(event.target.value)} className="pl-9" placeholder="Find a field…" /></div>
            </div>
            <div className="max-h-72 space-y-4 overflow-y-auto rounded-lg border p-3">
              {fields.isPending ? <p className="text-sm text-muted-foreground">Loading fields…</p> : null}
              {fields.error ? <p role="alert" className="text-sm text-destructive">{fields.error.message}</p> : null}
              {[...grouped.entries()].map(([group, groupFields]) => <div key={group}><p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{group}</p>{groupFields.map((field) => <button type="button" key={field.id} onClick={() => toggle(field.id)} className="flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"><span>{field.display_name}</span><span className="text-xs text-muted-foreground">{field.value_type.toLocaleLowerCase()}</span></button>)}</div>)}
              {!fields.isPending && !available.length ? <p className="text-sm text-muted-foreground">No additional fields match.</p> : null}
            </div>
          </section>
          <section className="space-y-3">
            <div><p className="text-sm font-medium">CSV columns</p><p className="text-xs text-muted-foreground">Columns are written in this order.</p></div>
            <div className="max-h-[22rem] space-y-1 overflow-y-auto rounded-lg border p-2">
              {selected.map((field, index) => <div key={field.id} className="flex items-center gap-2 rounded-md bg-muted/50 px-2 py-2"><span className="min-w-0 flex-1 truncate text-sm font-medium">{field.display_name}</span>{field.large_value ? <span className="text-xs text-warning">large</span> : null}<Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Move ${field.display_name} up`} disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp /></Button><Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Move ${field.display_name} down`} disabled={index === selected.length - 1} onClick={() => move(index, 1)}><ArrowDown /></Button><Button type="button" variant="ghost" size="icon" className="size-8" aria-label={`Remove ${field.display_name}`} onClick={() => toggle(field.id)}><X /></Button></div>)}
              {!selected.length ? <p className="p-4 text-center text-sm text-muted-foreground">Choose at least one field.</p> : null}
            </div>
            {hasLargeValue ? <p className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm">Document text or another long-text field is selected. The export may be substantially larger.</p> : null}
            <p className="text-xs text-muted-foreground">The export runs as a durable job. Follow progress and download the completed CSV from this matter’s Search exports job history.</p>
          </section>
        </div>
        {mutation.error ? <p role="alert" className="text-sm text-destructive">{mutation.error.message}</p> : null}
        <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="button" disabled={!selectedIds.length || !filename.trim() || mutation.isPending} onClick={create}>{mutation.isPending ? <LoaderCircle className="animate-spin" /> : <FileDown />}{mutation.isPending ? "Queuing…" : "Create export"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
