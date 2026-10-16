"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bot, CheckCircle2, ChevronRight, Eye, GitMerge, Pencil, Split, Trash2, XCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, type ReactNode, useMemo, useState } from "react";
import { toast } from "sonner";

import { FactEvidenceViewer } from "@/components/facts/fact-evidence-viewer";
import { FactStatusBadge } from "@/components/facts/fact-status-badge";
import { useIssueNames } from "@/components/facts/facts-list-panel";
import { QueryError, TableLoading } from "@/components/query-state";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  ClientRead,
  EffectivePermissionsRead,
  FactIssueCatalogEntry,
  MatterEntityPage,
  MatterEntityRead,
  MatterFactDetailRead,
  MatterFactEvidenceRead,
  MatterFactPage,
  MatterFactRead,
  MatterFactSuggestionRead,
  MatterFactUpdate,
  MatterRead,
} from "@/generated/models";
import { ApiError, coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { factDetailHref, formatFactDate } from "@/lib/facts";

const PRECISIONS = ["DAY", "MONTH", "QUARTER", "YEAR", "RANGE", "UNKNOWN"] as const;
const PRECISION_LABELS: Record<string, string> = { DAY: "Day", MONTH: "Month", QUARTER: "Quarter", YEAR: "Year", RANGE: "Date range", UNKNOWN: "Undated" };
const BASES = ["STATED_IN_TEXT", "DOCUMENT_DATE", "INFERRED"] as const;
const BASIS_LABELS: Record<string, string> = { STATED_IN_TEXT: "Stated in the text", DOCUMENT_DATE: "The document's date", INFERRED: "Inferred" };
const ROLE_LABELS: Record<string, string> = { PARTICIPANT_ACTOR: "Actor", PARTICIPANT_RECIPIENT: "Recipient" };
const FIELD_LABELS: Record<string, string> = {
  summary: "Summary",
  date_precision: "Date precision",
  event_date_start: "Date",
  event_date_end: "End date",
  date_basis: "Date basis",
  owner_entity_id: "Owner",
  issues: "Issues",
  participants: "Participants",
};

type IssueChoice = `${string}:${string}`;

interface EditState {
  summary: string;
  datePrecision: string;
  start: string;
  end: string;
  dateBasis: string;
  ownerId: string;
  keyFact: boolean;
  issues: IssueChoice[];
  participants: { entity_id: string; role: string }[];
}

function editStateFor(fact: MatterFactRead): EditState {
  return {
    summary: fact.summary,
    datePrecision: fact.date_precision,
    start: fact.event_date_start ?? "",
    end: fact.event_date_end ?? "",
    dateBasis: fact.date_basis ?? "",
    ownerId: fact.owner?.entity_id ?? "",
    keyFact: fact.key_fact,
    issues: fact.issues.map((issue) => `${issue.matter_analysis_task_id}:${issue.issue_key}` as IssueChoice),
    participants: fact.participants.map((participant) => ({ entity_id: participant.entity_id, role: participant.role ?? "PARTICIPANT_ACTOR" })),
  };
}

function updatePayload(fact: MatterFactRead, edit: EditState): MatterFactUpdate {
  const payload: MatterFactUpdate = { lock_version: fact.lock_version };
  const undated = edit.datePrecision === "UNKNOWN";
  if (edit.summary.trim() !== fact.summary) payload.summary = edit.summary.trim();
  if (
    edit.datePrecision !== fact.date_precision ||
    (edit.start || null) !== (fact.event_date_start ?? null) ||
    (edit.end || null) !== (fact.event_date_end ?? null) ||
    (edit.dateBasis || null) !== (fact.date_basis ?? null)
  ) {
    payload.date_precision = edit.datePrecision as MatterFactUpdate["date_precision"];
    payload.event_date_start = undated ? null : edit.start || null;
    payload.event_date_end = undated || edit.datePrecision !== "RANGE" ? null : edit.end || null;
    payload.date_basis = undated ? null : ((edit.dateBasis || null) as MatterFactUpdate["date_basis"]);
  }
  if ((edit.ownerId || null) !== (fact.owner?.entity_id ?? null)) payload.owner_entity_id = edit.ownerId || null;
  if (edit.keyFact !== fact.key_fact) payload.key_fact = edit.keyFact;
  const currentIssues = editStateFor(fact).issues.slice().sort().join("|");
  if (edit.issues.slice().sort().join("|") !== currentIssues) {
    payload.issues = edit.issues.map((choice) => {
      const separator = choice.indexOf(":");
      return { matter_analysis_task_id: choice.slice(0, separator), issue_key: choice.slice(separator + 1) };
    });
  }
  const participantKey = (items: { entity_id: string; role: string }[]) => items.map((item) => `${item.entity_id}:${item.role}`).sort().join("|");
  if (participantKey(edit.participants) !== participantKey(editStateFor(fact).participants)) {
    payload.participants = edit.participants.filter((item) => item.entity_id) as MatterFactUpdate["participants"];
  }
  return payload;
}

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError && error.status === 409) return `${error.message}. The fact was reloaded; review it and try again.`;
  return error instanceof Error ? error.message : fallback;
}

