"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, ChevronRight, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { ClientRead, MatterRead, MatterTopicApplyRequest, MatterTopicJobRead, MatterTopicProposalReview, MetadataDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

type EditableProposal = MatterTopicProposalReview & {
  keywords: string[];
  representativeExcerpts: string[];
  sampledChunkCount: number;
};

export function TopicReviewPage({ clientId, matterId, jobId }: { clientId: string; matterId: string; jobId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const job = useQuery({ queryKey: ["matter-topic-job", matterId, jobId], queryFn: () => coreApi<MatterTopicJobRead>(`/v1/matters/${matterId}/topic-jobs/${jobId}`) });
  const definitions = useQuery({ queryKey: ["metadata-definitions", matterId], queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`) });
  const apply = useMutation({
    mutationFn: (payload: MatterTopicApplyRequest) => coreApi<MatterTopicJobRead>(`/v1/matters/${matterId}/topic-jobs/${jobId}/apply`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["matter-topic-job", matterId, jobId], updated);
      queryClient.setQueryData<MatterTopicJobRead[]>(["matter-topic-jobs", matterId], (current) => current?.map((item) => item.id === updated.id ? updated : item));
      void queryClient.invalidateQueries({ queryKey: ["metadata-definitions", matterId] });
      void queryClient.invalidateQueries({ queryKey: ["metadata-groups", matterId] });
      toast.success("The approved topics are being applied to the matter.");
      router.push(`/app/clients/${clientId}/matters/${matterId}?tab=jobs`);
    },
  });
  const jobsHref = `/app/clients/${clientId}/matters/${matterId}?tab=jobs`;

  if (client.isPending || matter.isPending || job.isPending || definitions.isPending) return <TableLoading />;
  if (client.error || matter.error || job.error || definitions.error) return <QueryError message={client.error?.message ?? matter.error?.message ?? job.error?.message ?? definitions.error?.message} />;
  const awaitingReview = job.data.status === "AWAITING_REVIEW";
  const hasProposals = Boolean(job.data.clusters?.length);

  return (
    <div className="min-w-0">
      <ResourcePageHeader
        breadcrumbs={<><Link href="/app/clients" className="hover:text-foreground">Clients</Link><ChevronRight className="size-4" /><Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link><ChevronRight className="size-4" /><Link href={jobsHref} className="hover:text-foreground">{matter.data.name}</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{awaitingReview ? "Topic review" : "Topic results"}</span></>}
        title={awaitingReview ? "Review proposed topics" : "View topic proposals"}
      />
      {awaitingReview || hasProposals
        ? <TopicReviewEditor job={job.data} definitions={definitions.data} jobsHref={jobsHref} submitting={apply.isPending} readOnly={!awaitingReview} onApply={(payload) => apply.mutateAsync(payload).then(() => undefined)} />
        : <Card className="p-6"><div className="flex flex-wrap items-start justify-between gap-4"><div><div className="mb-3"><StatusBadge status={job.data.status} /></div><h2 className="text-lg font-semibold">No topic proposals are available</h2><p className="mt-1 text-sm text-muted-foreground">This job did not retain a proposal set that can be displayed.</p></div><Button asChild variant="outline"><Link href={jobsHref}><ArrowLeft />Back to jobs</Link></Button></div></Card>}
    </div>
  );
}

function fieldKeyFromName(name: string) {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^[^a-z]+/, "").slice(0, 100);
}

function TopicReviewEditor({ job, definitions, jobsHref, submitting, readOnly, onApply }: {
  job: MatterTopicJobRead;
  definitions: MetadataDefinitionRead[];
  jobsHref: string;
  submitting: boolean;
  readOnly: boolean;
  onApply: (payload: MatterTopicApplyRequest) => Promise<void>;
}) {
  const [proposals, setProposals] = useState<EditableProposal[]>(() => (job.clusters ?? []).map((cluster) => ({
    id: cluster.id,
    name: cluster.name,
    description: cluster.description,
    included: cluster.included,
    keywords: cluster.keywords,
    representativeExcerpts: cluster.representative_excerpts,
    sampledChunkCount: cluster.sampled_chunk_count,
  })));
  const compatibleDefinitions = definitions.filter((definition) => definition.key !== "topics" && definition.status === "ACTIVE" && definition.value_source === "ASSERTED" && definition.type === "ENUM" && definition.cardinality === "MULTIPLE");
  const hasConfiguredDestination = Boolean(job.destination_mode);
  const [destinationMode, setDestinationMode] = useState<"TOPICS" | "EXISTING_FIELD" | "NEW_FIELD">(job.destination_mode ?? "TOPICS");
  const [existingDefinitionId, setExistingDefinitionId] = useState<string | undefined>(job.destination_metadata_definition_id ?? undefined);
  const [newFieldName, setNewFieldName] = useState(job.destination_mode === "NEW_FIELD" ? job.destination_field_name ?? "" : "");
  const [newFieldKey, setNewFieldKey] = useState(job.destination_mode === "NEW_FIELD" ? job.destination_field_key ?? "" : "");
  const [fieldKeyEdited, setFieldKeyEdited] = useState(false);
  const [error, setError] = useState<string>();

  function update(id: string, values: Partial<EditableProposal>) {
    setProposals((current) => current.map((proposal) => proposal.id === id ? { ...proposal, ...values } : proposal));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (readOnly) return;
    const included = proposals.filter((proposal) => proposal.included);
    if (!included.length) {
      setError("Include at least one topic before applying.");
      return;
    }
    if (proposals.some((proposal) => !proposal.name.trim())) {
      setError("Every topic proposal needs a name.");
      return;
    }
    if (!hasConfiguredDestination && destinationMode === "EXISTING_FIELD" && !existingDefinitionId) {
      setError("Select an existing destination field.");
      return;
    }
    if (!hasConfiguredDestination && destinationMode === "NEW_FIELD" && (!newFieldName.trim() || !newFieldKey.trim())) {
      setError("Enter a name and key for the new destination field.");
      return;
    }
    setError(undefined);
    try {
      const destination = hasConfiguredDestination ? {} : {
        destination_mode: destinationMode,
        ...(destinationMode === "EXISTING_FIELD" ? { existing_metadata_definition_id: existingDefinitionId } : {}),
        ...(destinationMode === "NEW_FIELD" ? { new_field_name: newFieldName.trim(), new_field_key: newFieldKey.trim() } : {}),
      };
      await onApply({
        topics: proposals.map((proposal) => ({
          id: proposal.id,
          name: proposal.name.trim(),
          description: proposal.description?.trim() || null,
          included: Boolean(proposal.included),
        })),
        ...destination,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The approved topics could not be applied.");
    }
  }

  const includedCount = proposals.filter((proposal) => proposal.included).length;
  return (
    <form onSubmit={submit} className="min-w-0 space-y-5">
      <div className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-4 rounded-xl border bg-background/95 p-4 shadow-sm backdrop-blur">
        <div><div className="flex flex-wrap items-center gap-2"><StatusBadge status={job.status} /><Badge variant="outline">{proposals.length.toLocaleString()} proposals</Badge><Badge variant="accent">{includedCount.toLocaleString()} included</Badge></div><p className="mt-2 text-sm text-muted-foreground">{readOnly ? "View the saved proposals and approval decisions from this clustering run." : "Edit names and descriptions, exclude weak topics, then apply the approved set to the frozen job scope."}</p></div>
        <div className="flex flex-wrap gap-2"><Button asChild type="button" variant="outline"><Link href={jobsHref}><ArrowLeft />Back to jobs</Link></Button>{!readOnly ? <Button type="submit" disabled={submitting || !proposals.length}>{submitting ? <LoaderCircle className="animate-spin" /> : <CheckCircle2 />}Apply {includedCount} {includedCount === 1 ? "topic" : "topics"}</Button> : null}</div>
      </div>

      {proposals.length === 1 ? <p role="status" className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-foreground">Automatic discovery produced only one proposal. Review its examples carefully; cancel this job and retry with a fixed topic count if it is too broad.</p> : null}
      {hasConfiguredDestination ? <section className="rounded-xl border bg-card p-5">
        <h2 className="font-semibold">Destination field</h2>
        <p className="mt-1 text-sm text-muted-foreground">Approved topics will be stored in <span className="font-medium text-foreground">{job.destination_field_name ?? "Topics"}</span>. This destination was selected when the run was created.</p>
      </section> : null}
      {!readOnly && !hasConfiguredDestination ? <section className="space-y-4 rounded-xl border bg-card p-5">
        <div><h2 className="font-semibold">Destination field</h2><p className="mt-1 text-sm text-muted-foreground">Choose where the approved topic values will be stored.</p></div>
        <div className="space-y-2">
          <Label>Field option</Label>
          <Select value={destinationMode} onValueChange={(value) => setDestinationMode(value as typeof destinationMode)}>
            <SelectTrigger aria-label="Destination field option"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="TOPICS">Use Topics field</SelectItem>
              <SelectItem value="EXISTING_FIELD" disabled={compatibleDefinitions.length === 0}>Choose an existing field</SelectItem>
              <SelectItem value="NEW_FIELD">Create a new field</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {destinationMode === "EXISTING_FIELD" ? (
          <div className="space-y-2">
            <Label>Existing field</Label>
            <Select value={existingDefinitionId} onValueChange={setExistingDefinitionId}>
              <SelectTrigger aria-label="Existing destination field"><SelectValue placeholder="Select a multi-value enum field" /></SelectTrigger>
              <SelectContent>{compatibleDefinitions.map((definition) => <SelectItem key={definition.id} value={definition.id}>{definition.display_name}</SelectItem>)}</SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">Approved topics will be added to the field&apos;s existing allowed values.</p>
          </div>
        ) : null}
        {destinationMode === "NEW_FIELD" ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="topic-field-name">Field name</Label><Input id="topic-field-name" value={newFieldName} maxLength={200} placeholder="Communication topics" onChange={(event) => { const value = event.target.value; setNewFieldName(value); if (!fieldKeyEdited) setNewFieldKey(fieldKeyFromName(value)); }} /></div>
            <div className="space-y-2"><Label htmlFor="topic-field-key">Field key</Label><Input id="topic-field-key" value={newFieldKey} maxLength={100} placeholder="communication_topics" pattern="[a-z][a-z0-9_]{0,99}" onChange={(event) => { setFieldKeyEdited(true); setNewFieldKey(event.target.value.toLowerCase()); }} /><p className="text-xs text-muted-foreground">Lowercase letters, numbers, and underscores.</p></div>
          </div>
        ) : null}
        {destinationMode === "TOPICS" ? <p className="text-xs text-muted-foreground">Priv-View will use the standard Topics field, creating it if necessary.</p> : null}
      </section> : null}
      {!readOnly ? <div className="flex flex-wrap justify-end gap-2"><Button type="button" size="sm" variant="outline" onClick={() => setProposals((current) => current.map((proposal) => ({ ...proposal, included: true })))}>Include all</Button><Button type="button" size="sm" variant="outline" onClick={() => setProposals((current) => current.map((proposal) => ({ ...proposal, included: false })))}>Exclude all</Button></div> : null}
      <div className="grid min-w-0 gap-5 xl:grid-cols-2">
        {proposals.map((proposal, index) => (
          <section key={proposal.id} className={`min-w-0 rounded-xl border p-5 ${proposal.included ? "bg-card" : "bg-muted/35 opacity-75"}`}>
            <div className="mb-4 flex items-start justify-between gap-4">
              <label className="flex items-center gap-2 text-sm font-semibold"><input type="checkbox" className="size-4 rounded border-input accent-primary" checked={Boolean(proposal.included)} disabled={readOnly} onChange={(event) => update(proposal.id, { included: event.target.checked })} />Include topic {index + 1}</label>
              <span className="text-xs tabular-nums text-muted-foreground">{proposal.sampledChunkCount.toLocaleString()} sampled chunks</span>
            </div>
            <div className="grid gap-4 md:grid-cols-2">
              <div className="space-y-2"><Label htmlFor={`topic-name-${proposal.id}`}>Topic name</Label><Input id={`topic-name-${proposal.id}`} maxLength={200} value={proposal.name} disabled={readOnly || !proposal.included} onChange={(event) => update(proposal.id, { name: event.target.value })} /></div>
              <div className="space-y-2"><Label htmlFor={`topic-description-${proposal.id}`}>Description</Label><Textarea id={`topic-description-${proposal.id}`} rows={3} maxLength={2000} value={proposal.description ?? ""} disabled={readOnly || !proposal.included} onChange={(event) => update(proposal.id, { description: event.target.value })} /></div>
            </div>
            {proposal.keywords.length ? <div className="mt-4 flex flex-wrap gap-1">{proposal.keywords.map((keyword) => <Badge key={keyword} variant="outline">{keyword}</Badge>)}</div> : null}
            {proposal.representativeExcerpts.length ? <div className="mt-4 space-y-2"><p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Representative excerpts</p>{proposal.representativeExcerpts.map((excerpt, excerptIndex) => <blockquote key={`${proposal.id}-${excerptIndex}`} className="break-words border-l-2 border-border pl-3 text-xs leading-5 text-muted-foreground">{excerpt}</blockquote>)}</div> : null}
          </section>
        ))}
      </div>
      {error ? <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
    </form>
  );
}
