"use client";

import { CheckCircle2, LoaderCircle } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { MatterTopicApplyRequest, MatterTopicJobRead, MatterTopicProposalReview } from "@/generated/models";

type EditableProposal = MatterTopicProposalReview & {
  keywords: string[];
  representativeExcerpts: string[];
  sampledChunkCount: number;
};

export function ReviewTopicProposalsDialog({ job, onApply }: {
  job: MatterTopicJobRead;
  onApply: (jobId: string, payload: MatterTopicApplyRequest) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [proposals, setProposals] = useState<EditableProposal[]>([]);
  const [error, setError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);

  function changeOpen(nextOpen: boolean) {
    setOpen(nextOpen);
    setError(undefined);
    if (nextOpen) {
      setProposals((job.clusters ?? []).map((cluster) => ({
        id: cluster.id,
        name: cluster.name,
        description: cluster.description,
        included: cluster.included,
        keywords: cluster.keywords,
        representativeExcerpts: cluster.representative_excerpts,
        sampledChunkCount: cluster.sampled_chunk_count,
      })));
    }
  }

  function update(id: string, values: Partial<EditableProposal>) {
    setProposals((current) => current.map((proposal) => proposal.id === id ? { ...proposal, ...values } : proposal));
  }

  async function apply() {
    const included = proposals.filter((proposal) => proposal.included);
    if (!included.length) {
      setError("Include at least one topic before applying.");
      return;
    }
    if (proposals.some((proposal) => !proposal.name.trim())) {
      setError("Every topic proposal needs a name.");
      return;
    }
    setSubmitting(true);
    setError(undefined);
    try {
      await onApply(job.id, {
        topics: proposals.map((proposal) => ({
          id: proposal.id,
          name: proposal.name.trim(),
          description: proposal.description?.trim() || null,
          included: Boolean(proposal.included),
        })),
      });
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The approved topics could not be applied.");
    } finally {
      setSubmitting(false);
    }
  }

  const includedCount = proposals.filter((proposal) => proposal.included).length;
  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild><Button size="sm"><CheckCircle2 />Review topics</Button></DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-4xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Review proposed topics</DialogTitle>
          <DialogDescription>Edit names and descriptions, exclude weak topics, then apply the approved set to all documents in the frozen job scope.</DialogDescription>
        </DialogHeader>
        {proposals.length === 1 ? <p role="status" className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-foreground">Automatic discovery produced only one proposal. Review its examples carefully; cancel this job and retry with a fixed topic count if it is too broad.</p> : null}
        <div className="space-y-4">
          {proposals.map((proposal, index) => (
            <section key={proposal.id} className={`rounded-xl border p-4 ${proposal.included ? "bg-card" : "bg-muted/35 opacity-75"}`}>
              <div className="mb-4 flex items-start justify-between gap-4">
                <label className="flex items-center gap-2 text-sm font-semibold">
                  <input type="checkbox" className="size-4 rounded border-input accent-primary" checked={Boolean(proposal.included)} onChange={(event) => update(proposal.id, { included: event.target.checked })} />
                  Include topic {index + 1}
                </label>
                <span className="text-xs tabular-nums text-muted-foreground">{proposal.sampledChunkCount.toLocaleString()} sampled chunks</span>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div className="space-y-2"><Label htmlFor={`topic-name-${proposal.id}`}>Topic name</Label><Input id={`topic-name-${proposal.id}`} maxLength={200} value={proposal.name} disabled={!proposal.included} onChange={(event) => update(proposal.id, { name: event.target.value })} /></div>
                <div className="space-y-2"><Label htmlFor={`topic-description-${proposal.id}`}>Description</Label><Textarea id={`topic-description-${proposal.id}`} rows={2} maxLength={2000} value={proposal.description ?? ""} disabled={!proposal.included} onChange={(event) => update(proposal.id, { description: event.target.value })} /></div>
              </div>
              {proposal.keywords.length ? <div className="mt-3 flex flex-wrap gap-1">{proposal.keywords.map((keyword) => <Badge key={keyword} variant="outline">{keyword}</Badge>)}</div> : null}
              {proposal.representativeExcerpts.length ? <div className="mt-4 space-y-2"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Representative excerpts</p>{proposal.representativeExcerpts.map((excerpt, excerptIndex) => <blockquote key={`${proposal.id}-${excerptIndex}`} className="border-l-2 border-border pl-3 text-xs leading-5 text-muted-foreground">{excerpt}</blockquote>)}</div> : null}
            </section>
          ))}
        </div>
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Close</Button><Button type="button" onClick={apply} disabled={submitting || !proposals.length}>{submitting ? <LoaderCircle className="animate-spin" /> : null}Apply {includedCount} {includedCount === 1 ? "topic" : "topics"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
