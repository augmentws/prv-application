"use client";

import { useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronRight, CircleHelp, Download, LoaderCircle } from "lucide-react";
import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { QueryError } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type {
  ReviewBatchCodingComparisonRead,
  ReviewBatchComparisonOptionsRead,
  ReviewBatchComparisonRequest,
  ReviewBatchComparisonRequestRowFilter,
  ReviewBatchComparisonSourceOptionRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 50;
const RUN_COLORS = ["#38bdf8", "#f59e0b", "#34d399", "#f472b6", "#a78bfa", "#fb7185"];

function percent(value: number | null | undefined) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function shortLabel(value: string) {
  return value.length > 28 ? `${value.slice(0, 26)}…` : value;
}

function runTitle(source: ReviewBatchComparisonSourceOptionRead) {
  const type = source.source_type === "REVIEWER" ? "reviewer" : source.run_type?.toLowerCase() ?? "evaluation";
  return `${source.label} · ${type} · ${formatDate(source.created_at)}`;
}

function agreementClass(value: number | null | undefined) {
  if (value == null) return "bg-muted text-muted-foreground";
  if (value >= 0.9) return "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300";
  if (value >= 0.7) return "bg-amber-500/20 text-amber-700 dark:text-amber-300";
  return "bg-destructive/15 text-destructive";
}

function JaccardHelp() {
  return (
    <span
      tabIndex={0}
      aria-label="About Jaccard similarity"
      className="group relative inline-flex rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <CircleHelp aria-hidden="true" className="size-3.5" />
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full right-0 z-50 mb-2 hidden w-80 max-w-[calc(100vw-2rem)] rounded-md border bg-popover px-3 py-2 text-left text-xs font-normal leading-5 text-popover-foreground shadow-lg group-hover:block group-focus:block"
      >
        Pairwise Jaccard measures values selected by both sources divided by values selected by either source (intersection ÷ union). The all-source per-value overlap is documents where every selected source chose the value divided by documents where at least one selected source chose it.
      </span>
    </span>
  );
}

function ComparisonCountsChart({ comparison }: { comparison: ReviewBatchCodingComparisonRead }) {
  const runMap = new Map(comparison.sources.map((source) => [source.id, source]));
  const chartData = useMemo(() => {
    const rows = new Map<string, Record<string, string | number>>();
    for (const summary of comparison.source_summaries) {
      for (const count of summary.value_counts) {
        const row = rows.get(count.value_key) ?? { key: count.value_key, label: count.display_label, total: 0 };
        row[summary.source_id] = count.document_count;
        row.total = Number(row.total) + count.document_count;
        rows.set(count.value_key, row);
      }
    }
    return [...rows.values()].sort((left, right) => Number(right.total) - Number(left.total)).slice(0, 40);
  }, [comparison.source_summaries]);
  const chartHeight = Math.max(260, chartData.length * 34);

  if (!chartData.length) return <p className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">The selected runs contain no values for this field.</p>;
  return <section className="rounded-lg border p-4">
    <h3 className="font-semibold">Coding counts</h3>
    <p className="mt-1 text-xs text-muted-foreground">Distinct documents per value and source. Multi-value documents can contribute to more than one bar.</p>
    <div className="mt-4 w-full" style={{ height: chartHeight }} aria-label="Coding counts by value and source">
      <ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 960, height: chartHeight }}>
        <BarChart data={chartData} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 12 }} accessibilityLayer>
          <CartesianGrid horizontal={false} stroke="var(--border)" strokeOpacity={0.7} />
          <XAxis type="number" allowDecimals={false} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
          <YAxis type="category" dataKey="label" width={190} interval={0} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} tickFormatter={(value) => shortLabel(String(value))} />
          <Tooltip formatter={(value, name) => [Number(value).toLocaleString(), runMap.get(String(name))?.label ?? String(name)]} />
          <Legend formatter={(value) => runMap.get(String(value))?.label ?? String(value)} />
          {comparison.sources.map((source, index) => <Bar key={source.id} dataKey={source.id} fill={RUN_COLORS[index % RUN_COLORS.length]} radius={[0, 3, 3, 0]} maxBarSize={18} isAnimationActive={false} />)}
        </BarChart>
      </ResponsiveContainer>
    </div>
    {comparison.source_summaries.some((summary) => summary.value_counts_truncated) ? <p className="mt-2 text-xs text-amber-600">The API returned the 200 most frequent values for at least one source; the export retains every document value.</p> : null}
    {chartData.length === 40 ? <p className="mt-2 text-xs text-muted-foreground">Showing the 40 most frequent values across the selected sources.</p> : null}
  </section>;
}

