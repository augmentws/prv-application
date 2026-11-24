"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Bot, Building2, CheckCircle2, Play, Plus, Scale, Search, UserRound, XCircle } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { EmptyState, QueryError, TableLoading } from "@/components/query-state";
import { ResultPagination } from "@/components/result-pagination";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type {
  MatterEntityCreate,
  MatterEntityPage,
  MatterEntityRead,
  MatterIdentityAnalysisRead,
  MatterIdentityDomainDecisionCreate,
  MatterIdentityDomainRecommendationPage,
  MatterIdentityRecommendationRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { entityDetailHref, entityLabel } from "@/lib/facts";

export type EntitiesTab = "directory" | "proposed";

const PAGE_SIZE = 50;
const ENTITY_STATUS_CLASSES: Record<string, string> = {
  PROPOSED: "border border-pending/40 bg-pending/10 text-pending-foreground",
  CONFIRMED: "border border-confirmed/40 bg-confirmed/10 text-confirmed",
  REJECTED: "border border-rejected/40 bg-rejected/10 text-rejected",
};
const ENTITY_STATUS_LABELS: Record<string, string> = { PROPOSED: "Proposed", CONFIRMED: "Confirmed", REJECTED: "Rejected" };

export function EntityStatusBadge({ status }: { status: string }) {
  return <Badge variant={null} className={ENTITY_STATUS_CLASSES[status]}>{ENTITY_STATUS_LABELS[status] ?? status}</Badge>;
}

export function EntityTypeLabel({ type }: { type: string }) {
  const Icon = type === "ORGANIZATION" ? Building2 : UserRound;
  return <span className="inline-flex items-center gap-1.5 text-muted-foreground"><Icon className="size-4" aria-hidden="true" />{type === "ORGANIZATION" ? "Organization" : "Person"}</span>;
}

export function organizationCategoryLabel(category: string) {
  return category.replaceAll("_", " ").toLowerCase().replace(/^./, (value) => value.toUpperCase());
}

export function OrganizationCategoryBadge({ category }: { category: string }) {
  if (category === "LAW_FIRM") {
    return (
      <Badge variant={null} className="border border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300">
        <Scale className="size-3" aria-hidden="true" />Law firm · privilege signal
      </Badge>
    );
  }
  return <Badge variant="outline">{organizationCategoryLabel(category)}</Badge>;
}

function IdentityDomainReview({
  clientId,
  matterId,
  analysis,
  canManage,
  onEntitiesChanged,
}: {
  clientId: string;
  matterId: string;
  analysis: MatterIdentityAnalysisRead;
  canManage: boolean;
  onEntitiesChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const runs = useQuery({
    queryKey: ["matter-identity-domain-recommendation-runs", matterId, analysis.id],
    queryFn: () => coreApi<MatterIdentityRecommendationRunRead[]>(
      `/v1/matters/${matterId}/identity-analysis-jobs/${analysis.id}/domain-recommendation-runs?limit=5`,
    ),
    refetchInterval: (query) => query.state.data?.some((run) => run.status === "QUEUED" || run.status === "RUNNING") ? 3_000 : false,
  });
  const latestRun = runs.data?.[0];
  const active = latestRun?.status === "QUEUED" || latestRun?.status === "RUNNING";
  const recommendations = useQuery({
    queryKey: ["matter-identity-domain-recommendations", matterId, analysis.id, latestRun?.id],
    queryFn: () => coreApi<MatterIdentityDomainRecommendationPage>(
      `/v1/matters/${matterId}/identity-analysis-jobs/${analysis.id}/domain-recommendation-runs/${latestRun?.id}/recommendations`,
    ),
    enabled: latestRun?.status === "COMPLETED",
  });
  const start = useMutation({
    mutationFn: () => coreApi<MatterIdentityRecommendationRunRead>(
      `/v1/matters/${matterId}/identity-analysis-jobs/${analysis.id}/domain-recommendation-runs`,
      { method: "POST" },
    ),
    onSuccess: () => {
      toast.success("Organization domain review queued");
      void queryClient.invalidateQueries({ queryKey: ["matter-identity-domain-recommendation-runs", matterId, analysis.id] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Domain review could not be started"),
  });
  const decide = useMutation({
    mutationFn: (payload: MatterIdentityDomainDecisionCreate) => coreApi(
      `/v1/matters/${matterId}/identity-analysis-jobs/${analysis.id}/domain-recommendation-runs/${latestRun?.id}/decisions`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
    onSuccess: (_result, payload) => {
      toast.success(payload.decision === "ACCEPTED" ? "Organization added to the entity directory" : "Candidate rejected");
      void queryClient.invalidateQueries({ queryKey: ["matter-identity-domain-recommendations", matterId, analysis.id, latestRun?.id] });
      onEntitiesChanged();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The candidate decision could not be saved"),
  });
  const candidateRows = recommendations.data?.recommendations ?? [];
  const pendingCandidates = candidateRows.filter((candidate) => !candidate.decision && !candidate.proposed_entity_id);
  const acceptedCandidateCount = candidateRows.filter((candidate) => candidate.decision === "ACCEPTED").length;
  const rejectedCandidateCount = candidateRows.filter((candidate) => candidate.decision === "REJECTED").length;
  const proposedCandidateCount = new Set(
    candidateRows
      .filter((candidate) => candidate.proposed_entity_id && (candidate.publication_status === "PROPOSED" || candidate.publication_status === "ALREADY_PROPOSED"))
      .map((candidate) => candidate.proposed_entity_id),
  ).size;
  const confirmedCandidateCount = new Set(
    candidateRows
      .filter((candidate) => candidate.proposed_entity_id && candidate.publication_status === "ALREADY_CONFIRMED")
      .map((candidate) => candidate.proposed_entity_id),
  ).size;

  return (
    <div className="mt-4 border-t pt-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">Organizations identified from domains</h3>
          <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
            Review aggregate domain metadata for credible organizations. Recommendations are added to Proposed Entities for confirmation. A law-firm category is only a signal: it does not establish representation or make any document privileged.
          </p>
        </div>
        {canManage ? (
          <Button variant="outline" disabled={active || start.isPending} onClick={() => start.mutate()}>
            <Play aria-hidden="true" />
            {latestRun?.status === "QUEUED"
              ? "Domain review queued"
              : latestRun?.status === "RUNNING"
                ? "Domain review running"
                : latestRun
                  ? "Run domain review again"
                  : "Review domains"}
          </Button>
        ) : null}
      </div>
      {runs.isPending ? <p className="mt-3 text-sm text-muted-foreground">Loading domain-review status…</p>
        : runs.error ? <p className="mt-3 text-sm text-destructive">Domain-review status is unavailable.</p>
        : latestRun ? (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-x-5 gap-y-1 text-sm">
              <span><span className="text-muted-foreground">Status:</span> {latestRun.status.replaceAll("_", " ").toLowerCase()}</span>
              <span className="tabular-nums"><span className="text-muted-foreground">Domains reviewed:</span> {latestRun.processed_domain_count.toLocaleString()} / {latestRun.domain_count.toLocaleString()}</span>
              {latestRun.status === "COMPLETED" ? <span className="tabular-nums"><span className="text-muted-foreground">Candidates:</span> {latestRun.recommendation_count.toLocaleString()}</span> : null}
            </div>
            {latestRun.failure_summary ? <p className="text-sm text-destructive">{latestRun.failure_summary}</p> : null}
            {recommendations.isPending ? <p className="text-sm text-muted-foreground">Loading candidates…</p>
              : recommendations.error ? <p className="text-sm text-destructive">Candidates could not be loaded.</p>
              : recommendations.data ? (
                <div className="space-y-2">
                  <p className="text-xs text-muted-foreground">
                    {proposedCandidateCount.toLocaleString()} proposed · {confirmedCandidateCount.toLocaleString()} already confirmed · {pendingCandidates.length.toLocaleString()} legacy candidates · {acceptedCandidateCount.toLocaleString()} accepted · {rejectedCandidateCount.toLocaleString()} rejected
                  </p>
                  {proposedCandidateCount ? (
                    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-muted/30 p-3">
                      <p className="text-sm">{proposedCandidateCount.toLocaleString()} organization {proposedCandidateCount === 1 ? "proposal is" : "proposals are"} awaiting entity review.</p>
                      <Button asChild size="sm" variant="outline">
                        <Link href={`/app/clients/${clientId}/matters/${matterId}/entities/proposed`}>Review proposed entities</Link>
                      </Button>
                    </div>
                  ) : null}
                  {pendingCandidates.length ? (
                    <div className="divide-y rounded-md border">
                      {pendingCandidates.map((candidate) => (
                        <div key={candidate.domain} className="grid gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_auto]">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <p className="font-medium">{candidate.organization_name}</p>
                              <Badge variant="outline">{Math.round(candidate.confidence * 100)}% confidence</Badge>
                            </div>
                            <p className="mt-1 text-sm"><span className="font-mono">{candidate.domain}</span>{candidate.canonical_domain !== candidate.domain ? ` → ${candidate.canonical_domain}` : ""}</p>
                            <p className="mt-1 text-sm text-muted-foreground">{candidate.rationale}</p>
                            {candidate.evidence_signals.length ? <p className="mt-1 text-xs text-muted-foreground">Signals: {candidate.evidence_signals.join(" · ")}</p> : null}
                          </div>
                          {canManage ? (
                            <div className="flex items-start gap-2">
                              <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate({ domain: candidate.domain, decision: "ACCEPTED", organization_name: candidate.organization_name })}>Accept organization</Button>
                              <Button size="sm" variant="ghost" disabled={decide.isPending} onClick={() => decide.mutate({ domain: candidate.domain, decision: "REJECTED" })}>Reject</Button>
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : candidateRows.length ? (
                    <p className="text-sm text-muted-foreground">All recommended domains have been reviewed.</p>
                  ) : (
                    <p className="text-sm text-muted-foreground">No credible organizations were recommended from the domain metadata.</p>
                  )}
                </div>
              ) : null}
          </div>
        ) : <p className="mt-3 text-sm text-muted-foreground">Run this after Identity Analysis to create a reviewable candidate list.</p>}
    </div>
  );
}

/** Names of the matter's organizations, for showing a person's organization. */
export function useOrganizationNames(matterId: string, enabled = true) {
  const organizations = useQuery({
    queryKey: ["matter-entity-organizations", matterId],
    queryFn: () => coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?entity_type=ORGANIZATION&limit=500`),
    enabled,
    staleTime: 60_000,
  });
  const names = useMemo(() => new Map((organizations.data?.items ?? []).map((item) => [item.id, entityLabel(item)])), [organizations.data]);
  return { organizations, names };
}

function readState(params: URLSearchParams) {
  const page = Number.parseInt(params.get("epage") ?? "1", 10);
  const type = params.get("etype");
  const status = params.get("estatus");
  return {
    query: params.get("eq") ?? "",
    type: type === "PERSON" || type === "ORGANIZATION" ? type : "",
    status: status === "REJECTED" ? "REJECTED" : "CONFIRMED",
    page: Number.isFinite(page) && page > 0 ? page : 1,
  };
}

export function EntitiesPanel({ clientId, matterId, tab, canManage }: { clientId: string; matterId: string; tab: EntitiesTab; canManage: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const state = useMemo(() => readState(new URLSearchParams(searchParams.toString())), [searchParams]);
  const [queryDraft, setQueryDraft] = useState(state.query);
  const [rejecting, setRejecting] = useState<MatterEntityRead | null>(null);
  const [creating, setCreating] = useState(false);
  const { names: organizationNames } = useOrganizationNames(matterId);
  const status = tab === "proposed" ? "PROPOSED" : state.status;
  const offset = (state.page - 1) * PAGE_SIZE;

  const identityAnalyses = useQuery({
    queryKey: ["matter-identity-analyses", matterId],
    queryFn: () => coreApi<MatterIdentityAnalysisRead[]>(`/v1/matters/${matterId}/identity-analysis-jobs?limit=5`),
    refetchInterval: (query) => query.state.data?.some((job) => job.status === "QUEUED" || job.status === "RUNNING") ? 3_000 : false,
  });

  function update(patch: Partial<ReturnType<typeof readState>>) {
    const next = { ...state, page: 1, ...patch };
    const params = new URLSearchParams(searchParams.toString());
    for (const key of ["eq", "etype", "estatus", "epage"]) params.delete(key);
    if (next.query.trim()) params.set("eq", next.query.trim());
    if (next.type) params.set("etype", next.type);
    if (next.status !== "CONFIRMED") params.set("estatus", next.status);
    if (next.page > 1) params.set("epage", String(next.page));
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  const entities = useQuery({
    queryKey: ["matter-entities", matterId, status, state.type, state.query, offset],
    queryFn: () => {
      const params = new URLSearchParams({ status, limit: String(PAGE_SIZE), offset: String(offset) });
      if (state.type) params.set("entity_type", state.type);
      if (state.query.trim()) params.set("q", state.query.trim());
      return coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?${params}`);
    },
    placeholderData: keepPreviousData,
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["matter-entities", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-entity-organizations", matterId] });
  }

  const review = useMutation({
    mutationFn: ({ entity, action }: { entity: MatterEntityRead; action: "confirm" | "reject" }) =>
      coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities/${entity.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ lock_version: entity.lock_version }),
      }),
    onSuccess: (entity, { action }) => {
      toast.success(`${entityLabel(entity)} ${action === "confirm" ? "confirmed" : "rejected"}`);
      setRejecting(null);
      refresh();
      // Rejecting removes the entity from facts, so fact lists must reload.
      if (action === "reject") void queryClient.invalidateQueries({ queryKey: ["matter-facts", matterId] });
    },
    onError: (error) => {
      toast.error(error instanceof Error ? error.message : "The entity could not be changed");
      refresh();
    },
  });

  const create = useMutation({
    mutationFn: (payload: MatterEntityCreate) => coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (entity) => {
      toast.success(`${entityLabel(entity)} added`);
      setCreating(false);
      refresh();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The entity could not be added"),
  });

  const startIdentityAnalysis = useMutation({
    mutationFn: () => coreApi<MatterIdentityAnalysisRead>(`/v1/matters/${matterId}/identity-analysis-jobs`, { method: "POST" }),
    onSuccess: () => {
      toast.success("Identity Analysis queued");
      void queryClient.invalidateQueries({ queryKey: ["matter-identity-analyses", matterId] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Identity Analysis could not be started"),
  });

  const latestIdentityAnalysis = identityAnalyses.data?.[0];
  const identityAnalysisActive = latestIdentityAnalysis?.status === "QUEUED" || latestIdentityAnalysis?.status === "RUNNING";

  const columns: ColumnDef<MatterEntityRead>[] = [
    {
      id: "name",
      header: "Name",
      size: 260,
      cell: ({ row }) => (
        <div className="min-w-0">
          <Link href={entityDetailHref(clientId, matterId, row.original.id)} className="font-medium hover:text-primary">{entityLabel(row.original)}</Link>
          {row.original.created_by_workflow_run_id ? <span className="ml-2 inline-flex items-center gap-1 text-xs text-agent"><Bot className="size-3" aria-hidden="true" />Agent</span> : null}
          {row.original.variants?.length ? <p className="truncate text-xs text-muted-foreground">Also: {row.original.variants.map((variant) => variant.email ?? entityLabel(variant)).join(", ")}</p> : null}
        </div>
      ),
    },
    {
      id: "type",
      header: "Type",
      size: 160,
      cell: ({ row }) => (
        <div className="space-y-1">
          <EntityTypeLabel type={row.original.entity_type} />
          {row.original.organization_category ? <OrganizationCategoryBadge category={row.original.organization_category} /> : null}
        </div>
      ),
    },
    {
      id: "role",
      header: "Title and organization",
      size: 240,
      cell: ({ row }) => {
        const organization = row.original.organization_id ? organizationNames.get(row.original.organization_id) : undefined;
        const parts = [row.original.title, organization].filter(Boolean);
        return <span className="truncate text-muted-foreground">{parts.length ? parts.join(", ") : "—"}</span>;
      },
    },
    { id: "email", header: "Email", size: 220, cell: ({ row }) => <span className="truncate">{row.original.email ?? "—"}</span> },
    { id: "facts", header: "Facts", size: 80, cell: ({ row }) => <span className="tabular-nums">{row.original.fact_count}</span> },
    { id: "status", header: "Status", size: 120, cell: ({ row }) => <EntityStatusBadge status={row.original.status} /> },
    ...(canManage && tab === "proposed"
      ? [{
          id: "actions",
          header: "Review",
          size: 210,
          cell: ({ row }) => (
            <div className="flex gap-1">
              <Button size="sm" variant="outline" disabled={review.isPending} onClick={() => review.mutate({ entity: row.original, action: "confirm" })} aria-label={`Confirm ${entityLabel(row.original)}`}>
                <CheckCircle2 aria-hidden="true" />Confirm
              </Button>
              <Button size="sm" variant="ghost" disabled={review.isPending} onClick={() => setRejecting(row.original)} aria-label={`Reject ${entityLabel(row.original)}`}>
                <XCircle aria-hidden="true" />Reject
              </Button>
            </div>
          ),
        } satisfies ColumnDef<MatterEntityRead>]
      : []),
  ];

  return (
    <div className="space-y-3">
      {tab === "directory" ? (
        <section className="rounded-lg border bg-card p-4" aria-labelledby="identity-analysis-title">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 id="identity-analysis-title" className="font-semibold">Identity Analysis</h2>
              <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
                Inventory email addresses and domains from structured matter metadata. This does not read document text, create entities, or determine privilege.
              </p>
            </div>
            {canManage ? (
              <Button variant="outline" disabled={identityAnalysisActive || startIdentityAnalysis.isPending} onClick={() => startIdentityAnalysis.mutate()}>
                <Play aria-hidden="true" />{identityAnalysisActive ? "Analysis running" : "Run Identity Analysis"}
              </Button>
            ) : null}
          </div>
          {identityAnalyses.isPending ? <p className="mt-3 text-sm text-muted-foreground">Loading recent analyses…</p>
            : identityAnalyses.error ? <p className="mt-3 text-sm text-destructive">Identity Analysis status is unavailable.</p>
            : latestIdentityAnalysis ? (
              <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-5">
                <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Status</p><p className="font-medium">{latestIdentityAnalysis.status.replaceAll("_", " ").toLowerCase()}</p></div>
                <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Documents</p><p className="font-medium tabular-nums">{latestIdentityAnalysis.processed_document_count.toLocaleString()} / {latestIdentityAnalysis.total_document_count.toLocaleString()}</p></div>
                <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">With participants</p><p className="font-medium tabular-nums">{latestIdentityAnalysis.documents_with_participants_count.toLocaleString()}</p></div>
                <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Email addresses</p><p className="font-medium tabular-nums">{latestIdentityAnalysis.identity_count.toLocaleString()}</p></div>
                <div><p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Domains</p><p className="font-medium tabular-nums">{latestIdentityAnalysis.domain_count.toLocaleString()}</p></div>
                {latestIdentityAnalysis.failure_summary ? <p className="sm:col-span-2 lg:col-span-5 text-destructive">{latestIdentityAnalysis.failure_summary}</p> : null}
              </div>
            ) : <p className="mt-3 text-sm text-muted-foreground">No Identity Analysis has been run for this matter.</p>}
          {latestIdentityAnalysis?.status === "COMPLETED" ? (
            <IdentityDomainReview clientId={clientId} matterId={matterId} analysis={latestIdentityAnalysis} canManage={canManage} onEntitiesChanged={refresh} />
          ) : null}
        </section>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <form className="flex min-w-0 flex-1 items-center gap-2" role="search" onSubmit={(event: FormEvent) => { event.preventDefault(); update({ query: queryDraft }); }}>
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
            <Input value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="Search names and email addresses" aria-label="Search entities" className="pl-9" />
          </div>
          <Button type="submit" variant="outline">Search</Button>
        </form>
        <Select value={state.type || "ALL"} onValueChange={(value) => update({ type: value === "ALL" ? "" : value })}>
          <SelectTrigger className="w-44" aria-label="Entity type"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="ALL">People and organizations</SelectItem>
            <SelectItem value="PERSON">People</SelectItem>
            <SelectItem value="ORGANIZATION">Organizations</SelectItem>
          </SelectContent>
        </Select>
        {tab === "directory" ? (
          <Select value={state.status} onValueChange={(value) => update({ status: value })}>
            <SelectTrigger className="w-36" aria-label="Entity status"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="CONFIRMED">Confirmed</SelectItem>
              <SelectItem value="REJECTED">Rejected</SelectItem>
            </SelectContent>
          </Select>
        ) : null}
        {canManage ? <Button onClick={() => setCreating(true)}><Plus aria-hidden="true" />New entity</Button> : null}
      </div>
      {entities.isPending ? <TableLoading />
        : entities.error ? <QueryError message={entities.error.message} />
        : !entities.data.total && !state.query && !state.type ? (
          tab === "proposed"
            ? <EmptyState title="Nothing to review" description="People and organizations that agents find in documents appear here until a reviewer confirms or rejects them." />
            : <EmptyState title={state.status === "REJECTED" ? "No rejected entities" : "No confirmed entities yet"} description="Confirmed people and organizations appear here. Agents propose new ones as they read documents." />
        ) : (
          <>
            <p className="text-sm text-muted-foreground">{entities.data.total.toLocaleString()} {entities.data.total === 1 ? "entity" : "entities"}</p>
            <DataTable columns={columns} data={entities.data.items} emptyMessage="No people or organizations match." />
            <ResultPagination total={entities.data.total} offset={offset} pageSize={PAGE_SIZE} disabled={entities.isFetching} onPageChange={(nextOffset) => update({ page: Math.floor(nextOffset / PAGE_SIZE) + 1 })} />
          </>
        )}
      <RejectEntityDialog entity={rejecting} busy={review.isPending} onCancel={() => setRejecting(null)} onReject={(entity) => review.mutate({ entity, action: "reject" })} />
      <CreateEntityDialog open={creating} busy={create.isPending} onClose={() => setCreating(false)} onCreate={(payload) => create.mutate(payload)} />
    </div>
  );
}

export function RejectEntityDialog({ entity, busy, onCancel, onReject }: { entity: MatterEntityRead | null; busy: boolean; onCancel: () => void; onReject: (entity: MatterEntityRead) => void }) {
  return (
    <Dialog open={Boolean(entity)} onOpenChange={(open) => { if (!open) onCancel(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Reject {entity ? entityLabel(entity) : "entity"}?</DialogTitle>
          <DialogDescription>
            {entity?.fact_count
              ? `It will be removed as owner or participant from ${entity.fact_count} ${entity.fact_count === 1 ? "fact" : "facts"}. The facts themselves are kept.`
              : "It will no longer be offered as a fact's owner or participant."}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          <Button variant="destructive" disabled={busy || !entity} onClick={() => entity && onReject(entity)}>Reject</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CreateEntityDialog({ open, busy, onClose, onCreate }: { open: boolean; busy: boolean; onClose: () => void; onCreate: (payload: MatterEntityCreate) => void }) {
  const [type, setType] = useState<"PERSON" | "ORGANIZATION">("PERSON");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [title, setTitle] = useState("");
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New person or organization</DialogTitle>
          <DialogDescription>Reviewer-created entities are confirmed immediately.</DialogDescription>
        </DialogHeader>
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            onCreate({
              entity_type: type,
              display_name: name.trim() || null,
              email: email.trim() || null,
              title: type === "PERSON" ? title.trim() || null : null,
              status: "CONFIRMED",
            });
          }}
        >
          <div className="space-y-2">
            <Label htmlFor="entity-type">Type</Label>
            <Select value={type} onValueChange={(value) => setType(value as "PERSON" | "ORGANIZATION")}>
              <SelectTrigger id="entity-type"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="PERSON">Person</SelectItem>
                <SelectItem value="ORGANIZATION">Organization</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="entity-name">Name</Label>
            <Input id="entity-name" value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="entity-email">Email address</Label>
            <Input id="entity-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
          </div>
          {type === "PERSON" ? (
            <div className="space-y-2">
              <Label htmlFor="entity-title">Title</Label>
              <Input id="entity-title" value={title} onChange={(event) => setTitle(event.target.value)} />
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy || (!name.trim() && !email.trim())}>Add</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
