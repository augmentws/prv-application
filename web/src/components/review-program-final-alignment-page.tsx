"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, GitCompareArrows } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { ReviewProgramFinalAlignmentPageRead, ReviewProgramRunRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref } from "@/lib/matter-routes";

const PAGE_SIZE = 50;

export function ReviewProgramFinalAlignmentPage({
  clientId,
  matterId,
  programId,
  runId,
}: {
  clientId: string;
  matterId: string;
  programId: string;
  runId: string;
}) {
  const [offset, setOffset] = useState(0);
  const run = useQuery({
    queryKey: ["review-program-run", matterId, programId, runId],
    queryFn: () => coreApi<ReviewProgramRunRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}`),
  });
  const output = useQuery({
    queryKey: ["review-program-final-alignment", matterId, programId, runId, offset],
    queryFn: () => coreApi<ReviewProgramFinalAlignmentPageRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/final-alignment?offset=${offset}&limit=${PAGE_SIZE}`),
  });
  const reportRaw = run.data?.progress_summary?.final_alignment;
  const report = reportRaw && typeof reportRaw === "object" && !Array.isArray(reportRaw) ? reportRaw as Record<string, unknown> : {};
  const alignmentRaw = report.alignment_counts;
  const alignment = alignmentRaw && typeof alignmentRaw === "object" && !Array.isArray(alignmentRaw) ? alignmentRaw as Record<string, unknown> : {};
  const issues = Array.isArray(report.issues)
    ? report.issues.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item)))
    : [];
  const minimumConfidence = typeof report.minimum_confidence === "number" ? report.minimum_confidence : output.data?.minimum_confidence;

  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="mb-2 flex items-center gap-2 text-sm text-muted-foreground"><GitCompareArrows className="size-4" />Review Program analysis</div>
        <h2 className="text-xl font-semibold">Classifier and decision-model comparison</h2>
        <p className="mt-1 max-w-4xl text-sm text-muted-foreground">The frozen Step 15 population compares the accepted classifier with {output.data?.profile_a_name ?? "the first Q&A model"} and {output.data?.profile_b_name ?? "the second Q&A model"}. Agreement measures consistency among the three coding sources, not accuracy.</p>
      </div>
      <Button asChild size="sm" variant="outline"><Link href={matterHref(clientId, matterId, "review-programs", { program: programId, run: runId })}><ArrowLeft />Back to Review Program</Link></Button>
    </div>

    {run.isLoading || output.isLoading ? <TableLoading /> : run.error ? <QueryError message={run.error.message} /> : output.error ? <QueryError message={output.error.message} /> : output.data ? <>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
        <Metric label={minimumConfidence != null ? `Classifier-positive at or above ${(minimumConfidence * 100).toFixed(0)}%` : "Classifier-positive population"} value={Number(report.population_document_count ?? output.data.total)} />
        <Metric label="All three exactly agree" value={Number(alignment.EXACT_ALL ?? 0)} />
        <Metric label="Q&A models agree; classifier differs" value={Number(alignment.QA_PAIR_EXACT ?? 0)} />
        <Metric label="Some coding values overlap" value={Number(alignment.PARTIAL ?? 0)} />
        <Metric label="No coding values overlap" value={Number(alignment.DISAGREEMENT ?? 0)} />
        <Metric label="Incomplete comparison" value={Number(alignment.INCOMPLETE ?? 0)} />
      </div>

      <AlignmentDistributionChart alignment={alignment} population={Number(report.population_document_count ?? output.data.total)} />

      {issues.length ? <Card><CardHeader><CardTitle>Issue-level comparison</CardTitle><CardDescription>Positive assignments by the classifier, each named Q&amp;A model, both Q&amp;A models together, and all three sources together.</CardDescription></CardHeader><div className="overflow-x-auto border-t"><Table><TableHeader><TableRow><TableHead>Issue</TableHead><TableHead className="text-right">Classifier</TableHead><TableHead className="text-right">{output.data.profile_a_name}</TableHead><TableHead className="text-right">{output.data.profile_b_name}</TableHead><TableHead className="text-right">Both Q&amp;A models</TableHead><TableHead className="text-right">All three</TableHead></TableRow></TableHeader><TableBody>{issues.map((issue) => {
        const key = String(issue.issue_key ?? "Issue");
        return <TableRow key={key}><TableCell className="font-medium">{issueLabel(key)}</TableCell><Count value={issue.classifier} /><Count value={issue.profile_a} /><Count value={issue.profile_b} /><Count value={issue.qa_pair} /><Count value={issue.all_three} /></TableRow>;
      })}</TableBody></Table></div></Card> : null}

      <Card>
        <CardHeader><CardTitle>Document comparison</CardTitle><CardDescription>Frozen document-level codings retained by the final-alignment manifest. Showing {output.data.offset + 1}–{Math.min(output.data.offset + output.data.items.length, output.data.total)} of {output.data.total.toLocaleString()}.</CardDescription></CardHeader>
        <div className="overflow-x-auto border-t"><Table><TableHeader><TableRow><TableHead>Document</TableHead><TableHead>Classifier</TableHead><TableHead>{output.data.profile_a_name}</TableHead><TableHead>{output.data.profile_b_name}</TableHead><TableHead>Alignment</TableHead><TableHead>Source</TableHead></TableRow></TableHeader><TableBody>{output.data.items.map((item) => <TableRow key={item.matter_document_id}><TableCell><p className="max-w-72 truncate font-medium">{item.filename ?? `Document ${item.scope_ordinal.toLocaleString()}`}</p><p className="font-mono text-xs text-muted-foreground">{item.matter_document_id}</p></TableCell><CodingList values={item.classifier_codings} /><CodingList values={item.profile_a_codings} /><CodingList values={item.profile_b_codings} /><TableCell><Badge variant="outline">{friendly(item.alignment_status)}</Badge></TableCell><TableCell>{friendly(item.source_status)}</TableCell></TableRow>)}</TableBody></Table></div>
        <div className="flex items-center justify-between gap-3 border-t p-4"><Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft />Previous</Button><p className="text-sm tabular-nums text-muted-foreground">Page {Math.floor(offset / PAGE_SIZE) + 1} of {Math.max(1, Math.ceil(output.data.total / PAGE_SIZE))}</p><Button variant="outline" size="sm" disabled={offset + PAGE_SIZE >= output.data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next<ChevronRight /></Button></div>
      </Card>
    </> : null}
  </div>;
}