function PairwiseAgreement({ comparison }: { comparison: ReviewBatchCodingComparisonRead }) {
  const runMap = new Map(comparison.sources.map((source) => [source.id, source]));
  const valueLabels = new Map(comparison.source_summaries.flatMap((summary) => summary.value_counts.map((value) => [value.value_key, value.display_label] as const)));
  valueLabels.set("__NO_VALUE__", "No value");
  const pairMap = new Map<string, (typeof comparison.pairwise_agreements)[number]>(comparison.pairwise_agreements.flatMap((pair) => [
    [`${pair.left_source_id}:${pair.right_source_id}`, pair] as const,
    [`${pair.right_source_id}:${pair.left_source_id}`, pair] as const,
  ]));
  const [selectedPairKey, setSelectedPairKey] = useState(comparison.pairwise_agreements[0] ? `${comparison.pairwise_agreements[0].left_source_id}:${comparison.pairwise_agreements[0].right_source_id}` : "");
  const selectedPair = pairMap.get(selectedPairKey) ?? comparison.pairwise_agreements[0];

  return <section className="rounded-lg border p-4">
    <h3 className="font-semibold">Pairwise agreement</h3>
    <p className="mt-1 text-xs text-muted-foreground">Exact set agreement among documents evaluated by both sources. This measures agreement, not accuracy.</p>
    <div className="mt-4 overflow-x-auto">
      <table className="min-w-full border-separate border-spacing-1 text-xs">
        <thead><tr><th className="p-2 text-left text-muted-foreground">Source</th>{comparison.sources.map((source) => <th key={source.id} className="max-w-32 p-2 text-left" title={runTitle(source)}>{shortLabel(source.label)}</th>)}</tr></thead>
        <tbody>{comparison.sources.map((left) => <tr key={left.id}><th className="max-w-40 p-2 text-left font-medium" title={runTitle(left)}>{shortLabel(left.label)}</th>{comparison.sources.map((right) => {
          if (left.id === right.id) return <td key={right.id} className="rounded p-2 text-center bg-muted">100%</td>;
          const pair = pairMap.get(`${left.id}:${right.id}`);
          return <td key={right.id} className={cn("cursor-pointer rounded p-2 text-center font-semibold", agreementClass(pair?.exact_agreement))} onClick={() => pair && setSelectedPairKey(`${pair.left_source_id}:${pair.right_source_id}`)} title={`${pair?.compared_document_count.toLocaleString() ?? 0} documents evaluated by both`}>{percent(pair?.exact_agreement)}</td>;
        })}</tr>)}</tbody>
      </table>
    </div>
    {selectedPair ? <div className="mt-4 border-t pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium">{runMap.get(selectedPair.left_source_id)?.label} ↔ {runMap.get(selectedPair.right_source_id)?.label}</p>
        <div className="flex flex-wrap gap-2 text-xs"><Badge variant="outline">{selectedPair.compared_document_count.toLocaleString()} compared</Badge><Badge variant="outline">{selectedPair.incomplete_document_count.toLocaleString()} incomplete</Badge><Badge variant="outline">Jaccard {percent(selectedPair.mean_jaccard)} <JaccardHelp /></Badge></div>
      </div>
      {selectedPair.confusion_matrix.length ? <div className="mt-3"><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Confusion matrix</p><Table><TableHeader><TableRow><TableHead>{runMap.get(selectedPair.left_source_id)?.label} value</TableHead><TableHead>{runMap.get(selectedPair.right_source_id)?.label} value</TableHead><TableHead className="text-right">Documents</TableHead></TableRow></TableHeader><TableBody>{selectedPair.confusion_matrix.map((cell) => <TableRow key={`${cell.left_value}:${cell.right_value}`}><TableCell>{valueLabels.get(cell.left_value) ?? cell.left_value}</TableCell><TableCell>{valueLabels.get(cell.right_value) ?? cell.right_value}</TableCell><TableCell className="text-right tabular-nums">{cell.document_count.toLocaleString()}</TableCell></TableRow>)}</TableBody></Table></div> : null}
    </div> : null}
  </section>;
}

