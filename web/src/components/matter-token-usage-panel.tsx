import { ArrowDown, ArrowUp, Database, Hash, Send } from "lucide-react";

import type {
  MatterProviderUsageReportRead,
  ProviderUsageJobBreakdownRead,
  ProviderUsageModelBreakdownRead,
} from "@/generated/models";
import { Button } from "@/components/ui/button";
import { formatDate } from "@/lib/format";

function count(value: number) {
  return value.toLocaleString();
}

function jobTypeLabel(value: string) {
  return value.toLowerCase().split("_").map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
}

function UsageMetric({ icon: Icon, label, value, detail }: {
  icon: typeof Hash;
  label: string;
  value: number;
  detail?: string;
}) {
  return <div className="rounded-xl border bg-card p-4 shadow-xs">
    <div className="flex items-center gap-2 text-sm font-medium text-muted-foreground"><Icon className="size-4 text-primary" />{label}</div>
    <p className="mt-2 text-2xl font-semibold tabular-nums tracking-tight">{count(value)}</p>
    {detail ? <p className="mt-1 text-xs text-muted-foreground">{detail}</p> : null}
  </div>;
}

function BreakdownTable({ rows, kind }: {
  rows: ProviderUsageModelBreakdownRead[] | ProviderUsageJobBreakdownRead[];
  kind: "model" | "job";
}) {
  return <div className="overflow-x-auto rounded-xl border bg-card">
    <table className="w-full min-w-[760px] text-sm">
      <thead className="border-b bg-muted/35 text-left text-xs uppercase tracking-wide text-muted-foreground"><tr><th className="px-4 py-3">{kind === "model" ? "Provider / model" : "Workflow"}</th><th className="px-4 py-3 text-right">Requests</th><th className="px-4 py-3 text-right">Input</th><th className="px-4 py-3 text-right">Cached input</th><th className="px-4 py-3 text-right">Cache writes</th><th className="px-4 py-3 text-right">Output</th><th className="px-4 py-3 text-right">Total</th></tr></thead>
      <tbody className="divide-y">{rows.map((row) => {
        const key = kind === "model" ? `${(row as ProviderUsageModelBreakdownRead).provider}:${(row as ProviderUsageModelBreakdownRead).model}` : (row as ProviderUsageJobBreakdownRead).job_type;
        const label = kind === "model" ? <><span className="font-semibold">{(row as ProviderUsageModelBreakdownRead).model}</span><span className="block text-xs text-muted-foreground">{(row as ProviderUsageModelBreakdownRead).provider}</span></> : <span className="font-semibold">{jobTypeLabel((row as ProviderUsageJobBreakdownRead).job_type)}</span>;
        return <tr key={key}><td className="px-4 py-3">{label}</td><td className="px-4 py-3 text-right tabular-nums">{count(row.request_count)}</td><td className="px-4 py-3 text-right tabular-nums">{count(row.input_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(row.cached_input_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(row.cache_write_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(row.output_tokens)}</td><td className="px-4 py-3 text-right font-semibold tabular-nums">{count(row.total_tokens)}</td></tr>;
      })}</tbody>
    </table>
  </div>;
}

export function MatterTokenUsagePanel({ report, loading, onPageChange }: {
  report: MatterProviderUsageReportRead;
  loading: boolean;
  onPageChange: (offset: number) => void;
}) {
  const totals = report.totals;
  const previousOffset = Math.max(0, report.entries_offset - report.entries_limit);
  const nextOffset = report.entries_offset + report.entries.length;
  const hasPrevious = report.entries_offset > 0;
  const hasNext = nextOffset < totals.record_count;
  return <div className="space-y-8">
    <div><h2 className="text-lg font-semibold">Token usage</h2><p className="text-sm text-muted-foreground">All external model usage attributed to this matter, from the immutable provider-usage ledger.</p></div>

    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
      <UsageMetric icon={Hash} label="Total tokens" value={totals.total_tokens} detail="Input plus output" />
      <UsageMetric icon={ArrowDown} label="Input tokens" value={totals.input_tokens} />
      <UsageMetric icon={ArrowUp} label="Output tokens" value={totals.output_tokens} />
      <UsageMetric icon={Database} label="Cached input" value={totals.cached_input_tokens} detail="Reported subset of input" />
      <UsageMetric icon={Send} label="Provider requests" value={totals.request_count} detail={`${count(totals.record_count)} ledger entries`} />
    </div>

    <div className="rounded-lg border border-dashed bg-muted/20 px-4 py-3 text-sm text-muted-foreground">These are provider-reported token costs. Cached input and cache-write tokens are shown separately and are not added again to total tokens. Dollar estimates are not shown because the ledger does not yet preserve a versioned provider price snapshot.</div>

    <section className="space-y-3"><div><h3 className="font-semibold">By provider and model</h3><p className="text-sm text-muted-foreground">All-time usage, ordered by total tokens.</p></div>{report.by_model.length ? <BreakdownTable rows={report.by_model} kind="model" /> : <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">No provider usage has been recorded for this matter.</p>}</section>

    {report.by_job_type.length ? <section className="space-y-3"><div><h3 className="font-semibold">By workflow</h3><p className="text-sm text-muted-foreground">See which matter operations consumed the tokens.</p></div><BreakdownTable rows={report.by_job_type} kind="job" /></section> : null}

    <section className="space-y-3"><div><h3 className="font-semibold">Usage ledger</h3><p className="text-sm text-muted-foreground">{report.entries.length < totals.record_count ? `Latest ${count(report.entries.length)} of ${count(totals.record_count)} entries.` : `${count(totals.record_count)} total entries.`}</p></div>
      {report.entries.length ? <><div className="overflow-x-auto rounded-xl border bg-card"><table className="w-full min-w-[980px] text-sm"><thead className="border-b bg-muted/35 text-left text-xs uppercase tracking-wide text-muted-foreground"><tr><th className="px-4 py-3">Date</th><th className="px-4 py-3">Workflow</th><th className="px-4 py-3">Provider / model</th><th className="px-4 py-3">Started by</th><th className="px-4 py-3 text-right">Input</th><th className="px-4 py-3 text-right">Cached</th><th className="px-4 py-3 text-right">Cache writes</th><th className="px-4 py-3 text-right">Output</th><th className="px-4 py-3 text-right">Total</th></tr></thead><tbody className="divide-y">{report.entries.map((entry) => <tr key={entry.id}><td className="whitespace-nowrap px-4 py-3 text-muted-foreground">{formatDate(entry.job_created_at)}</td><td className="px-4 py-3"><span className="font-medium">{jobTypeLabel(entry.job_type)}</span><span className="block font-mono text-[10px] text-muted-foreground">{entry.job_id}</span></td><td className="px-4 py-3"><span className="font-medium">{entry.model}</span><span className="block text-xs text-muted-foreground">{entry.provider}</span></td><td className="px-4 py-3"><span>{entry.started_by_display_name}</span><span className="block text-xs text-muted-foreground">{entry.started_by_email}</span></td><td className="px-4 py-3 text-right tabular-nums">{count(entry.input_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(entry.cached_input_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(entry.cache_write_tokens)}</td><td className="px-4 py-3 text-right tabular-nums">{count(entry.output_tokens)}</td><td className="px-4 py-3 text-right font-semibold tabular-nums">{count(entry.total_tokens)}</td></tr>)}</tbody></table></div><div className="flex items-center justify-between gap-3"><p className="text-xs text-muted-foreground">Showing {count(report.entries_offset + 1)}–{count(report.entries_offset + report.entries.length)} of {count(totals.record_count)}</p><div className="flex gap-2"><Button type="button" variant="outline" size="sm" disabled={!hasPrevious || loading} onClick={() => onPageChange(previousOffset)}>Previous</Button><Button type="button" variant="outline" size="sm" disabled={!hasNext || loading} onClick={() => onPageChange(nextOffset)}>Next</Button></div></div></> : <p className="rounded-xl border border-dashed p-6 text-center text-sm text-muted-foreground">The ledger is empty for this matter.</p>}
    </section>
  </div>;
}
