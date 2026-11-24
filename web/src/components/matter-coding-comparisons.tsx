"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
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
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type {
  MatterCodingComparisonCreate,
  MatterCodingComparisonDocumentPage,
  MatterCodingComparisonFieldOptionRead,
  MatterCodingComparisonOptionsRead,
  MatterCodingComparisonRead,
  MatterCodingComparisonSource,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

type MappingState = Record<string, Record<string, string>>;
type CanonicalValueOption = { key: string; label: string };
const SOURCE_COLORS = ["#38bdf8", "#f59e0b", "#34d399", "#f472b6", "#a78bfa", "#fb7185"];

type ComparisonSourceSummary = {
  source_id: string;
  label: string;
  source_type: string;
  state_counts: Record<string, number>;
  value_counts: Record<string, number>;
  evaluated_document_count?: number;
  no_value_document_count?: number;
  value_counts_truncated?: boolean;
};

type ComparisonPair = {
  left_source_id: string;
  right_source_id: string;
  compared_document_count: number;
  incomplete_document_count?: number;
  agreement_counts: Record<string, number>;
  exact_agreement: number | null;
  mean_jaccard?: number | null;
  confusion_matrix: Array<{ left_value: string; right_value: string; document_count: number }>;
  confusion_matrix_truncated?: boolean;
};

type ComparisonStatistics = {
  document_count?: number;
  agreement_counts?: Record<string, number>;
  sources?: ComparisonSourceSummary[];
  pairwise?: ComparisonPair[];
  multi_source?: {
    compared_document_count: number;
    incomplete_document_count: number;
    class_agreement: Array<{
      value_key: string;
      source_counts: Array<{ source_id: string; document_count: number }>;
      all_selected_count: number;
      any_selected_count: number;
      jaccard: number | null;
    }>;
    class_agreement_truncated?: boolean;
  };
};

function displayValue(value: string) {
  if (value === "__NO_VALUE__") return "No value";
  return value.replaceAll("_", " ").toLowerCase().replace(/^./, (letter) => letter.toUpperCase());
}

function percent(value: number | null | undefined) {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

function agreementClass(value: number | null | undefined) {
  if (value == null) return "bg-muted text-muted-foreground";
  if (value >= 0.9) return "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300";
  if (value >= 0.7) return "bg-amber-500/20 text-amber-700 dark:text-amber-300";
  return "bg-destructive/15 text-destructive";
}

function fieldValueKeys(field: MatterCodingComparisonFieldOptionRead | undefined) {
  if (!field) return [];
  if (field.type === "BOOLEAN") return ["false", "true"];
  return field.allowed_values
    .map((value) => value.key)
    .filter((value): value is string => typeof value === "string");
}

function fieldValueOptions(field: MatterCodingComparisonFieldOptionRead | undefined): CanonicalValueOption[] {
  if (!field) return [];
  if (field.type === "BOOLEAN") return [{ key: "false", label: "False / No" }, { key: "true", label: "True / Yes" }];
  return field.allowed_values.flatMap((value) => typeof value.key === "string" ? [{ key: value.key, label: typeof value.label === "string" ? value.label : value.key }] : []);
}

function canonicalValueOptions(fields: Array<MatterCodingComparisonFieldOptionRead | undefined>) {
  const options = new Map<string, CanonicalValueOption>();
  for (const field of fields) {
    for (const option of fieldValueOptions(field)) options.set(option.key, option);
  }
  return [...options.values()];
}

function sourceMapping(mappingState: MappingState, sourceId: string, field: MatterCodingComparisonFieldOptionRead | undefined) {
  return Object.fromEntries(fieldValueKeys(field).map((key) => [key, mappingState[sourceId]?.[key] ?? key]));
}

export function MatterCodingComparisons({ matterId, batchesHref }: { matterId: string; batchesHref: string }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("Coding comparison");
  const [batchIds, setBatchIds] = useState<string[]>([]);
  const [qaRunIds, setQaRunIds] = useState<string[]>([]);
  const [qaFieldId, setQaFieldId] = useState("");
  const [reviewerFieldId, setReviewerFieldId] = useState("");
  const [reviewerCodingIds, setReviewerCodingIds] = useState<string[]>([]);
  const [humanFieldId, setHumanFieldId] = useState("");
  const [scoringRunId, setScoringRunId] = useState("none");
  const [mappingState, setMappingState] = useState<MappingState>({});
  const [selectedComparisonId, setSelectedComparisonId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("comparison");
  });
  const [agreementFilter, setAgreementFilter] = useState("ALL");
  const comparisonResultRef = useRef<HTMLDivElement>(null);

  const options = useQuery({
    queryKey: ["matter-coding-comparison-options", matterId],
    queryFn: () => coreApi<MatterCodingComparisonOptionsRead>(`/v1/matters/${matterId}/coding-comparisons/options`),
  });
  const comparisons = useQuery({
    queryKey: ["matter-coding-comparisons", matterId],
    queryFn: () => coreApi<MatterCodingComparisonRead[]>(`/v1/matters/${matterId}/coding-comparisons`),
    refetchInterval: (query) => query.state.data?.some((item) => ["QUEUED", "RUNNING"].includes(item.status)) ? 2000 : false,
  });
  const listedComparison = comparisons.data?.find((item) => item.id === selectedComparisonId);
  const comparisonDetail = useQuery({
    queryKey: ["matter-coding-comparison", matterId, selectedComparisonId],
    queryFn: () => coreApi<MatterCodingComparisonRead>(`/v1/matters/${matterId}/coding-comparisons/${selectedComparisonId}`),
    enabled: Boolean(selectedComparisonId),
    initialData: listedComparison,
  });
  const selectedComparison = comparisonDetail.data ?? listedComparison;
  const documents = useQuery({
    queryKey: ["matter-coding-comparison-documents", matterId, selectedComparisonId, agreementFilter],
    queryFn: () => {
      const filter = agreementFilter === "ALL" ? "" : `&agreement_status=${agreementFilter}`;
      return coreApi<MatterCodingComparisonDocumentPage>(`/v1/matters/${matterId}/coding-comparisons/${selectedComparisonId}/documents?offset=0&limit=100${filter}`);
    },
    enabled: selectedComparison?.status === "COMPLETED" || selectedComparison?.status === "COMPLETED_WITH_ERRORS",
  });

  const fieldsById = useMemo(() => new Map(options.data?.fields.map((field) => [field.id, field]) ?? []), [options.data]);
  const qaField = fieldsById.get(qaFieldId);
  const reviewerField = fieldsById.get(reviewerFieldId);
  const humanField = fieldsById.get(humanFieldId);
  const scoringRun = options.data?.classifier_scoring_runs.find((run) => run.id === scoringRunId);
  const classifierField = fieldsById.get(scoringRun?.metadata_definition_id ?? "");
  const visibleQaRuns = options.data?.qa_runs.filter((run) => batchIds.includes(run.review_batch_id)) ?? [];
  const visibleReviewerCodings = options.data?.reviewer_codings?.filter((coding) => (
    batchIds.includes(coding.review_batch_id)
    && (!reviewerFieldId || coding.metadata_definition_ids.includes(reviewerFieldId))
  )) ?? [];
  const canonicalOptions = canonicalValueOptions([qaField, reviewerField, humanField, classifierField]);

  useEffect(() => {
    if (!selectedComparisonId || !selectedComparison) return;
    comparisonResultRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [selectedComparison, selectedComparisonId]);

  function viewComparison(comparisonId: string) {
    setSelectedComparisonId(comparisonId);
    setAgreementFilter("ALL");
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("comparison", comparisonId);
      window.history.replaceState(window.history.state, "", url);
    }
  }

  const createComparison = useMutation({
    mutationFn: (payload: MatterCodingComparisonCreate) => coreApi<MatterCodingComparisonRead>(`/v1/matters/${matterId}/coding-comparisons`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterCodingComparisonRead[]>(["matter-coding-comparisons", matterId], (current) => current ? [created, ...current.filter((item) => item.id !== created.id)] : [created]);
      viewComparison(created.id);
      toast.success("Coding comparison queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The coding comparison could not be started."),
  });

  function toggleBatch(batchId: string) {
    const next = batchIds.includes(batchId) ? batchIds.filter((id) => id !== batchId) : [...batchIds, batchId];
    setBatchIds(next);
    setQaRunIds((runIds) => runIds.filter((runId) => options.data?.qa_runs.some((run) => run.id === runId && next.includes(run.review_batch_id))));
    setReviewerCodingIds((codingIds) => codingIds.filter((codingId) => options.data?.reviewer_codings?.some((coding) => coding.id === codingId && next.includes(coding.review_batch_id))));
  }

  function toggleQaRun(runId: string) {
    setQaRunIds((current) => current.includes(runId) ? current.filter((id) => id !== runId) : [...current, runId]);
  }

  function toggleReviewerCoding(codingId: string) {
    setReviewerCodingIds((current) => current.includes(codingId) ? current.filter((id) => id !== codingId) : [...current, codingId]);
  }

  function updateMapping(sourceId: string, rawKey: string, canonicalKey: string) {
    setMappingState((current) => ({ ...current, [sourceId]: { ...current[sourceId], [rawKey]: canonicalKey } }));
  }

  function submit() {
    const sources: MatterCodingComparisonSource[] = [];
    if (qaRunIds.length && qaField) {
      sources.push({ source_id: "qa", source_type: "QA_RUN_GROUP", label: "Q&A coding", metadata_definition_id: qaField.id, run_ids: qaRunIds, value_mapping: sourceMapping(mappingState, "qa", qaField) });
    }
    if (reviewerCodingIds.length && reviewerField) {
      const selectedCodings = options.data?.reviewer_codings?.filter((coding) => reviewerCodingIds.includes(coding.id)) ?? [];
      sources.push({
        source_id: "reviewer",
        source_type: "REVIEWER_CODING_GROUP",
        label: selectedCodings.length === 1 ? `${selectedCodings[0].label} · ${reviewerField.display_name}` : `Reviewer coding · ${reviewerField.display_name}`,
        metadata_definition_id: reviewerField.id,
        reviewer_codings: selectedCodings.map((coding) => ({ id: coding.id, revision: coding.revision })),
        value_mapping: sourceMapping(mappingState, "reviewer", reviewerField),
      });
    }
    if (humanField) {
      sources.push({ source_id: "human", source_type: "MATTER_FIELD", label: humanField.display_name, metadata_definition_id: humanField.id, value_mapping: sourceMapping(mappingState, "human", humanField) });
    }
    if (scoringRun && classifierField) {
      sources.push({ source_id: "classifier", source_type: "CLASSIFIER_SCORING_RUN", label: scoringRun.label, metadata_definition_id: classifierField.id, scoring_run_id: scoringRun.id, value_mapping: sourceMapping(mappingState, "classifier", classifierField) });
    }
    if (!batchIds.length || sources.length < 2) {
      toast.error("Select at least one batch and two coding sources.");
      return;
    }
    createComparison.mutate({ name, review_batch_ids: batchIds, sources, idempotency_key: crypto.randomUUID() });
  }

  if (options.isPending || comparisons.isPending) return <TableLoading />;
  if (options.error || comparisons.error) return <QueryError message={options.error?.message ?? comparisons.error?.message} />;

  return <div className="space-y-6">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">Compare coding</h2><p className="text-sm text-muted-foreground">Compare Q&A runs across batches with matter coding and whole-matter classifier predictions.</p></div>
      <Button variant="outline" asChild><Link href={batchesHref}>Back to batches</Link></Button>
    </div>

    <Card>
      <CardHeader><CardTitle>New comparison</CardTitle><CardDescription>The selected batches form one deduplicated population. Missing, failed, conflicted, and unmapped values stay distinct from negative coding.</CardDescription></CardHeader>
      <CardContent className="space-y-6">
        <div className="grid gap-4 md:grid-cols-2"><div className="space-y-2"><Label htmlFor="comparison-name">Name</Label><Input id="comparison-name" value={name} onChange={(event) => setName(event.target.value)} /></div></div>
        <fieldset className="space-y-2"><legend className="text-sm font-semibold">Population batches</legend><div className="grid gap-2 md:grid-cols-2">{options.data.batches.map((batch) => <label key={batch.id} className="flex items-center gap-2 rounded-lg border p-3 text-sm"><input type="checkbox" checked={batchIds.includes(batch.id)} onChange={() => toggleBatch(batch.id)} /><span className="flex-1">{batch.name}</span><span className="tabular-nums text-muted-foreground">{batch.document_count.toLocaleString()}</span></label>)}</div></fieldset>

        <div className="grid gap-5 lg:grid-cols-2 xl:grid-cols-4">
          <SourceCard title="Q&A coding" description="Choose one field and the completed Q&A runs that cover the selected batches.">
            <FieldSelect value={qaFieldId} onChange={setQaFieldId} fields={options.data.fields} placeholder="Q&A field" />
            <div className="max-h-48 space-y-2 overflow-auto rounded-lg border p-2">{visibleQaRuns.length ? visibleQaRuns.map((run) => <label key={run.id} className="flex items-start gap-2 text-sm"><input type="checkbox" checked={qaRunIds.includes(run.id)} onChange={() => toggleQaRun(run.id)} /><span>{run.label}</span></label>) : <p className="text-sm text-muted-foreground">Select population batches to see their Q&A runs.</p>}</div>
            <MappingEditor sourceId="qa" field={qaField} mapping={mappingState.qa} canonicalOptions={canonicalOptions} onChange={updateMapping} />
          </SourceCard>
          <SourceCard title="Reviewer coding" description="Compare immutable human reviewer coding from the selected batches, including imported coding.">
            <FieldSelect value={reviewerFieldId} onChange={(value) => { setReviewerFieldId(value); setReviewerCodingIds([]); }} fields={options.data.fields} placeholder="Reviewer coding field" />
            <div className="max-h-48 space-y-2 overflow-auto rounded-lg border p-2">{visibleReviewerCodings.length ? visibleReviewerCodings.map((coding) => <label key={coding.id} className="flex items-start gap-2 text-sm"><input type="checkbox" checked={reviewerCodingIds.includes(coding.id)} onChange={() => toggleReviewerCoding(coding.id)} /><span><span className="block">{coding.label}</span><span className="block text-xs text-muted-foreground">{coding.detail} · revision {coding.revision.toLocaleString()}</span></span></label>) : <p className="text-sm text-muted-foreground">Select population batches and a field to see reviewer coding.</p>}</div>
            <MappingEditor sourceId="reviewer" field={reviewerField} mapping={mappingState.reviewer} canonicalOptions={canonicalOptions} onChange={updateMapping} />
          </SourceCard>
          <SourceCard title="Matter coding" description="Optional: read the current resolved matter coding from a field.">
            <FieldSelect value={humanFieldId} onChange={setHumanFieldId} fields={options.data.fields} placeholder="Human coding field" />
            <MappingEditor sourceId="human" field={humanField} mapping={mappingState.human} canonicalOptions={canonicalOptions} onChange={updateMapping} />
          </SourceCard>
          <SourceCard title="Classifier predictions" description="Optional: compare a completed whole-matter scoring artifact without publishing predictions.">
            <Select value={scoringRunId} onValueChange={setScoringRunId}><SelectTrigger><SelectValue placeholder="No classifier" /></SelectTrigger><SelectContent><SelectItem value="none">Do not include</SelectItem>{options.data.classifier_scoring_runs.map((run) => <SelectItem key={run.id} value={run.id}>{run.label} · {run.population_document_count.toLocaleString()}</SelectItem>)}</SelectContent></Select>
            <MappingEditor sourceId="classifier" field={classifierField} mapping={mappingState.classifier} canonicalOptions={canonicalOptions} onChange={updateMapping} />
          </SourceCard>
        </div>
        <div className="flex justify-end"><Button disabled={createComparison.isPending || !name.trim()} onClick={submit}>{createComparison.isPending ? "Queuing…" : "Run comparison"}</Button></div>
      </CardContent>
    </Card>

    <Card>
      <CardHeader><CardTitle>Comparison jobs</CardTitle><CardDescription>Document-level matrices are stored as artifacts; this list retains only operational status and summary counts.</CardDescription></CardHeader>
      <CardContent><Table><TableHeader><TableRow><TableHead>Name</TableHead><TableHead>Status</TableHead><TableHead>Population</TableHead><TableHead>Compared</TableHead><TableHead>Created</TableHead><TableHead /></TableRow></TableHeader><TableBody>{comparisons.data.length ? comparisons.data.map((comparison) => <TableRow key={comparison.id}><TableCell className="font-medium">{comparison.name}{comparison.failure_summary ? <p className="max-w-lg text-xs text-destructive">{comparison.failure_summary}</p> : null}</TableCell><TableCell><StatusBadge status={comparison.status} /></TableCell><TableCell>{comparison.population_document_count.toLocaleString()}</TableCell><TableCell>{comparison.compared_document_count.toLocaleString()}</TableCell><TableCell>{formatDate(comparison.created_at)}</TableCell><TableCell><Button size="sm" variant={selectedComparisonId === comparison.id ? "default" : "outline"} disabled={!comparison.result_artifact_id} onClick={() => viewComparison(comparison.id)}>{selectedComparisonId === comparison.id ? "Viewing" : "View statistics"}</Button></TableCell></TableRow>) : <TableRow><TableCell colSpan={6} className="py-8 text-center text-muted-foreground">No coding comparisons have been run.</TableCell></TableRow>}</TableBody></Table></CardContent>
    </Card>

    {selectedComparisonId ? <div ref={comparisonResultRef} className="scroll-mt-4 space-y-6">
      {comparisonDetail.isPending && !selectedComparison ? <Card><CardContent className="pt-6"><TableLoading /></CardContent></Card> : null}
      {comparisonDetail.error ? <QueryError message={comparisonDetail.error.message} /> : null}
      {selectedComparison?.summary ? <ComparisonSummary comparison={selectedComparison} /> : null}
      {selectedComparison?.result_artifact_id ? <Card><CardHeader><div className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>Document matrix</CardTitle><CardDescription>First 100 matching documents. Artifact rows retain the complete matrix.</CardDescription></div><Select value={agreementFilter} onValueChange={setAgreementFilter}><SelectTrigger className="w-48"><SelectValue /></SelectTrigger><SelectContent>{["ALL", "EXACT", "PARTIAL", "DISAGREEMENT", "INCOMPLETE"].map((value) => <SelectItem key={value} value={value}>{value.replace("_", " ").toLowerCase()}</SelectItem>)}</SelectContent></Select></div></CardHeader><CardContent>{documents.isPending ? <TableLoading /> : documents.error ? <QueryError message={documents.error.message} /> : <Table><TableHeader><TableRow><TableHead>Document</TableHead><TableHead>Agreement</TableHead>{selectedComparison.configuration.sources && Array.isArray(selectedComparison.configuration.sources) ? (selectedComparison.configuration.sources as Array<{ source_id: string; label: string }>).map((source) => <TableHead key={source.source_id}>{source.label}</TableHead>) : null}</TableRow></TableHeader><TableBody>{documents.data?.items.map((document) => <TableRow key={document.matter_document_id}><TableCell className="font-mono text-xs">…{document.matter_document_id.slice(-8)}</TableCell><TableCell>{document.agreement_status}</TableCell>{document.sources.map((source) => <TableCell key={source.source_id}>{source.state === "EVALUATED" ? (source.value_keys.join(", ") || "No value") : <span className="text-muted-foreground">{source.state.toLowerCase()}</span>}</TableCell>)}</TableRow>)}</TableBody></Table>}</CardContent></Card> : null}
    </div> : null}
  </div>;
}

function SourceCard({ title, description, children }: { title: string; description: string; children: React.ReactNode }) {
  return <div className="space-y-4 rounded-xl border p-4"><div><h3 className="font-semibold">{title}</h3><p className="text-xs text-muted-foreground">{description}</p></div>{children}</div>;
}

function FieldSelect({ value, onChange, fields, placeholder }: { value: string; onChange: (value: string) => void; fields: MatterCodingComparisonFieldOptionRead[]; placeholder: string }) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger><SelectValue placeholder={placeholder} /></SelectTrigger><SelectContent>{fields.filter((field) => ["BOOLEAN", "ENUM", "TEXT", "LONG_TEXT"].includes(field.type)).map((field) => <SelectItem key={field.id} value={field.id}>{field.display_name}</SelectItem>)}</SelectContent></Select>;
}

function MappingEditor({ sourceId, field, mapping, canonicalOptions, onChange }: { sourceId: string; field: MatterCodingComparisonFieldOptionRead | undefined; mapping: Record<string, string> | undefined; canonicalOptions: CanonicalValueOption[]; onChange: (sourceId: string, rawKey: string, canonicalKey: string) => void }) {
  const keys = fieldValueKeys(field);
  if (!field || !keys.length) return null;
  return <div className="space-y-2"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Map to comparison value</p>{keys.map((key) => <div key={key} className="grid grid-cols-[minmax(0,0.8fr)_auto_minmax(0,1.2fr)] items-center gap-2"><span className="truncate text-xs" title={key}>{fieldValueOptions(field).find((option) => option.key === key)?.label ?? key}</span><span className="text-muted-foreground">→</span><Select value={mapping?.[key] ?? key} onValueChange={(value) => onChange(sourceId, key, value)}><SelectTrigger className="h-8 text-xs" aria-label={`${field.display_name} ${key} comparison value`}><SelectValue /></SelectTrigger><SelectContent>{canonicalOptions.map((option) => <SelectItem key={option.key} value={option.key}>{option.label} · {option.key}</SelectItem>)}</SelectContent></Select></div>)}</div>;
}

function ComparisonSummary({ comparison }: { comparison: MatterCodingComparisonRead }) {
  const summary = comparison.summary as ComparisonStatistics;
  const sources = useMemo(() => summary.sources ?? [], [summary.sources]);
  const pairs = summary.pairwise ?? [];
  const counts = summary.agreement_counts ?? {};
  const population = summary.document_count ?? comparison.population_document_count;
  const incomplete = counts.INCOMPLETE ?? Math.max(0, population - comparison.compared_document_count);
  const compared = Math.max(0, population - incomplete);
  const exact = counts.EXACT ?? 0;
  const partial = counts.PARTIAL ?? 0;
  const disagreement = counts.DISAGREEMENT ?? 0;
  const sourceMap = new Map(sources.map((source) => [source.source_id, source]));
  const pairMap = new Map<string, ComparisonPair>(pairs.flatMap((pair) => [
    [`${pair.left_source_id}:${pair.right_source_id}`, pair] as const,
    [`${pair.right_source_id}:${pair.left_source_id}`, pair] as const,
  ]));
  const [selectedPairKey, setSelectedPairKey] = useState(pairs[0] ? `${pairs[0].left_source_id}:${pairs[0].right_source_id}` : "");
  const selectedPair = pairMap.get(selectedPairKey) ?? pairs[0];
  const chartData = useMemo(() => {
    const values = new Map<string, Record<string, string | number>>();
    for (const source of sources) {
      for (const [valueKey, documentCount] of Object.entries(source.value_counts ?? {})) {
        const row = values.get(valueKey) ?? { value_key: valueKey, label: displayValue(valueKey), total: 0 };
        row[source.source_id] = documentCount;
        row.total = Number(row.total) + documentCount;
        values.set(valueKey, row);
      }
    }
    return [...values.values()].sort((left, right) => Number(right.total) - Number(left.total)).slice(0, 40);
  }, [sources]);
  const chartHeight = Math.max(220, chartData.length * 36);

  return <Card>
    <CardHeader>
      <CardTitle>{comparison.name} statistics</CardTitle>
      <CardDescription>Agreement is calculated only when every selected source produced an evaluated result. Missing and failed results are reported separately—not treated as negative coding.</CardDescription>
    </CardHeader>
    <CardContent className="space-y-5">
      <dl className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Statistic label="Population" value={population} detail="unique documents" />
        <Statistic label="Fully compared" value={compared} detail={`${percent(population ? compared / population : null)} of population`} />
        <Statistic label="Exact agreement" value={exact} detail={`${percent(compared ? exact / compared : null)} of compared`} />
        <Statistic label="Partial / disagreement" value={partial + disagreement} detail={`${partial.toLocaleString()} partial · ${disagreement.toLocaleString()} different`} />
        <Statistic label="Incomplete" value={incomplete} detail="missing, failed, skipped, or conflicted" />
      </dl>

      <section>
        <h3 className="font-semibold">Source coverage</h3>
        <p className="mt-1 text-xs text-muted-foreground">How much of the selected population each coding source actually evaluated.</p>
        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{sources.map((source) => {
          const evaluated = source.evaluated_document_count ?? source.state_counts.EVALUATED ?? 0;
          const noValue = source.no_value_document_count;
          const exceptional = Object.entries(source.state_counts).filter(([state]) => state !== "EVALUATED" && state !== "MISSING");
          return <div key={source.source_id} className="rounded-lg border bg-muted/20 p-3">
            <p className="truncate text-sm font-semibold" title={source.label}>{source.label}</p>
            <p className="mt-1 text-xs tabular-nums text-muted-foreground">{evaluated.toLocaleString()} evaluated · {noValue == null ? "—" : noValue.toLocaleString()} no value · {(source.state_counts.MISSING ?? 0).toLocaleString()} missing</p>
            {exceptional.length ? <p className="mt-0.5 text-xs tabular-nums text-muted-foreground">{exceptional.map(([state, value]) => `${displayValue(state)} ${value.toLocaleString()}`).join(" · ")}</p> : null}
          </div>;
        })}</div>
      </section>

      <section className="rounded-lg border p-4">
        <h3 className="font-semibold">Coding counts</h3>
        <p className="mt-1 text-xs text-muted-foreground">Distinct documents carrying each canonical value. Multi-value documents can contribute to more than one bar.</p>
        {chartData.length ? <div className="mt-4 w-full" style={{ height: chartHeight }} aria-label="Coding counts by value and source">
          <ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 960, height: chartHeight }}>
            <BarChart data={chartData} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 12 }} accessibilityLayer>
              <CartesianGrid horizontal={false} stroke="var(--border)" strokeOpacity={0.7} />
              <XAxis type="number" allowDecimals={false} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
              <YAxis type="category" dataKey="label" width={180} interval={0} axisLine={false} tickLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} />
              <Tooltip formatter={(value, name) => [Number(value).toLocaleString(), sourceMap.get(String(name))?.label ?? String(name)]} />
              <Legend formatter={(value) => sourceMap.get(String(value))?.label ?? String(value)} />
              {sources.map((source, index) => <Bar key={source.source_id} dataKey={source.source_id} fill={SOURCE_COLORS[index % SOURCE_COLORS.length]} radius={[0, 3, 3, 0]} maxBarSize={18} isAnimationActive={false} />)}
            </BarChart>
          </ResponsiveContainer>
        </div> : <p className="mt-4 rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">No canonical coding values were found in the compared sources.</p>}
        {sources.some((source) => source.value_counts_truncated) ? <p className="mt-2 text-xs text-muted-foreground">Showing the most frequent canonical values; the document artifact retains the complete result.</p> : null}
      </section>

      {pairs.length ? <section className="rounded-lg border p-4">
        <h3 className="font-semibold">Pairwise agreement</h3>
        <p className="mt-1 text-xs text-muted-foreground">Exact set agreement among documents evaluated by both sources. Agreement measures consistency, not which source is correct.</p>
        <div className="mt-4 overflow-x-auto"><table className="min-w-full border-separate border-spacing-1 text-xs"><thead><tr><th className="p-2 text-left text-muted-foreground">Source</th>{sources.map((source) => <th key={source.source_id} className="max-w-40 p-2 text-left">{source.label}</th>)}</tr></thead><tbody>{sources.map((left) => <tr key={left.source_id}><th className="max-w-40 p-2 text-left font-medium">{left.label}</th>{sources.map((right) => {
          if (left.source_id === right.source_id) return <td key={right.source_id} className="rounded bg-muted p-2 text-center">100%</td>;
          const pair = pairMap.get(`${left.source_id}:${right.source_id}`);
          return <td key={right.source_id} className={cn("cursor-pointer rounded p-2 text-center font-semibold", agreementClass(pair?.exact_agreement))} title={`${pair?.compared_document_count.toLocaleString() ?? 0} documents compared`} onClick={() => pair && setSelectedPairKey(`${pair.left_source_id}:${pair.right_source_id}`)}>{percent(pair?.exact_agreement)}</td>;
        })}</tr>)}</tbody></table></div>
        {selectedPair ? <div className="mt-4 border-t pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-medium">{sourceMap.get(selectedPair.left_source_id)?.label} ↔ {sourceMap.get(selectedPair.right_source_id)?.label}</p><div className="flex flex-wrap gap-2 text-xs"><Badge variant="outline">{selectedPair.compared_document_count.toLocaleString()} compared</Badge><Badge variant="outline">{(selectedPair.incomplete_document_count ?? selectedPair.agreement_counts.INCOMPLETE ?? 0).toLocaleString()} incomplete</Badge><Badge variant="outline">Jaccard {percent(selectedPair.mean_jaccard)}</Badge></div></div>
          {selectedPair.confusion_matrix.length ? <div className="mt-3"><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Confusion matrix</p><Table><TableHeader><TableRow><TableHead>{sourceMap.get(selectedPair.left_source_id)?.label} value</TableHead><TableHead>{sourceMap.get(selectedPair.right_source_id)?.label} value</TableHead><TableHead className="text-right">Documents</TableHead></TableRow></TableHeader><TableBody>{selectedPair.confusion_matrix.map((cell) => <TableRow key={`${cell.left_value}:${cell.right_value}`}><TableCell>{displayValue(cell.left_value)}</TableCell><TableCell>{displayValue(cell.right_value)}</TableCell><TableCell className="text-right tabular-nums">{cell.document_count.toLocaleString()}</TableCell></TableRow>)}</TableBody></Table></div> : null}
          {selectedPair.confusion_matrix_truncated ? <p className="mt-2 text-xs text-muted-foreground">Showing the most frequent value combinations; the document artifact retains the complete result.</p> : null}
        </div> : null}
      </section> : null}

      {summary.multi_source?.class_agreement.length ? <section className="rounded-lg border p-4">
        <h3 className="font-semibold">Per-value overlap</h3>
        <p className="mt-1 text-xs text-muted-foreground">Documents assigned each value by each source, restricted to documents evaluated by every selected source.</p>
        <div className="mt-3 flex flex-wrap gap-2 text-xs"><Badge variant="outline">{summary.multi_source.compared_document_count.toLocaleString()} compared</Badge><Badge variant="outline">{summary.multi_source.incomplete_document_count.toLocaleString()} incomplete</Badge></div>
        <div className="mt-3 overflow-x-auto"><Table><TableHeader><TableRow><TableHead>Value</TableHead>{sources.map((source) => <TableHead key={source.source_id} className="text-right">{source.label}</TableHead>)}<TableHead className="text-right">All selected</TableHead><TableHead className="text-right">Any selected</TableHead><TableHead className="text-right">Overlap</TableHead></TableRow></TableHeader><TableBody>{summary.multi_source.class_agreement.map((item) => {
          const sourceCounts = new Map(item.source_counts.map((count) => [count.source_id, count.document_count]));
          return <TableRow key={item.value_key}><TableCell className="font-medium">{displayValue(item.value_key)}</TableCell>{sources.map((source) => <TableCell key={source.source_id} className="text-right tabular-nums">{(sourceCounts.get(source.source_id) ?? 0).toLocaleString()}</TableCell>)}<TableCell className="text-right tabular-nums">{item.all_selected_count.toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{item.any_selected_count.toLocaleString()}</TableCell><TableCell className="text-right tabular-nums">{percent(item.jaccard)}</TableCell></TableRow>;
        })}</TableBody></Table></div>
        {summary.multi_source.class_agreement_truncated ? <p className="mt-2 text-xs text-muted-foreground">Showing the most frequent canonical values; the document artifact retains the complete result.</p> : null}
      </section> : null}
    </CardContent>
  </Card>;
}

function Statistic({ label, value, detail }: { label: string; value: number; detail: string }) {
  return <div className="rounded-lg border bg-muted/20 p-3"><dt className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</dt><dd className="mt-1 text-2xl font-semibold tabular-nums">{value.toLocaleString()}</dd><dd className="mt-1 text-xs text-muted-foreground">{detail}</dd></div>;
}