function MultiSourceOverlap({ comparison }: { comparison: ReviewBatchCodingComparisonRead }) {
  const sourceMap = new Map(comparison.sources.map((source) => [source.id, source]));
  const agreement = comparison.multi_source_agreement;

  if (!agreement?.class_agreement.length) return null;

  return <section className="rounded-lg border p-4">
    <h3 className="font-semibold">Per-value overlap</h3>
    <p className="mt-1 text-xs text-muted-foreground">
      Includes all {comparison.sources.length} selected sources and only documents evaluated by every selected source. Source columns count documents assigned the value; All selected ÷ Any selected is the all-source overlap.
    </p>
    <div className="mt-3 flex flex-wrap gap-2 text-xs">
      <Badge variant="outline">{agreement.compared_document_count.toLocaleString()} compared</Badge>
      <Badge variant="outline">{agreement.incomplete_document_count.toLocaleString()} incomplete</Badge>
    </div>
    <div className="mt-3 overflow-x-auto">
      <Table>
        <TableHeader><TableRow>
          <TableHead className="sticky left-0 min-w-44 bg-card">Value</TableHead>
          {comparison.sources.map((source) => <TableHead key={source.id} className="min-w-36 text-right" title={runTitle(source)}>{source.label}</TableHead>)}
          <TableHead className="min-w-28 text-right">All {comparison.sources.length}</TableHead>
          <TableHead className="min-w-28 text-right">Any selected</TableHead>
          <TableHead className="min-w-24"><span className="flex items-center justify-end gap-1">Overlap <JaccardHelp /></span></TableHead>
        </TableRow></TableHeader>
        <TableBody>{agreement.class_agreement.map((item) => {
          const counts = new Map(item.source_counts.map((count) => [count.source_id, count.document_count]));
          return <TableRow key={item.value_key}>
            <TableCell className="sticky left-0 bg-card font-medium">{item.display_label}</TableCell>
            {comparison.sources.map((source) => <TableCell key={source.id} className="text-right tabular-nums" title={sourceMap.get(source.id)?.label}>{(counts.get(source.id) ?? 0).toLocaleString()}</TableCell>)}
            <TableCell className="text-right tabular-nums">{item.all_selected_count.toLocaleString()}</TableCell>
            <TableCell className="text-right tabular-nums">{item.any_selected_count.toLocaleString()}</TableCell>
            <TableCell className="text-right tabular-nums">{percent(item.jaccard)}</TableCell>
          </TableRow>;
        })}</TableBody>
      </Table>
    </div>
  </section>;
}

