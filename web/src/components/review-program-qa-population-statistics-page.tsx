"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ListChecks } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ComparisonCountsChart, MultiSourceOverlap, PairwiseAgreement } from "@/components/batch-coding-comparison";
import { QueryError, TableLoading } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { ReviewProgramQAPopulationStatisticsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref } from "@/lib/matter-routes";

export function ReviewProgramQAPopulationStatisticsPage({
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
  const [fieldId, setFieldId] = useState("");
  const [includeClassifier, setIncludeClassifier] = useState(false);
  const [includeHuman, setIncludeHuman] = useState(false);
  const statistics = useQuery({
    queryKey: ["review-program-qa-population-statistics", matterId, programId, runId, fieldId, includeClassifier, includeHuman],
    queryFn: () => {
      const query = new URLSearchParams();
      if (fieldId) query.set("metadata_definition_id", fieldId);
      if (includeClassifier) query.set("include_classifier", "true");
      if (includeHuman) query.set("include_human", "true");
      return coreApi<ReviewProgramQAPopulationStatisticsRead>(`/v1/matters/${matterId}/review-programs/${programId}/runs/${runId}/qa-population-statistics${query.size ? `?${query}` : ""}`);
    },
  });
  const classifierSource = statistics.data?.available_sources.find((source) => source.run_type === "CLASSIFIER");
  const humanSource = statistics.data?.available_sources.find((source) => source.source_type === "REVIEWER");

  return <div className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <div className="mb-2 flex items-center gap-2 text-sm text-muted-foreground"><ListChecks className="size-4" />Review Program analysis</div>
        <h2 className="text-xl font-semibold">Q&amp;A reviewed population statistics</h2>
        <p className="mt-1 max-w-4xl text-sm text-muted-foreground">Aggregated across every unique document with a usable result from either pinned Q&amp;A model in this Review Program, including discovery, classifier-improvement, and classifier-positive coverage work.</p>
      </div>
      <Button asChild size="sm" variant="outline"><Link href={matterHref(clientId, matterId, "review-programs", { program: programId, run: runId })}><ArrowLeft />Back to Review Program</Link></Button>
    </div>

    {statistics.isLoading ? <TableLoading /> : statistics.error ? <QueryError message={statistics.error.message} /> : statistics.data ? <>
      <div className="grid gap-3 sm:grid-cols-3">
        <Card><CardHeader className="pb-2"><CardDescription>Unique Q&amp;A-reviewed documents</CardDescription><CardTitle className="text-2xl tabular-nums">{statistics.data.document_count.toLocaleString()}</CardTitle></CardHeader></Card>
        <Card><CardHeader className="pb-2"><CardDescription>Reviewed by both Q&amp;A models</CardDescription><CardTitle className="text-2xl tabular-nums">{statistics.data.both_profiles_document_count.toLocaleString()}</CardTitle></CardHeader></Card>
        <Card><CardHeader className="pb-2"><CardDescription>Reviewed by only one Q&amp;A model</CardDescription><CardTitle className="text-2xl tabular-nums">{statistics.data.one_profile_document_count.toLocaleString()}</CardTitle></CardHeader></Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Comparison sources</CardTitle><CardDescription>Both pinned Q&amp;A models are included by default. Add accepted classifier or human-review results when they are available for the selected coding field.</CardDescription></CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-[minmax(16rem,0.4fr)_minmax(0,1fr)]">
            <label className="block space-y-1.5 text-sm font-medium">Coding field<Select value={statistics.data.selected_metadata_definition_id} onValueChange={(value) => { setFieldId(value); setIncludeClassifier(false); setIncludeHuman(false); }}><SelectTrigger aria-label="Q&A population coding field"><SelectValue /></SelectTrigger><SelectContent>{statistics.data.fields.map((field) => <SelectItem key={field.id} value={field.id}>{field.display_name} · {field.cardinality.toLowerCase()}</SelectItem>)}</SelectContent></Select></label>
            <div className="space-y-2"><p className="text-sm font-medium">Optional sources</p><div className="flex flex-wrap gap-4">
              {classifierSource ? <label className="flex items-start gap-2 text-sm"><input className="mt-0.5" type="checkbox" checked={includeClassifier} onChange={(event) => setIncludeClassifier(event.target.checked)} /><span><span className="font-medium">{classifierSource.label}</span><span className="block text-xs text-muted-foreground">{classifierSource.detail}</span></span></label> : null}
              {humanSource ? <label className="flex items-start gap-2 text-sm"><input className="mt-0.5" type="checkbox" checked={includeHuman} onChange={(event) => setIncludeHuman(event.target.checked)} /><span><span className="font-medium">{humanSource.label}</span><span className="block text-xs text-muted-foreground">{humanSource.detail}</span></span></label> : null}
              {!classifierSource && !humanSource ? <p className="text-sm text-muted-foreground">No classifier or human-review source is available for this field.</p> : null}
            </div></div>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{statistics.data.source_summaries.map((summary) => { const source = statistics.data.sources.find((item) => item.id === summary.source_id); return <div key={summary.source_id} className="rounded-lg border bg-muted/20 p-3"><p className="font-semibold">{source?.label ?? "Comparison source"}</p><p className="mt-1 text-xs text-muted-foreground">{summary.evaluated_document_count.toLocaleString()} evaluated · {summary.no_value_document_count.toLocaleString()} no value · {summary.not_evaluated_document_count.toLocaleString()} not reviewed</p></div>; })}</div>
        </CardContent>
      </Card>

      <ComparisonCountsChart comparison={statistics.data} />
      <PairwiseAgreement comparison={statistics.data} />
      <MultiSourceOverlap comparison={statistics.data} />
    </> : null}
  </div>;
}