const ALIGNMENT_COHORTS = [
  { key: "EXACT_ALL", label: "All three exactly agree", color: "#10b981" },
  { key: "QA_PAIR_EXACT", label: "Q&A models agree; classifier differs", color: "#38bdf8" },
  { key: "PARTIAL", label: "Some coding values overlap", color: "#a78bfa" },
  { key: "DISAGREEMENT", label: "No coding values overlap", color: "#f43f5e" },
  { key: "INCOMPLETE", label: "Incomplete comparison", color: "#94a3b8" },
] as const;

function AlignmentDistributionChart({ alignment, population }: { alignment: Record<string, unknown>; population: number }) {
  const data = ALIGNMENT_COHORTS.map((cohort) => {
    const count = Number(alignment[cohort.key] ?? 0);
    return { ...cohort, count, percentage: population > 0 ? count / population : 0 };
  });
  return <Card aria-label="Alignment distribution chart">
    <CardHeader><CardTitle>Agreement distribution</CardTitle><CardDescription>How the classifier and both decision models compare across the frozen Step 15 population. Percentages use {population.toLocaleString()} documents as the denominator.</CardDescription></CardHeader>
    <CardContent>
      <div className="h-80 w-full">
        <ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 960, height: 320 }}>
          <BarChart data={data} layout="vertical" margin={{ top: 8, right: 30, bottom: 8, left: 12 }} accessibilityLayer>
            <CartesianGrid horizontal={false} stroke="var(--border)" strokeOpacity={0.7} />
            <XAxis type="number" allowDecimals={false} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
            <YAxis type="category" dataKey="label" width={240} interval={0} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
            <Tooltip formatter={(value, _name, item) => [`${Number(value).toLocaleString()} (${(Number(item.payload.percentage) * 100).toFixed(1)}%)`, "Documents"]} />
            <Bar dataKey="count" radius={[0, 4, 4, 0]} maxBarSize={28} isAnimationActive={false}>{data.map((cohort) => <Cell key={cohort.key} fill={cohort.color} />)}</Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2 xl:grid-cols-5">{data.map((cohort) => <div key={cohort.key} className="flex items-start gap-2 rounded-md border p-2"><span className="mt-0.5 size-2.5 shrink-0 rounded-sm" style={{ backgroundColor: cohort.color }} /><div><p className="font-medium">{cohort.label}</p><p className="text-muted-foreground">{cohort.count.toLocaleString()} · {(cohort.percentage * 100).toFixed(1)}%</p></div></div>)}</div>
    </CardContent>
  </Card>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return <Card><CardHeader className="pb-2"><CardDescription>{label}</CardDescription><CardTitle className="text-2xl tabular-nums">{value.toLocaleString()}</CardTitle></CardHeader></Card>;
}

function Count({ value }: { value: unknown }) {
  return <TableCell className="text-right tabular-nums">{Number(value ?? 0).toLocaleString()}</TableCell>;
}

function CodingList({ values }: { values: Array<{ key: string; label: string }> }) {
  return <TableCell>{values.length ? <div className="flex max-w-72 flex-wrap gap-1">{values.map((value) => <Badge key={value.key} variant="outline">{value.label}</Badge>)}</div> : <span className="text-muted-foreground">No issue coding</span>}</TableCell>;
}

function issueLabel(key: string) {
  return friendly(key.includes(":") ? key.split(":").at(-1)! : key);
}

function friendly(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