export function BatchCodingComparison({ matterId, batchId, active }: { matterId: string; batchId: string; active: boolean }) {
  const [fieldId, setFieldId] = useState("");
  const [selectedSourceIds, setSelectedSourceIds] = useState<string[] | null>(null);
  const [rowFilter, setRowFilter] = useState<ReviewBatchComparisonRequestRowFilter>("ALL");
  const [offset, setOffset] = useState(0);
  const options = useQuery({
    queryKey: ["review-batch-comparison-options", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchComparisonOptionsRead>(`/v1/matters/${matterId}/review-batches/${batchId}/comparison-options`),
    enabled: active,
  });
  const effectiveFieldId = fieldId || options.data?.fields[0]?.id || "";
  const visibleSources = (options.data?.sources ?? []).filter((source) => (
    source.source_type === "REVIEWER"
    || (
      ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(source.status ?? "")
      && source.metadata_definition_ids.includes(effectiveFieldId)
    )
  )) ?? [];
  const completedSources = visibleSources.filter((source) => source.source_type === "REVIEWER" || ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(source.status ?? ""));
  const defaultSourceIds = (completedSources.length >= 2 ? completedSources : visibleSources).slice(0, 2).map((source) => source.id);
  const effectiveSourceIds = selectedSourceIds ?? defaultSourceIds;

  const request: ReviewBatchComparisonRequest | null = effectiveFieldId && effectiveSourceIds.length >= 2 ? {
    metadata_definition_id: effectiveFieldId,
    source_ids: effectiveSourceIds,
    row_filter: rowFilter,
    offset,
    limit: PAGE_SIZE,
  } : null;
  const comparison = useQuery({
    queryKey: ["review-batch-coding-comparison", matterId, batchId, request],
    queryFn: () => coreApi<ReviewBatchCodingComparisonRead>(`/v1/matters/${matterId}/review-batches/${batchId}/coding-comparison`, { method: "POST", body: JSON.stringify(request) }),
    enabled: active && request !== null,
  });
  const sourceMap = new Map(visibleSources.map((source) => [source.id, source]));

  const toggleRun = (sourceId: string) => {
    setOffset(0);
    setSelectedSourceIds((current) => {
      const selected = current ?? effectiveSourceIds;
      return selected.includes(sourceId) ? selected.filter((id) => id !== sourceId) : selected.length < 6 ? [...selected, sourceId] : selected;
    });
  };
  const exportUrl = (format: "csv" | "parquet") => {
    const params = new URLSearchParams({ metadata_definition_id: effectiveFieldId, row_filter: rowFilter, format });
    for (const sourceId of effectiveSourceIds) params.append("source_id", sourceId);
    return `/api/core/v1/matters/${matterId}/review-batches/${batchId}/coding-comparison/export?${params.toString()}`;
  };

  if (options.isPending) return <div className="space-y-3"><Skeleton className="h-24 w-full" /><Skeleton className="h-64 w-full" /></div>;
  if (options.error) return <QueryError message={options.error.message} />;
  if (!visibleSources.length) return <p className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">No reviewer coding or completed analysis runs exist for this batch yet.</p>;

  return <div className="space-y-4">
    <section className="rounded-lg border p-4">
      <div className="grid gap-4 lg:grid-cols-[minmax(14rem,0.7fr)_minmax(0,1.3fr)]">
        <label className="space-y-1.5 text-sm font-medium">Coding field<Select value={effectiveFieldId} onValueChange={(value) => { setFieldId(value); setOffset(0); }}><SelectTrigger aria-label="Comparison coding field"><SelectValue placeholder="Select a field" /></SelectTrigger><SelectContent>{options.data.fields.map((field) => <SelectItem key={field.id} value={field.id}>{field.display_name} · {field.cardinality.toLowerCase()}</SelectItem>)}</SelectContent></Select></label>
        <div><p className="text-sm font-medium">Reviewers and completed analysis runs</p><p className="mb-2 text-xs text-muted-foreground">Select 2–6 coding sources. Unfinished, unsuccessful, and zero-value analysis runs are hidden.</p><div className="grid gap-2 sm:grid-cols-2">{visibleSources.map((source) => { const selected = effectiveSourceIds.includes(source.id); const sourceType = source.source_type === "REVIEWER" ? "reviewer" : source.run_type?.toLowerCase() ?? "evaluation"; const sourceStatus = source.source_type === "REVIEWER" ? "current coding" : source.status?.toLowerCase().replaceAll("_", " "); return <button key={source.id} type="button" onClick={() => toggleRun(source.id)} className={cn("flex min-w-0 items-start gap-2 rounded-md border px-3 py-2 text-left text-sm", selected ? "border-primary bg-primary/10" : "hover:bg-muted/50", !selected && effectiveSourceIds.length >= 6 && "cursor-not-allowed opacity-60")} title={runTitle(source)}><span className={cn("mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border", selected && "border-primary bg-primary text-primary-foreground")}>{selected ? <Check className="size-3" /> : null}</span><span className="min-w-0"><span className="block truncate font-medium">{source.label}</span><span className="block truncate text-xs text-muted-foreground">{sourceType} · {sourceStatus}{source.detail ? ` · ${source.detail}` : ""}</span></span></button>; })}</div></div>
      </div>
      {effectiveSourceIds.length < 2 ? <p className="mt-3 text-sm text-amber-600">Select at least two result sets to compare.</p> : null}
    </section>

    {comparison.isPending && request ? <div className="flex items-center justify-center gap-2 rounded-lg border p-12 text-sm text-muted-foreground"><LoaderCircle className="animate-spin" />Comparing coding across the frozen batch…</div> : null}
    {comparison.error ? <QueryError message={comparison.error.message} /> : null}
    {comparison.data ? <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{comparison.data.source_summaries.map((summary) => <div key={summary.source_id} className="rounded-lg border bg-muted/20 p-3"><p className="truncate text-sm font-semibold" title={sourceMap.get(summary.source_id)?.label}>{sourceMap.get(summary.source_id)?.label}</p><p className="mt-1 text-xs tabular-nums text-muted-foreground">{summary.evaluated_document_count.toLocaleString()} evaluated · {summary.no_value_document_count.toLocaleString()} no value</p><p className="mt-0.5 text-xs tabular-nums text-muted-foreground">{summary.skipped_document_count.toLocaleString()} skipped · {summary.failed_document_count.toLocaleString()} failed · {(summary.incomplete_document_count + summary.not_evaluated_document_count).toLocaleString()} incomplete</p></div>)}</div>
      <ComparisonCountsChart comparison={comparison.data} />
      <PairwiseAgreement comparison={comparison.data} />
      <MultiSourceOverlap comparison={comparison.data} />
      <section className="rounded-lg border p-4">
        <div className="flex flex-wrap items-end justify-between gap-3"><div><h3 className="font-semibold">Document matrix</h3><p className="mt-1 text-xs text-muted-foreground">One frozen batch document per row and one selected result set per column.</p></div><div className="flex flex-wrap items-center gap-2"><Select value={rowFilter} onValueChange={(value) => { setRowFilter(value as ReviewBatchComparisonRequestRowFilter); setOffset(0); }}><SelectTrigger className="w-40" aria-label="Document agreement filter"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ALL">All documents</SelectItem><SelectItem value="DISAGREEMENT">Disagreements</SelectItem><SelectItem value="AGREEMENT">Exact agreement</SelectItem><SelectItem value="INCOMPLETE">Incomplete</SelectItem></SelectContent></Select><Button asChild size="sm" variant="outline"><a href={exportUrl("csv")} download><Download />CSV</a></Button><Button asChild size="sm" variant="outline"><a href={exportUrl("parquet")} download><Download />Parquet</a></Button></div></div>
        <div className="mt-3"><Table><TableHeader><TableRow><TableHead className="sticky left-0 bg-muted">Document</TableHead><TableHead>Status</TableHead>{comparison.data.sources.map((source) => <TableHead key={source.id} className="min-w-48" title={runTitle(source)}>{source.label}</TableHead>)}</TableRow></TableHeader><TableBody>{comparison.data.documents.map((document) => <TableRow key={document.matter_document_id}><TableCell className="sticky left-0 bg-card font-medium tabular-nums">#{document.sequence_number}<span className="ml-2 font-mono text-xs text-muted-foreground">{document.matter_document_id.slice(0, 8)}</span></TableCell><TableCell><Badge variant={document.agreement_status === "EXACT" ? "accent" : "outline"}>{document.agreement_status.toLowerCase()}</Badge></TableCell>{document.sources.map((source) => <TableCell key={source.source_id}><span className={cn(source.state !== "EVALUATED" && "text-muted-foreground")}>{source.state === "EVALUATED" ? source.display_values.length ? source.display_values.join(" · ") : "No value" : source.state.toLowerCase().replaceAll("_", " ")}</span>{source.top_confidence != null ? <span className="ml-2 text-xs tabular-nums text-muted-foreground">{Math.round(source.top_confidence * 100)}%</span> : null}</TableCell>)}</TableRow>)}</TableBody></Table></div>
        {!comparison.data.documents.length ? <p className="py-8 text-center text-sm text-muted-foreground">No documents match this agreement filter.</p> : null}
        <div className="mt-3 flex items-center justify-between gap-3"><p className="text-xs tabular-nums text-muted-foreground">{comparison.data.filtered_document_count.toLocaleString()} matching documents{comparison.data.filtered_document_count ? ` · ${offset + 1}–${Math.min(offset + PAGE_SIZE, comparison.data.filtered_document_count)}` : ""}</p><div className="flex gap-2"><Button type="button" size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft />Previous</Button><Button type="button" size="sm" variant="outline" disabled={offset + PAGE_SIZE >= comparison.data.filtered_document_count} onClick={() => setOffset(offset + PAGE_SIZE)}>Next<ChevronRight /></Button></div></div>
      </section>
    </> : null}
  </div>;
}
