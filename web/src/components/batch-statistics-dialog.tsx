"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, LoaderCircle, Upload } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { BatchCodingComparison } from "@/components/batch-coding-comparison";
import { QueryError } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { ReviewBatchFieldCodingCountRead, ReviewBatchRead, ReviewBatchReviewerStatisticsRead, ReviewBatchRunPromotionRead, ReviewBatchStatisticsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

function groupFieldCounts(fields: ReviewBatchFieldCodingCountRead[]) {
  const groups = new Map<string, { name: string; fields: ReviewBatchFieldCodingCountRead[] }>();
  for (const field of fields) {
    const group = groups.get(field.coding_group_id) ?? { name: field.coding_group_name, fields: [] };
    group.fields.push(field);
    groups.set(field.coding_group_id, group);
  }
  return [...groups.entries()].map(([id, group]) => ({ id, ...group }));
}

function promotionSummary(promotion: ReviewBatchRunPromotionRead) {
  if (promotion.status === "FAILED") return promotion.error_message ?? "Promotion failed.";
  if (promotion.status === "QUEUED") return `Promotion is queued${promotion.execution ? ` · attempt ${promotion.execution.attempt_number}` : ""}.`;
  const stage = promotion.execution ? `${promotion.execution.stage_label} · ` : "";
  return `${stage}${promotion.processed_document_count.toLocaleString()} of ${promotion.eligible_document_count.toLocaleString()} eligible documents promoted · ${promotion.skipped_document_count.toLocaleString()} excluded`;
}

function ReviewerStatistics({ reviewerCoding, batchDocumentCount, canPromote, promoting, onRequestPromotion }: {
  reviewerCoding: ReviewBatchReviewerStatisticsRead;
  batchDocumentCount: number;
  canPromote: boolean;
  promoting: boolean;
  onRequestPromotion: () => void;
}) {
  const groups = useMemo(() => groupFieldCounts(reviewerCoding.field_counts), [reviewerCoding.field_counts]);
  const promotionActive = ["QUEUED", "RUNNING"].includes(reviewerCoding.promotion?.status ?? "");

  return (
    <section className="rounded-lg border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{reviewerCoding.reviewer.display_name}</h3>
          </div>
          <p className="text-xs text-muted-foreground">{reviewerCoding.reviewer.email} · updated {formatDate(reviewerCoding.updated_at)}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs tabular-nums">
          <Badge variant="outline">{reviewerCoding.completed_document_count.toLocaleString()} completed</Badge>
          {reviewerCoding.skipped_document_count ? <Badge variant="outline">{reviewerCoding.skipped_document_count.toLocaleString()} skipped</Badge> : null}
          {reviewerCoding.incomplete_document_count ? <Badge variant="outline">{reviewerCoding.incomplete_document_count.toLocaleString()} incomplete</Badge> : null}
          <span className="text-muted-foreground">of {batchDocumentCount.toLocaleString()}</span>
        </div>
      </div>

      <div className="mt-4 space-y-3">
        {groups.map((group) => (
          <div key={group.id}>
            <h4 className="mb-1 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{group.name}</h4>
            <div className="divide-y rounded-md border">
              {group.fields.map((field) => (
                <div key={field.metadata_definition_id} className="flex items-center justify-between gap-4 px-3 py-2 text-sm">
                  <span>{field.field_display_name}</span>
                  <span className={cn("tabular-nums", field.document_count ? "font-medium" : "text-muted-foreground")}>
                    {field.document_count.toLocaleString()} coded
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
        {!groups.length ? <p className="text-sm text-muted-foreground">This batch has no snapshotted coding fields.</p> : null}
      </div>

      <div className="mt-4 border-t pt-3">
        {reviewerCoding.promotion ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className={cn("text-xs tabular-nums text-muted-foreground", reviewerCoding.promotion.status === "FAILED" && "text-destructive")}>
              {promotionSummary(reviewerCoding.promotion)}
            </p>
            {reviewerCoding.promotion.status === "COMPLETED" ? <Badge variant="accent"><Check />Promoted</Badge> : canPromote ? (
              <Button type="button" size="sm" disabled={promoting || promotionActive} onClick={onRequestPromotion}>
                {promoting || promotionActive ? <LoaderCircle className="animate-spin" /> : <Upload />}
                {reviewerCoding.promotion.status === "FAILED" ? "Retry promotion" : "Promoting…"}
              </Button>
            ) : null}
          </div>
        ) : reviewerCoding.completed_document_count > 0 && canPromote ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">Promote this reviewer&apos;s completed-document coding to the matter.</p>
            <Button type="button" size="sm" onClick={onRequestPromotion}><Upload />Promote coding</Button>
          </div>
        ) : reviewerCoding.completed_document_count > 0 ? (
          <p className="text-xs text-muted-foreground">Matter-manager permission is required to promote this coding.</p>
        ) : (
          <p className="text-xs text-muted-foreground">This reviewer has not completed any documents yet.</p>
        )}
      </div>
    </section>
  );
}

export function BatchStatisticsPage({ matterId, batchId }: {
  matterId: string;
  batchId: string;
}) {
  const queryClient = useQueryClient();
  const [view, setView] = useState<"comparison" | "progress">("comparison");
  const [confirmationReviewerCodingId, setConfirmationReviewerCodingId] = useState<string | null>(null);
  const batch = useQuery({
    queryKey: ["review-batch", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}`),
  });
  const statisticsKey = ["review-batch-statistics", matterId, batchId] as const;
  const statistics = useQuery({
    queryKey: statisticsKey,
    queryFn: () => coreApi<ReviewBatchStatisticsRead>(`/v1/matters/${matterId}/review-batches/${batchId}/statistics`),
    refetchInterval: (query) => query.state.data?.reviewers?.some((reviewer) => ["QUEUED", "RUNNING"].includes(reviewer.promotion?.status ?? "")) ? 1_500 : false,
  });
  const promote = useMutation({
    mutationFn: (reviewerCodingId: string) => coreApi<ReviewBatchRunPromotionRead>(`/v1/matters/${matterId}/review-batches/${batchId}/reviewer-coding/${reviewerCodingId}/promotion`, { method: "POST" }),
    onSuccess: () => {
      setConfirmationReviewerCodingId(null);
      void queryClient.invalidateQueries({ queryKey: statisticsKey });
      toast.success("Batch coding promotion was queued.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The batch coding could not be promoted."),
  });
  const confirmationReviewer = statistics.data?.reviewers?.find(
    (reviewer) => reviewer.reviewer_coding_id === confirmationReviewerCodingId,
  );

  return (
    <main id="main-content" className="min-h-dvh bg-background">
      <header className="border-b bg-card">
        <div className="mx-auto flex max-w-[96rem] flex-wrap items-center gap-4 px-6 py-5">
          <Button asChild size="sm" variant="ghost"><Link href={`/review/matters/${matterId}?batch=${batchId}`}><ArrowLeft />Back to batch</Link></Button>
          <div className="min-w-0 flex-1 border-l pl-4">
            <h1 className="truncate text-xl font-semibold">{batch.data?.name ?? "Batch statistics"}</h1>
            <p className="mt-1 text-sm text-muted-foreground">Compare human, JEV, agent, and classifier coding over the same frozen documents, or inspect human-review progress and promotion.</p>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-[96rem] space-y-5 px-6 py-6">
        {batch.error ? <QueryError message={batch.error.message} /> : null}

        <div className="flex gap-2 border-b pb-3">
          <Button type="button" size="sm" variant={view === "comparison" ? "default" : "ghost"} onClick={() => setView("comparison")}>Compare coding</Button>
          <Button type="button" size="sm" variant={view === "progress" ? "default" : "ghost"} onClick={() => setView("progress")}>Human progress &amp; promotion</Button>
        </div>

        {view === "comparison" ? <BatchCodingComparison matterId={matterId} batchId={batchId} active={view === "comparison"} /> : null}

        {view === "progress" && statistics.isPending ? <div className="space-y-3"><Skeleton className="h-24 w-full" /><Skeleton className="h-48 w-full" /></div> : null}
        {view === "progress" && statistics.error ? <QueryError message={statistics.error.message} /> : null}
        {view === "progress" && statistics.data ? (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-muted/50 px-4 py-3">
              <div><p className="text-sm font-semibold">{batch.data?.name ?? "Batch"}</p><p className="text-xs text-muted-foreground">Frozen batch membership</p></div>
              <p className="text-lg font-semibold tabular-nums">{statistics.data.document_count.toLocaleString()} <span className="text-sm font-normal text-muted-foreground">documents</span></p>
            </div>
            {(statistics.data.reviewers ?? []).map((reviewerCoding) => (
              <ReviewerStatistics
                key={reviewerCoding.reviewer_coding_id}
                reviewerCoding={reviewerCoding}
                batchDocumentCount={statistics.data.document_count}
                canPromote={statistics.data.can_promote}
                promoting={promote.isPending && promote.variables === reviewerCoding.reviewer_coding_id}
                onRequestPromotion={() => setConfirmationReviewerCodingId(reviewerCoding.reviewer_coding_id)}
              />
            ))}
            {!(statistics.data.reviewers ?? []).length ? <p className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">No reviewer coding has been recorded for this batch.</p> : null}
          </div>
        ) : null}

        {view === "progress" && confirmationReviewer ? (
          <div className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-4">
            <h3 className="font-semibold">Promote {confirmationReviewer.reviewer.display_name}&apos;s coding?</h3>
            <p className="mt-1 text-sm text-muted-foreground">
              For each of the {confirmationReviewer.completed_document_count.toLocaleString()} completed documents, this replaces every configured matter coding field with this reviewer&apos;s values. A field left blank clears its current matter value. Skipped and unfinished documents are not changed.
            </p>
            <div className="mt-3 flex justify-end gap-2">
              <Button type="button" variant="outline" disabled={promote.isPending} onClick={() => setConfirmationReviewerCodingId(null)}>Cancel</Button>
              <Button type="button" disabled={promote.isPending} onClick={() => promote.mutate(confirmationReviewer.reviewer_coding_id)}>
                {promote.isPending ? <LoaderCircle className="animate-spin" /> : <Upload />}Confirm promotion
              </Button>
            </div>
          </div>
        ) : null}

        <p className="border-t pt-4 text-xs text-muted-foreground">{view === "comparison" ? "Comparison reads authoritative batch-run values and does not modify matter coding or OpenSearch." : "Promotion is durable, audited, and reindexes the affected documents."}</p>
      </div>
    </main>
  );
}
