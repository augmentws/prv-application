"use client";

import { keepPreviousData, useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Bot, CircleDashed } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { DateHistogram } from "@/components/date-histogram";
import { FactExportDialog } from "@/components/facts/fact-export-dialog";
import { FactSearchForm, useFactFilterControls, useFactListState } from "@/components/facts/fact-filters";
import { EmptyState, QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { MatterFactHistogramRead, MatterFactPage, MatterFactRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";
import { factDetailHref, formatFactDate, groupTimeline, timelineFilters } from "@/lib/facts";

export const TIMELINE_PAGE_SIZE = 100;
const REFRESH_INTERVAL_MS = 15_000;
const HISTOGRAM_INTERVALS: Record<"month" | "year", "MONTH" | "YEAR"> = { month: "MONTH", year: "YEAR" };
const FACT_UNIT = { singular: "fact", plural: "facts" };

function TimelineCard({ fact, clientId, matterId, issueNames }: { fact: MatterFactRead; clientId: string; matterId: string; issueNames: Map<string, string> }) {
  const working = fact.status === "WORKING";
  return (
    <li
      className={cn(
        "relative rounded-lg border bg-card p-3 text-sm shadow-xs",
        working && "border-dashed border-pending/60 bg-pending/5",
      )}
    >
      <span aria-hidden="true" className={cn("absolute top-4 -left-[1.4rem] size-2.5 rounded-full border-2 border-background", working ? "bg-pending" : "bg-confirmed")} />
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium tabular-nums text-muted-foreground">{formatFactDate(fact)}</span>
        {working ? (
          <Badge variant="outline" className="gap-1 border-pending/50 text-pending"><CircleDashed className="size-3" aria-hidden="true" />Unconfirmed</Badge>
        ) : null}
      </div>
      <Link href={factDetailHref(clientId, matterId, fact.id)} className="mt-1 block font-medium hover:text-primary">{fact.summary}</Link>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        {fact.owner ? <span className="font-medium text-foreground">{fact.owner.canonical_display_name}</span> : null}
        {fact.issues.map((issue) => (
          <Badge key={`${issue.matter_analysis_task_id}:${issue.issue_key}`} variant="outline">{issueNames.get(issue.issue_key) ?? issue.issue_key}</Badge>
        ))}
        {fact.key_fact ? <Badge variant="accent">Key fact</Badge> : null}
        {fact.has_contradiction ? <Badge variant="outline" className="text-conflict">Contradicted</Badge> : null}
        {fact.origin_workflow_run_id ? <span className="inline-flex items-center gap-1 text-agent"><Bot className="size-3" aria-hidden="true" />Agent</span> : null}
        <span>{fact.evidence_count} {fact.evidence_count === 1 ? "quote" : "quotes"} in {fact.document_count} {fact.document_count === 1 ? "document" : "documents"}</span>
      </div>
    </li>
  );
}

/** A chronological reading view of confirmed facts, optionally with working facts marked as unconfirmed. */
export function FactsTimelinePanel({ clientId, matterId, canExport = false }: { clientId: string; matterId: string; canExport?: boolean }) {
  const { state, update } = useFactListState();
  const { issueNames, sidebar, activeFilterBar, hasFilters } = useFactFilterControls({
    matterId,
    state,
    update,
    baseFilters: timelineFilters,
    hiddenFacets: ["status"],
  });
  const [interval, setHistogramInterval] = useState<"month" | "year">("year");
  const filters = useMemo(() => timelineFilters(state), [state]);

  const histogram = useQuery({
    queryKey: ["matter-fact-histogram", matterId, filters, interval],
    queryFn: () =>
      coreApi<MatterFactHistogramRead>(`/v1/matters/${matterId}/facts/date-histogram`, {
        method: "POST",
        body: JSON.stringify({ filters, interval: HISTOGRAM_INTERVALS[interval] }),
      }),
    placeholderData: keepPreviousData,
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const facts = useInfiniteQuery({
    queryKey: ["matter-fact-timeline", matterId, filters],
    queryFn: ({ pageParam }) =>
      coreApi<MatterFactPage>(`/v1/matters/${matterId}/facts/search`, {
        method: "POST",
        body: JSON.stringify({ filters, sort: "DATE_ASC", limit: TIMELINE_PAGE_SIZE, offset: pageParam }),
      }),
    initialPageParam: 0,
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((count, page) => count + page.items.length, 0);
      return loaded < last.total && last.items.length ? loaded : undefined;
    },
    // Confirming, rejecting and merging change the timeline; keep it live while people review.
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const loaded = useMemo(() => facts.data?.pages.flatMap((page) => page.items) ?? [], [facts.data]);
  const total = facts.data?.pages.at(-1)?.total;
  const { years, undated } = useMemo(() => groupTimeline(loaded), [loaded]);

  return (
    <div className="grid gap-5 lg:grid-cols-[17rem_minmax(0,1fr)]">
      {sidebar()}
      <div className="min-w-0 space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <FactSearchForm query={state.query} onSearch={(query) => update({ query })} />
          <label className="flex cursor-pointer items-center gap-2 rounded-md border px-3 py-2 text-sm">
            <input type="checkbox" className="size-4 accent-primary" checked={state.includeWorking} onChange={(event) => update({ includeWorking: event.target.checked })} />
            Include unconfirmed
          </label>
          {canExport ? <FactExportDialog matterId={matterId} filters={filters} factCount={total} defaultFilename="timeline" /> : null}
        </div>
        {activeFilterBar}
        <Card>
          <CardContent className="space-y-2 pt-4">
            <DateHistogram
              compact
              unit={FACT_UNIT}
              intervals={["month", "year"]}
              interval={interval}
              onIntervalChange={(next) => setHistogramInterval(next === "month" ? "month" : "year")}
              buckets={histogram.data?.buckets ?? []}
              loading={histogram.isPending}
              error={histogram.isError}
              selectedFrom={state.dateFrom}
              selectedTo={state.dateTo}
              onSelect={({ from, to }) => update({ dateFrom: from, dateTo: to })}
            />
            {histogram.data?.undated_count ? (
              <p className="text-xs text-muted-foreground">{histogram.data.undated_count.toLocaleString()} undated {histogram.data.undated_count === 1 ? "fact is" : "facts are"} listed at the end.</p>
            ) : null}
          </CardContent>
        </Card>
        {facts.isPending ? <TableLoading />
          : facts.error ? <QueryError message={facts.error.message} />
          : !loaded.length ? (
            hasFilters || state.includeWorking
              ? <EmptyState title="No facts match" description="No facts on the timeline match these filters." />
              : <EmptyState title="The timeline is empty" description="Facts appear here as soon as a reviewer confirms them. Turn on Include unconfirmed to see working facts." />
          ) : (
            <div className="space-y-6">
              <p className="text-sm text-muted-foreground">
                {total?.toLocaleString()} {total === 1 ? "fact" : "facts"}{state.includeWorking ? ", confirmed and unconfirmed" : " confirmed"}, oldest first
              </p>
              {years.map((year) => (
                <section key={year.year} aria-labelledby={`timeline-year-${year.year}`} className="space-y-3">
                  <h3 id={`timeline-year-${year.year}`} className="text-lg font-semibold">{year.year}</h3>
                  {year.sections.map((section) => (
                    <div key={section.key} className="space-y-2">
                      <h4 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{section.label}</h4>
                      <ol className="ml-2 space-y-2 border-l pl-5">
                        {section.facts.map((fact) => <TimelineCard key={fact.id} fact={fact} clientId={clientId} matterId={matterId} issueNames={issueNames} />)}
                      </ol>
                    </div>
                  ))}
                </section>
              ))}
              {undated.length ? (
                <section aria-labelledby="timeline-undated" className="space-y-3">
                  <h3 id="timeline-undated" className="text-lg font-semibold">Undated</h3>
                  <ol className="ml-2 space-y-2 border-l pl-5">
                    {undated.map((fact) => <TimelineCard key={fact.id} fact={fact} clientId={clientId} matterId={matterId} issueNames={issueNames} />)}
                  </ol>
                </section>
              ) : null}
              {facts.hasNextPage ? (
                <div className="flex items-center justify-center gap-3">
                  <span className="text-sm text-muted-foreground">Showing {loaded.length.toLocaleString()} of {total?.toLocaleString()}</span>
                  <Button variant="outline" disabled={facts.isFetchingNextPage} onClick={() => void facts.fetchNextPage()}>
                    {facts.isFetchingNextPage ? "Loading…" : "Show more"}
                  </Button>
                </div>
              ) : null}
            </div>
          )}
      </div>
    </div>
  );
}