export function FactDetail({ clientId, matterId, factId }: { clientId: string; matterId: string; factId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [viewing, setViewing] = useState<MatterFactEvidenceRead | null>(null);
  const [mergeOpen, setMergeOpen] = useState(false);
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const factKey = ["matter-fact", matterId, factId] as const;
  const fact = useQuery({ queryKey: factKey, queryFn: () => coreApi<MatterFactDetailRead>(`/v1/matters/${matterId}/facts/${factId}`) });
  const suggestions = useQuery({
    queryKey: ["matter-fact-suggestions", matterId, factId],
    queryFn: () => coreApi<MatterFactSuggestionRead[]>(`/v1/matters/${matterId}/facts/${factId}/suggestions`),
  });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const canReview = permissionKeys.has("fact.review");
  const { catalog, names: issueNames } = useIssueNames(matterId);
  const entities = useQuery({
    queryKey: ["matter-entities-picker", matterId],
    queryFn: () => coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?limit=500&status=CONFIRMED&status=PROPOSED`),
    enabled: permissionKeys.has("entity.view"),
    staleTime: 60_000,
  });
  const entityNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const entity of entities.data?.items ?? []) map.set(entity.id, entity.display_name);
    for (const ref of [fact.data?.owner, ...(fact.data?.participants ?? [])]) if (ref) map.set(ref.entity_id, ref.canonical_display_name);
    return map;
  }, [entities.data, fact.data]);

  function refreshAfterChange(updated?: MatterFactDetailRead) {
    if (updated && updated.id === factId) queryClient.setQueryData(factKey, updated);
    void queryClient.invalidateQueries({ queryKey: factKey });
    void queryClient.invalidateQueries({ queryKey: ["matter-fact-suggestions", matterId, factId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-facts", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-fact-facet", matterId] });
  }

  const action = useMutation({
    mutationFn: ({ path, method = "POST", body }: { path: string; method?: string; body?: unknown; success: string }) =>
      coreApi<MatterFactDetailRead>(`/v1/matters/${matterId}/facts/${factId}${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body) }),
    onSuccess: (updated, variables) => {
      toast.success(variables.success);
      if (updated.id !== factId) {
        refreshAfterChange();
        router.push(factDetailHref(clientId, matterId, updated.id));
        return;
      }
      refreshAfterChange(updated);
      setEditing(false);
    },
    onError: (error) => {
      toast.error(errorMessage(error, "The fact could not be changed"));
      refreshAfterChange();
    },
  });

  if (fact.isPending || client.isPending || matter.isPending) return <TableLoading />;
  if (fact.error || client.error || matter.error) return <QueryError message={fact.error?.message ?? client.error?.message ?? matter.error?.message} />;
  const current = fact.data;
  const lock = { lock_version: current.lock_version };
  const active = current.status === "WORKING" || current.status === "CONFIRMED";
  const pending = (suggestions.data ?? []).filter((item) => item.status === "PENDING");
  const activeEvidence = current.evidence.filter((item) => item.status === "ACTIVE");
  const removedEvidence = current.evidence.filter((item) => item.status === "REMOVED");
  const factsHref = `/app/clients/${clientId}/matters/${matterId}?tab=facts`;

  return (
    <div className="space-y-5">
      <ResourcePageHeader
        title="Fact"
        breadcrumbs={
          <>
            <Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link>
            <ChevronRight className="size-4" aria-hidden="true" />
            <Link href={`/app/clients/${clientId}/matters/${matterId}`} className="hover:text-foreground">{matter.data.name}</Link>
            <ChevronRight className="size-4" aria-hidden="true" />
            <Link href={factsHref} className="hover:text-foreground">Facts</Link>
          </>
        }
      />
      <Link href={factsHref} className="inline-flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" aria-hidden="true" />Back to facts
      </Link>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <FactStatusBadge status={current.status} />
              {current.key_fact ? <Badge variant="accent">Key fact</Badge> : null}
              {current.has_contradiction ? <Badge variant="outline" className="text-conflict">Contradicted</Badge> : null}
              {current.origin_workflow_run_id ? <span className="inline-flex items-center gap-1 text-xs font-medium text-agent"><Bot className="size-3.5" aria-hidden="true" />Proposed by an agent</span> : null}
            </div>
            <CardTitle className="text-lg leading-snug">{current.summary}</CardTitle>
            <p className="text-sm text-muted-foreground">{formatFactDate(current)}{current.date_basis ? ` · ${BASIS_LABELS[current.date_basis] ?? current.date_basis}` : ""}</p>
          </div>
          {canReview ? (
            <div className="flex flex-wrap gap-2" aria-label="Fact actions">
              {active && !editing ? <Button variant="outline" size="sm" onClick={() => setEditing(true)}><Pencil aria-hidden="true" />Edit</Button> : null}
              {current.status === "WORKING" ? (
                <Button size="sm" disabled={action.isPending || current.evidence_count === 0} title={current.evidence_count === 0 ? "A fact needs a supporting quote before it can be confirmed" : undefined} onClick={() => action.mutate({ path: "/confirm", body: lock, success: "Fact confirmed" })}>
                  <CheckCircle2 aria-hidden="true" />Confirm
                </Button>
              ) : null}
              {active ? <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => action.mutate({ path: "/reject", body: lock, success: "Fact rejected" })}><XCircle aria-hidden="true" />Reject</Button> : null}
              {active ? <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => setMergeOpen(true)}><GitMerge aria-hidden="true" />Merge into…</Button> : null}
              {current.status === "MERGED" ? <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => action.mutate({ path: "/unmerge", body: lock, success: "Merge undone" })}><Split aria-hidden="true" />Unmerge</Button> : null}
            </div>
          ) : null}
        </CardHeader>
        <CardContent>
          {current.status === "MERGED" && current.merged_into_fact_id ? (
            <p className="mb-4 rounded-md border bg-muted/40 px-3 py-2 text-sm">
              This fact was merged into <Link className="font-medium text-primary hover:underline" href={factDetailHref(clientId, matterId, current.merged_into_fact_id)}>another fact</Link>. Unmerge it to make changes.
            </p>
          ) : null}
          {editing ? (
            <FactEditForm
              fact={current}
              catalog={catalog.data ?? []}
              entities={entities.data?.items ?? []}
              entitiesAvailable={permissionKeys.has("entity.view")}
              saving={action.isPending}
              onCancel={() => setEditing(false)}
              onSave={(payload) => action.mutate({ path: "", method: "PATCH", body: payload, success: "Fact updated" })}
            />
          ) : (
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_minmax(0,1fr)]">
              <dt className="font-medium text-muted-foreground">Owner</dt>
              <dd>{current.owner?.canonical_display_name ?? "—"}</dd>
              <dt className="font-medium text-muted-foreground">Participants</dt>
              <dd>{current.participants.length ? current.participants.map((item) => `${item.canonical_display_name} (${ROLE_LABELS[item.role ?? ""] ?? "participant"})`).join(", ") : "—"}</dd>
              <dt className="font-medium text-muted-foreground">Issues</dt>
              <dd className="flex flex-wrap gap-1">{current.issues.map((issue) => <Badge key={`${issue.matter_analysis_task_id}:${issue.issue_key}`} variant="outline">{issueNames.get(issue.issue_key) ?? issue.issue_key}</Badge>)}</dd>
              <dt className="font-medium text-muted-foreground">Evidence</dt>
              <dd>{current.evidence_count} {current.evidence_count === 1 ? "quote" : "quotes"} from {current.document_count} {current.document_count === 1 ? "document" : "documents"}</dd>
              <dt className="font-medium text-muted-foreground">Updated</dt>
              <dd>{formatDate(current.updated_at)} · revision {current.current_revision}</dd>
            </dl>
          )}
        </CardContent>
      </Card>

      {pending.length ? (
        <Card>
          <CardHeader><CardTitle className="text-base">Agent suggestions</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {pending.map((suggestion) => (
              <SuggestionItem
                key={suggestion.id}
                clientId={clientId}
                matterId={matterId}
                fact={current}
                suggestion={suggestion}
                issueNames={issueNames}
                entityNames={entityNames}
                canReview={canReview}
                busy={action.isPending}
                onAccept={(targetLockVersion) =>
                  action.mutate({
                    path: `/suggestions/${suggestion.id}/accept`,
                    body: { lock_version: current.lock_version, ...(targetLockVersion ? { target_lock_version: targetLockVersion } : {}) },
                    success: suggestion.kind === "MERGE" ? "Facts merged" : "Suggestion applied",
                  })
                }
                onDismiss={() => action.mutate({ path: `/suggestions/${suggestion.id}/dismiss`, body: {}, success: "Suggestion dismissed" })}
              />
            ))}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader><CardTitle className="text-base">Supporting quotes</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {activeEvidence.length ? activeEvidence.map((item) => (
            <EvidenceItem
              key={item.id}
              evidence={item}
              canRemove={canReview && active && !(current.status === "CONFIRMED" && activeEvidence.length <= 1)}
              busy={action.isPending}
              onView={() => setViewing(item)}
              onRemove={() => action.mutate({ path: `/evidence/${item.id}?lock_version=${current.lock_version}`, method: "DELETE", success: "Quote removed" })}
            />
          )) : <p className="text-sm text-muted-foreground">This fact has no active supporting quotes.</p>}
          {removedEvidence.length ? (
            <details className="text-sm">
              <summary className="cursor-pointer font-medium text-muted-foreground">{removedEvidence.length} removed {removedEvidence.length === 1 ? "quote" : "quotes"}</summary>
              <div className="mt-2 space-y-2">{removedEvidence.map((item) => <EvidenceItem key={item.id} evidence={item} canRemove={false} busy={false} onView={() => setViewing(item)} />)}</div>
            </details>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">History</CardTitle></CardHeader>
        <CardContent>
          <ol className="space-y-2 text-sm">
            {current.events.map((event) => (
              <li key={event.id} className="flex flex-wrap items-baseline gap-x-2">
                <span className="font-medium">{event.event_type.toLowerCase().replaceAll("_", " ")}</span>
                <span className={event.actor_type === "AGENT" ? "text-agent" : "text-muted-foreground"}>{event.actor_type === "AGENT" ? "by an agent" : event.actor_type === "HUMAN" ? "by a reviewer" : "by the system"}</span>
                <span className="text-muted-foreground">{formatDate(event.created_at)}</span>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      <FactEvidenceViewer matterId={matterId} evidence={viewing} onClose={() => setViewing(null)} />
      <MergeDialog
        open={mergeOpen}
        matterId={matterId}
        fact={current}
        busy={action.isPending}
        onClose={() => setMergeOpen(false)}
        onMerge={(target) => {
          setMergeOpen(false);
          action.mutate({ path: "/merge", body: { lock_version: current.lock_version, target_fact_id: target.id, target_lock_version: target.lock_version }, success: "Facts merged" });
        }}
      />
    </div>
  );
}

function EvidenceItem({ evidence, canRemove, busy, onView, onRemove }: { evidence: MatterFactEvidenceRead; canRemove: boolean; busy: boolean; onView: () => void; onRemove?: () => void }) {
  return (
    <figure className="rounded-lg border p-3">
      <blockquote className="text-sm leading-relaxed">“{evidence.quote}”</blockquote>
      <figcaption className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">{evidence.control_number ?? "Document"}</span>
        <span>{evidence.paragraph_id}</span>
        {evidence.stance === "CONTRADICTS" ? <Badge variant="outline" className="text-conflict">Contradicts</Badge> : null}
        {evidence.workflow_run_id ? <span className="text-agent">Agent</span> : null}
        <span className="ml-auto flex gap-1">
          <Button type="button" variant="ghost" size="sm" onClick={onView}><Eye aria-hidden="true" />View in document</Button>
          {canRemove && onRemove ? <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onRemove} aria-label={`Remove quote from ${evidence.control_number ?? "document"}`}><Trash2 aria-hidden="true" />Remove</Button> : null}
        </span>
      </figcaption>
    </figure>
  );
}

function displayChange(field: string, value: unknown, issueNames: Map<string, string>, entityNames: Map<string, string>): ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (field === "owner_entity_id") return entityNames.get(String(value)) ?? "Unknown entity";
  if (field === "date_precision") return PRECISION_LABELS[String(value)] ?? String(value);
  if (field === "date_basis") return BASIS_LABELS[String(value)] ?? String(value);
  if (field === "issues" && Array.isArray(value)) return value.map((item) => issueNames.get((item as { issue_key: string }).issue_key) ?? (item as { issue_key: string }).issue_key).join(", ") || "—";
  if (field === "participants" && Array.isArray(value)) {
    return value.map((item) => {
      const participant = item as { entity_id: string; role: string };
      return `${entityNames.get(participant.entity_id) ?? "Unknown entity"} (${ROLE_LABELS[participant.role] ?? participant.role})`;
    }).join(", ") || "—";
  }
  return String(value);
}

function currentValue(fact: MatterFactRead, field: string): unknown {
  switch (field) {
    case "owner_entity_id":
      return fact.owner?.entity_id ?? null;
    case "issues":
      return fact.issues;
    case "participants":
      return fact.participants.map((item) => ({ entity_id: item.entity_id, role: item.role }));
    default:
      return (fact as unknown as Record<string, unknown>)[field];
  }
}

function SuggestionItem({ clientId, matterId, fact, suggestion, issueNames, entityNames, canReview, busy, onAccept, onDismiss }: {
  clientId: string;
  matterId: string;
  fact: MatterFactDetailRead;
  suggestion: MatterFactSuggestionRead;
  issueNames: Map<string, string>;
  entityNames: Map<string, string>;
  canReview: boolean;
  busy: boolean;
  onAccept: (targetLockVersion?: number) => void;
  onDismiss: () => void;
}) {
  const otherId = suggestion.kind === "MERGE" ? (suggestion.fact_id === fact.id ? suggestion.target_fact_id : suggestion.fact_id) : null;
  const [other] = useQueries({
    queries: [{
      queryKey: ["matter-fact", matterId, otherId],
      queryFn: () => coreApi<MatterFactDetailRead>(`/v1/matters/${matterId}/facts/${otherId}`),
      enabled: Boolean(otherId),
    }],
  });
  const thisIsSource = suggestion.fact_id === fact.id;
  return (
    <div className="rounded-lg border p-3">
      {suggestion.kind === "REVISION" ? (
        <>
          <p className="text-sm font-medium">An agent suggests revising this confirmed fact.</p>
          <table className="mt-2 w-full text-sm">
            <thead><tr className="text-left text-xs text-muted-foreground"><th className="w-32 py-1 font-medium">Field</th><th className="py-1 font-medium">Current</th><th className="py-1 font-medium">Suggested</th></tr></thead>
            <tbody>
              {Object.entries(suggestion.changes).map(([field, value]) => (
                <tr key={field} className="border-t align-top">
                  <td className="py-1.5 pr-2 font-medium">{FIELD_LABELS[field] ?? field}</td>
                  <td className="py-1.5 pr-2 text-muted-foreground">{displayChange(field, currentValue(fact, field), issueNames, entityNames)}</td>
                  <td className="py-1.5">{displayChange(field, value, issueNames, entityNames)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {suggestion.evidence.length ? <p className="mt-2 text-xs text-muted-foreground">Accepting also adds {suggestion.evidence.length} supporting {suggestion.evidence.length === 1 ? "quote" : "quotes"}.</p> : null}
        </>
      ) : (
        <p className="text-sm">
          {thisIsSource ? "An agent suggests merging this fact into " : "An agent suggests merging "}
          {otherId ? <Link className="font-medium text-primary hover:underline" href={factDetailHref(clientId, matterId, otherId)}>{other.data?.summary ?? "another fact"}</Link> : null}
          {thisIsSource ? "." : " into this fact."}
        </p>
      )}
      {suggestion.rationale ? <p className="mt-2 text-xs text-muted-foreground">Reason: {suggestion.rationale}</p> : null}
      {canReview ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {suggestion.kind === "REVISION" || thisIsSource ? (
            <Button size="sm" disabled={busy || (suggestion.kind === "MERGE" && !other.data)} onClick={() => onAccept(suggestion.kind === "MERGE" ? other.data?.lock_version : undefined)}>
              <CheckCircle2 aria-hidden="true" />{suggestion.kind === "MERGE" ? "Merge" : "Apply"}
            </Button>
          ) : (
            <Button asChild size="sm" variant="outline"><Link href={factDetailHref(clientId, matterId, suggestion.fact_id)}>Review on the other fact</Link></Button>
          )}
          {suggestion.kind === "REVISION" || thisIsSource ? <Button size="sm" variant="outline" disabled={busy} onClick={onDismiss}>Dismiss</Button> : null}
        </div>
      ) : null}
    </div>
  );
}

function FactEditForm({ fact, catalog, entities, entitiesAvailable, saving, onCancel, onSave }: {
  fact: MatterFactRead;
  catalog: FactIssueCatalogEntry[];
  entities: MatterEntityRead[];
  entitiesAvailable: boolean;
  saving: boolean;
  onCancel: () => void;
  onSave: (payload: MatterFactUpdate) => void;
}) {
  const [edit, setEdit] = useState<EditState>(() => editStateFor(fact));
  const undated = edit.datePrecision === "UNKNOWN";
  const knownEntities = useMemo(() => {
    const byId = new Map(entities.map((entity) => [entity.id, entity.display_name]));
    for (const ref of [fact.owner, ...fact.participants]) if (ref && !byId.has(ref.entity_id)) byId.set(ref.entity_id, ref.canonical_display_name);
    return [...byId.entries()].sort((left, right) => left[1].localeCompare(right[1]));
  }, [entities, fact]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const payload = updatePayload(fact, edit);
    if (Object.keys(payload).length === 1) {
      onCancel();
      return;
    }
    onSave(payload);
  }

  return (
    <form onSubmit={submit} className="space-y-4" aria-label="Edit fact">
      <div className="space-y-2">
        <Label htmlFor="fact-summary">Summary</Label>
        <Textarea id="fact-summary" required value={edit.summary} onChange={(event) => setEdit({ ...edit, summary: event.target.value })} rows={3} />
      </div>
      <div className="grid gap-3 sm:grid-cols-4">
        <div className="space-y-2">
          <Label htmlFor="fact-precision">Date precision</Label>
          <Select value={edit.datePrecision} onValueChange={(value) => setEdit({ ...edit, datePrecision: value })}>
            <SelectTrigger id="fact-precision"><SelectValue /></SelectTrigger>
            <SelectContent>{PRECISIONS.map((value) => <SelectItem key={value} value={value}>{PRECISION_LABELS[value]}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="fact-start">{edit.datePrecision === "RANGE" ? "Start date" : "Date"}</Label>
          <Input id="fact-start" type="date" disabled={undated} required={!undated} value={undated ? "" : edit.start} onChange={(event) => setEdit({ ...edit, start: event.target.value })} />
        </div>
        {edit.datePrecision === "RANGE" ? (
          <div className="space-y-2">
            <Label htmlFor="fact-end">End date</Label>
            <Input id="fact-end" type="date" required min={edit.start || undefined} value={edit.end} onChange={(event) => setEdit({ ...edit, end: event.target.value })} />
          </div>
        ) : null}
        <div className="space-y-2">
          <Label htmlFor="fact-basis">Date basis</Label>
          <Select value={edit.dateBasis || "NONE"} disabled={undated} onValueChange={(value) => setEdit({ ...edit, dateBasis: value === "NONE" ? "" : value })}>
            <SelectTrigger id="fact-basis"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="NONE">Not stated</SelectItem>
              {BASES.map((value) => <SelectItem key={value} value={value}>{BASIS_LABELS[value]}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="fact-owner">Owner</Label>
          <Select value={edit.ownerId || "NONE"} disabled={!entitiesAvailable} onValueChange={(value) => setEdit({ ...edit, ownerId: value === "NONE" ? "" : value })}>
            <SelectTrigger id="fact-owner"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="NONE">No owner</SelectItem>
              {knownEntities.map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}
            </SelectContent>
          </Select>
          {!entitiesAvailable ? <p className="text-xs text-muted-foreground">You need entity access to change people and organizations.</p> : null}
        </div>
        <label className="flex items-center gap-2 self-end pb-2 text-sm font-medium">
          <input type="checkbox" className="size-4 accent-primary" checked={edit.keyFact} onChange={(event) => setEdit({ ...edit, keyFact: event.target.checked })} />
          Key fact
        </label>
      </div>
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Issues</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {catalog.map((entry) => {
            const choice = `${entry.matter_analysis_task_id}:${entry.issue_key}` as IssueChoice;
            return (
              <label key={choice} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="size-4 accent-primary"
                  checked={edit.issues.includes(choice)}
                  onChange={(event) => setEdit({ ...edit, issues: event.target.checked ? [...edit.issues, choice] : edit.issues.filter((item) => item !== choice) })}
                />
                {entry.name}
              </label>
            );
          })}
        </div>
        {!edit.issues.length ? <p className="text-xs text-destructive">A fact must support at least one issue.</p> : null}
      </fieldset>
      <fieldset className="space-y-2" disabled={!entitiesAvailable}>
        <legend className="text-sm font-medium">Participants</legend>
        {edit.participants.map((participant, index) => (
          <div key={index} className="flex flex-wrap items-center gap-2">
            <Select value={participant.entity_id || undefined} onValueChange={(value) => setEdit({ ...edit, participants: edit.participants.map((item, position) => position === index ? { ...item, entity_id: value } : item) })}>
              <SelectTrigger className="w-64" aria-label={`Participant ${index + 1}`}><SelectValue placeholder="Choose a person or organization" /></SelectTrigger>
              <SelectContent>{knownEntities.map(([id, name]) => <SelectItem key={id} value={id}>{name}</SelectItem>)}</SelectContent>
            </Select>
            <Select value={participant.role} onValueChange={(value) => setEdit({ ...edit, participants: edit.participants.map((item, position) => position === index ? { ...item, role: value } : item) })}>
              <SelectTrigger className="w-36" aria-label={`Participant ${index + 1} role`}><SelectValue /></SelectTrigger>
              <SelectContent>{Object.entries(ROLE_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent>
            </Select>
            <Button type="button" variant="ghost" size="sm" onClick={() => setEdit({ ...edit, participants: edit.participants.filter((_, position) => position !== index) })}>Remove</Button>
          </div>
        ))}
        <Button type="button" variant="outline" size="sm" onClick={() => setEdit({ ...edit, participants: [...edit.participants, { entity_id: "", role: "PARTICIPANT_ACTOR" }] })}>Add participant</Button>
      </fieldset>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving || !edit.issues.length || !edit.summary.trim()}>Save changes</Button>
      </div>
    </form>
  );
}

function MergeDialog({ open, matterId, fact, busy, onClose, onMerge }: {
  open: boolean;
  matterId: string;
  fact: MatterFactRead;
  busy: boolean;
  onClose: () => void;
  onMerge: (target: MatterFactRead) => void;
}) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const results = useQuery({
    queryKey: ["matter-fact-merge-targets", matterId, submitted],
    queryFn: () => coreApi<MatterFactPage>(`/v1/matters/${matterId}/facts/search`, {
      method: "POST",
      body: JSON.stringify({ filters: { query: submitted || null, statuses: ["WORKING", "CONFIRMED"] }, limit: 20 }),
    }),
    enabled: open,
  });
  const candidates = (results.data?.items ?? []).filter((item) => item.id !== fact.id);
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Merge into another fact</DialogTitle>
          <DialogDescription>This fact&apos;s quotes, issues and participants move to the fact you choose. You can undo the merge later.</DialogDescription>
        </DialogHeader>
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()); }}>
          <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search summaries and quotes" aria-label="Search for the fact to keep" />
          <Button type="submit" variant="outline">Search</Button>
        </form>
        <div className="max-h-80 space-y-2 overflow-y-auto">
          {results.isPending ? <TableLoading /> : results.error ? <QueryError message={results.error.message} /> : candidates.length ? candidates.map((candidate) => (
            <div key={candidate.id} className="flex items-start justify-between gap-3 rounded-lg border p-3">
              <div className="min-w-0 text-sm">
                <p className="font-medium">{candidate.summary}</p>
                <p className="mt-1 text-xs text-muted-foreground">{formatFactDate(candidate)} · <FactStatusBadge status={candidate.status} /></p>
              </div>
              <Button size="sm" disabled={busy} onClick={() => onMerge(candidate)}>Keep this fact</Button>
            </div>
          )) : <p className="text-sm text-muted-foreground">No other working or confirmed facts match.</p>}
        </div>
        <DialogFooter><Button variant="ghost" onClick={onClose}>Cancel</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
