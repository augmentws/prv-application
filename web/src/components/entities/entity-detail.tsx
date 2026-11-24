"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Bot, CheckCircle2, ChevronRight, GitMerge, Link2, Pencil, Split, XCircle } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";

import { EntityStatusBadge, EntityTypeLabel, OrganizationCategoryBadge, organizationCategoryLabel, RejectEntityDialog, useOrganizationNames } from "@/components/entities/entities-panel";
import { FactStatusBadge } from "@/components/facts/fact-status-badge";
import { QueryError, TableLoading } from "@/components/query-state";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  ClientRead,
  CustodianRead,
  EffectivePermissionsRead,
  MatterEntityPage,
  MatterEntityRead,
  MatterEntityRelationshipCreate,
  MatterEntityRelationshipDefinitionRead,
  MatterEntityRelationshipRead,
  MatterEntityUpdate,
  MatterEntityVariantRead,
  MatterFactPage,
  MatterRead,
} from "@/generated/models";
import { ApiError, coreApi } from "@/lib/api-client";
import { entityDetailHref, entityFactsHref, entityLabel, factDetailHref, formatFactDate } from "@/lib/facts";
import { matterHref } from "@/lib/matter-routes";

const ENTITY_FACT_PREVIEW = 20;

function errorMessage(error: unknown, fallback: string) {
  if (error instanceof ApiError && error.status === 409) return `${error.message}. The entity was reloaded; review it and try again.`;
  return error instanceof Error ? error.message : fallback;
}

export function EntityDetail({ clientId, matterId, entityId }: { clientId: string; matterId: string; entityId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [relationshipType, setRelationshipType] = useState("");
  const [relationshipTargetId, setRelationshipTargetId] = useState("");
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const entityKey = ["matter-entity", matterId, entityId] as const;
  const entity = useQuery({ queryKey: entityKey, queryFn: () => coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities/${entityId}`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const canManage = permissionKeys.has("entity.manage");
  const canViewFacts = permissionKeys.has("fact.view");
  const { organizations, names: organizationNames } = useOrganizationNames(matterId);
  const custodians = useQuery({
    queryKey: ["matter-custodians", matterId],
    queryFn: () => coreApi<CustodianRead[]>(`/v1/matters/${matterId}/custodians`),
    retry: false,
  });
  const canonicalId = entity.data?.id;
  const facts = useQuery({
    queryKey: ["matter-facts", matterId, { entity_id: canonicalId }, "entity-preview"],
    queryFn: () => coreApi<MatterFactPage>(`/v1/matters/${matterId}/facts/search`, {
      method: "POST",
      body: JSON.stringify({ filters: { entity_id: canonicalId }, sort: "DATE_ASC", limit: ENTITY_FACT_PREVIEW }),
    }),
    enabled: Boolean(canonicalId) && canViewFacts,
  });
  const relationships = useQuery({
    queryKey: ["matter-entity-relationships", matterId, entityId],
    queryFn: () => coreApi<MatterEntityRelationshipRead[]>(
      `/v1/matters/${matterId}/entity-relationships?entity_id=${entityId}`,
    ),
  });
  const relationshipDefinitions = useQuery({
    queryKey: ["matter-entity-relationship-types", matterId],
    queryFn: () => coreApi<MatterEntityRelationshipDefinitionRead[]>(
      `/v1/matters/${matterId}/entity-relationships/types`,
    ),
    enabled: entity.data?.status === "CONFIRMED" && canManage,
  });
  const relationshipTargets = useQuery({
    queryKey: ["matter-entity-relationship-targets", matterId],
    queryFn: () => coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?status=CONFIRMED&limit=500`),
    enabled: entity.data?.status === "CONFIRMED" && canManage,
  });

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ["matter-entity", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-entities", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-entity-organizations", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-facts", matterId] });
    void queryClient.invalidateQueries({ queryKey: ["matter-entity-relationships", matterId] });
  }

  const action = useMutation({
    mutationFn: ({ path, method = "POST", body }: { path: string; method?: string; body: unknown; success: string; navigate?: boolean }) =>
      coreApi<MatterEntityRead>(`/v1/matters/${matterId}/entities/${path}`, { method, body: JSON.stringify(body) }),
    onSuccess: (updated, variables) => {
      toast.success(variables.success);
      setEditing(false);
      setRejecting(false);
      setMergeOpen(false);
      refresh();
      if (variables.navigate && updated.id !== canonicalId) router.push(entityDetailHref(clientId, matterId, updated.id));
    },
    onError: (error) => {
      toast.error(errorMessage(error, "The entity could not be changed"));
      refresh();
    },
  });
  const createRelationship = useMutation({
    mutationFn: (payload: MatterEntityRelationshipCreate) => coreApi<MatterEntityRelationshipRead>(
      `/v1/matters/${matterId}/entity-relationships`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
    onSuccess: () => {
      toast.success("Relationship added");
      setRelationshipTargetId("");
      refresh();
    },
    onError: (error) => toast.error(errorMessage(error, "The relationship could not be added")),
  });

  if (entity.isPending || client.isPending || matter.isPending) return <TableLoading />;
  if (entity.error || client.error || matter.error) return <QueryError message={entity.error?.message ?? client.error?.message ?? matter.error?.message} />;
  const current = entity.data;
  const variants = current.variants ?? [];
  const lock = { lock_version: current.lock_version };
  const entitiesHref = matterHref(clientId, matterId, current.status === "PROPOSED" ? "entities-proposed" : "entities");
  const organization = current.organization_id ? organizationNames.get(current.organization_id) : undefined;
  const custodian = current.custodian_id ? custodians.data?.find((item) => item.id === current.custodian_id) : undefined;
  const eligibleRelationshipDefinitions = (relationshipDefinitions.data ?? []).filter((definition) => (
    definition.source_entity_types.some((entityType) => entityType === current.entity_type)
      && (definition.source_organization_categories === null
        || definition.source_organization_categories.some((category) => category === current.organization_category))
  ));
  const selectedRelationshipType = eligibleRelationshipDefinitions.some((definition) => definition.key === relationshipType)
    ? relationshipType
    : eligibleRelationshipDefinitions[0]?.key ?? "";
  const selectedRelationshipDefinition = eligibleRelationshipDefinitions.find(
    (definition) => definition.key === selectedRelationshipType,
  );
  const eligibleRelationshipTargets = (relationshipTargets.data?.items ?? []).filter((candidate) => (
    candidate.id !== current.id
      && selectedRelationshipDefinition?.target_entity_types.some((entityType) => entityType === candidate.entity_type)
  ));

  return (
    <div className="space-y-5">
      <ResourcePageHeader
        title="Entity"
        breadcrumbs={
          <>
            <Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.data.name}</Link>
            <ChevronRight className="size-4" aria-hidden="true" />
            <Link href={`/app/clients/${clientId}/matters/${matterId}`} className="hover:text-foreground">{matter.data.name}</Link>
            <ChevronRight className="size-4" aria-hidden="true" />
            <Link href={entitiesHref} className="hover:text-foreground">Entities</Link>
          </>
        }
      />
      <Link href={entitiesHref} className="inline-flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" aria-hidden="true" />Back to entities
      </Link>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <EntityStatusBadge status={current.status} />
              <EntityTypeLabel type={current.entity_type} />
              {current.organization_category ? <OrganizationCategoryBadge category={current.organization_category} /> : null}
              {current.created_by_workflow_run_id ? <span className="inline-flex items-center gap-1 text-xs font-medium text-agent"><Bot className="size-3.5" aria-hidden="true" />Proposed by an agent</span> : null}
            </div>
            <CardTitle className="text-lg">{entityLabel(current)}</CardTitle>
          </div>
          {canManage ? (
            <div className="flex flex-wrap gap-2" aria-label="Entity actions">
              {!editing ? <Button variant="outline" size="sm" onClick={() => setEditing(true)}><Pencil aria-hidden="true" />Edit</Button> : null}
              {current.status !== "CONFIRMED" ? <Button size="sm" disabled={action.isPending} onClick={() => action.mutate({ path: `${current.id}/confirm`, body: lock, success: "Entity confirmed" })}><CheckCircle2 aria-hidden="true" />Confirm</Button> : null}
              {current.status !== "REJECTED" ? <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => setRejecting(true)}><XCircle aria-hidden="true" />Reject</Button> : null}
              <Button variant="outline" size="sm" disabled={action.isPending} onClick={() => setMergeOpen(true)}><GitMerge aria-hidden="true" />Merge into…</Button>
            </div>
          ) : null}
        </CardHeader>
        <CardContent>
          {current.organization_category === "LAW_FIRM" ? (
            <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
              <p className="font-medium">Potential privilege-review signal</p>
              <p className="mt-1 text-muted-foreground">This organization was identified as a law firm. That does not establish whom it represents or make any communication or document privileged.</p>
            </div>
          ) : null}
          {editing ? (
            <EntityEditForm
              entity={current}
              organizations={(organizations.data?.items ?? []).filter((item) => item.id !== current.id && item.status !== "REJECTED")}
              custodians={custodians.data ?? []}
              saving={action.isPending}
              onCancel={() => setEditing(false)}
              onSave={(payload) => action.mutate({ path: current.id, method: "PATCH", body: payload, success: "Entity updated" })}
            />
          ) : (
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[10rem_minmax(0,1fr)]">
              <dt className="font-medium text-muted-foreground">Email</dt>
              <dd>{current.email ?? "—"}</dd>
              {current.entity_type === "PERSON" ? (
                <>
                  <dt className="font-medium text-muted-foreground">Title</dt>
                  <dd>{current.title ?? "—"}</dd>
                  <dt className="font-medium text-muted-foreground">Organization</dt>
                  <dd>{current.organization_id ? <Link className="text-primary hover:underline" href={entityDetailHref(clientId, matterId, current.organization_id)}>{organization ?? "Organization"}</Link> : "—"}</dd>
                </>
              ) : (
                <>
                  <dt className="font-medium text-muted-foreground">Organization category</dt>
                  <dd>{current.organization_category ? organizationCategoryLabel(current.organization_category) : "—"}</dd>
                  <dt className="font-medium text-muted-foreground">Domains</dt>
                  <dd>{current.domains?.length ? current.domains.map((domain) => domain.domain).join(", ") : "—"}</dd>
                </>
              )}
              <dt className="font-medium text-muted-foreground">Custodian</dt>
              <dd>{current.custodian_id ? custodian?.display_name ?? "Linked custodian" : "—"}</dd>
              <dt className="font-medium text-muted-foreground">Description</dt>
              <dd className="whitespace-pre-wrap">{current.description ?? "—"}</dd>
            </dl>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base"><Link2 className="size-4" aria-hidden="true" />Related</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">Matter-specific relationships connect people and organizations. They may improve review signals, but never establish privilege by themselves.</p>
          {relationships.isPending ? <TableLoading />
            : relationships.error ? <QueryError message={relationships.error.message} />
            : relationships.data.length ? (
              <ul className="divide-y rounded-lg border">
                {relationships.data.map((relationship) => {
                  const outbound = relationship.source.id === current.id;
                  const other = outbound ? relationship.target : relationship.source;
                  return (
                    <li key={relationship.id} className="flex flex-wrap items-center justify-between gap-3 p-3 text-sm">
                      <div>
                        <p className="font-medium">{outbound ? relationship.forward_label : relationship.inverse_label}</p>
                        <Link className="text-primary hover:underline" href={entityDetailHref(clientId, matterId, other.id)}>{other.display_name ?? other.email ?? "Entity"}</Link>
                      </div>
                      <EntityStatusBadge status={relationship.status} />
                    </li>
                  );
                })}
              </ul>
            ) : <p className="text-sm">No matter-specific relationships recorded.</p>}
          {canManage && current.status === "CONFIRMED" && eligibleRelationshipDefinitions.length ? (
            <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="relationship-type">Relationship type</Label>
                  <Select
                    value={selectedRelationshipType}
                    onValueChange={(value) => {
                      setRelationshipType(value);
                      setRelationshipTargetId("");
                    }}
                  >
                    <SelectTrigger id="relationship-type"><SelectValue placeholder="Select a relationship type" /></SelectTrigger>
                    <SelectContent>
                      {eligibleRelationshipDefinitions.map((definition) => (
                        <SelectItem key={definition.key} value={definition.key}>{definition.forward_label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="related-entity">Related entity</Label>
                  <Select value={relationshipTargetId} onValueChange={setRelationshipTargetId}>
                    <SelectTrigger id="related-entity"><SelectValue placeholder="Select a confirmed entity" /></SelectTrigger>
                    <SelectContent>
                      {eligibleRelationshipTargets.map((candidate) => (
                        <SelectItem key={candidate.id} value={candidate.id}>{entityLabel(candidate)}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted-foreground">
                  {selectedRelationshipDefinition
                    ? `Direction from this entity: ${selectedRelationshipDefinition.forward_label}.`
                    : "Select a relationship type and entity."}
                </p>
                <Button
                  disabled={!selectedRelationshipType || !relationshipTargetId || createRelationship.isPending}
                  onClick={() => createRelationship.mutate({
                    source_entity_id: current.id,
                    target_entity_id: relationshipTargetId,
                    relationship_type: selectedRelationshipType,
                    status: "CONFIRMED",
                  })}
                >Add relationship</Button>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-base">Names and email addresses</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">Other names and email addresses this {current.entity_type === "PERSON" ? "person" : "organization"} appears under. Facts that name a variant count toward this entity.</p>
          {variants.length ? (
            <ul className="divide-y rounded-lg border">
              {variants.map((variant) => (
                <VariantRow
                  key={variant.id}
                  variant={variant}
                  canManage={canManage}
                  busy={action.isPending}
                  onSplit={() => action.mutate({ path: `${variant.id}/unmerge`, body: { lock_version: variant.lock_version }, success: `${variant.email ?? entityLabel(variant)} split off as its own entity` })}
                />
              ))}
            </ul>
          ) : <p className="text-sm">No other names or email addresses.</p>}
          {canManage && current.status !== "REJECTED" ? (
            <AddVariantForm busy={action.isPending} onAdd={(payload) => action.mutate({ path: `${current.id}/variants`, body: { ...lock, ...payload }, success: "Added" })} />
          ) : null}
        </CardContent>
      </Card>

      {canViewFacts ? (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between gap-3">
            <CardTitle className="text-base">Facts</CardTitle>
            {facts.data?.total ? <Link className="text-sm font-medium text-primary hover:underline" href={entityFactsHref(clientId, matterId, current.id)}>View all {facts.data.total} in Facts</Link> : null}
          </CardHeader>
          <CardContent>
            {facts.isPending ? <TableLoading /> : facts.error ? <QueryError message={facts.error.message} /> : facts.data.items.length ? (
              <ol className="space-y-2">
                {facts.data.items.map((fact) => (
                  <li key={fact.id} className="grid gap-x-3 text-sm sm:grid-cols-[8rem_minmax(0,1fr)_auto]">
                    <span className="tabular-nums text-muted-foreground">{formatFactDate(fact)}</span>
                    <Link className="hover:text-primary" href={factDetailHref(clientId, matterId, fact.id)}>{fact.summary}</Link>
                    <FactStatusBadge status={fact.status} />
                  </li>
                ))}
              </ol>
            ) : <p className="text-sm text-muted-foreground">No facts name this {current.entity_type === "PERSON" ? "person" : "organization"} as owner or participant.</p>}
          </CardContent>
        </Card>
      ) : null}

      <RejectEntityDialog
        entity={rejecting ? { ...current, fact_count: facts.data?.total ?? current.fact_count } : null}
        busy={action.isPending}
        onCancel={() => setRejecting(false)}
        onReject={() => action.mutate({ path: `${current.id}/reject`, body: lock, success: "Entity rejected" })}
      />
      <MergeEntityDialog
        open={mergeOpen}
        matterId={matterId}
        entity={current}
        busy={action.isPending}
        onClose={() => setMergeOpen(false)}
        onMerge={(target) => action.mutate({ path: `${current.id}/merge`, body: { ...lock, target_entity_id: target.id }, success: `Merged into ${entityLabel(target)}`, navigate: true })}
      />
    </div>
  );
}

function VariantRow({ variant, canManage, busy, onSplit }: { variant: MatterEntityVariantRead; canManage: boolean; busy: boolean; onSplit: () => void }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm">
      <span>
        <span className="font-medium">{entityLabel(variant)}</span>
        {variant.email && variant.display_name ? <span className="ml-2 text-muted-foreground">{variant.email}</span> : null}
      </span>
      {canManage ? (
        <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={onSplit} aria-label={`Split off ${variant.email ?? entityLabel(variant)}`}>
          <Split aria-hidden="true" />Split off
        </Button>
      ) : null}
    </li>
  );
}

function AddVariantForm({ busy, onAdd }: { busy: boolean; onAdd: (payload: { display_name?: string; email?: string }) => void }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      aria-label="Add a name or email address"
      onSubmit={(event: FormEvent) => {
        event.preventDefault();
        onAdd({ ...(name.trim() ? { display_name: name.trim() } : {}), ...(email.trim() ? { email: email.trim() } : {}) });
        setName("");
        setEmail("");
      }}
    >
      <div className="space-y-1">
        <Label htmlFor="variant-name">Name</Label>
        <Input id="variant-name" value={name} onChange={(event) => setName(event.target.value)} className="w-56" />
      </div>
      <div className="space-y-1">
        <Label htmlFor="variant-email">Email address</Label>
        <Input id="variant-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} className="w-64" />
      </div>
      <Button type="submit" variant="outline" disabled={busy || (!name.trim() && !email.trim())}>Add</Button>
    </form>
  );
}

function EntityEditForm({ entity, organizations, custodians, saving, onCancel, onSave }: {
  entity: MatterEntityRead;
  organizations: MatterEntityRead[];
  custodians: CustodianRead[];
  saving: boolean;
  onCancel: () => void;
  onSave: (payload: MatterEntityUpdate) => void;
}) {
  const person = entity.entity_type === "PERSON";
  const [name, setName] = useState(entity.display_name ?? "");
  const [title, setTitle] = useState(entity.title ?? "");
  const [organizationId, setOrganizationId] = useState(entity.organization_id ?? "");
  const [custodianId, setCustodianId] = useState(entity.custodian_id ?? "");
  const [description, setDescription] = useState(entity.description ?? "");

  function submit(event: FormEvent) {
    event.preventDefault();
    const payload: MatterEntityUpdate = { lock_version: entity.lock_version };
    if ((name.trim() || null) !== entity.display_name) payload.display_name = name.trim() || null;
    if (person && (title.trim() || null) !== (entity.title ?? null)) payload.title = title.trim() || null;
    if (person && (organizationId || null) !== (entity.organization_id ?? null)) payload.organization_id = organizationId || null;
    if ((custodianId || null) !== (entity.custodian_id ?? null)) payload.custodian_id = custodianId || null;
    if ((description.trim() || null) !== (entity.description ?? null)) payload.description = description.trim() || null;
    if (Object.keys(payload).length === 1) {
      onCancel();
      return;
    }
    onSave(payload);
  }

  return (
    <form onSubmit={submit} className="space-y-4" aria-label="Edit entity">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-2">
          <Label htmlFor="entity-edit-name">Name</Label>
          <Input id="entity-edit-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        {person ? (
          <div className="space-y-2">
            <Label htmlFor="entity-edit-title">Title</Label>
            <Input id="entity-edit-title" value={title} onChange={(event) => setTitle(event.target.value)} />
          </div>
        ) : null}
        {person ? (
          <div className="space-y-2">
            <Label htmlFor="entity-edit-organization">Organization</Label>
            <Select value={organizationId || "NONE"} onValueChange={(value) => setOrganizationId(value === "NONE" ? "" : value)}>
              <SelectTrigger id="entity-edit-organization"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="NONE">None</SelectItem>
                {organizations.map((item) => <SelectItem key={item.id} value={item.id}>{entityLabel(item)}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        ) : null}
        <div className="space-y-2">
          <Label htmlFor="entity-edit-custodian">Custodian</Label>
          <Select value={custodianId || "NONE"} onValueChange={(value) => setCustodianId(value === "NONE" ? "" : value)}>
            <SelectTrigger id="entity-edit-custodian"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="NONE">Not a custodian</SelectItem>
              {custodians.map((item) => <SelectItem key={item.id} value={item.id}>{item.display_name}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>
      <div className="space-y-2">
        <Label htmlFor="entity-edit-description">Description</Label>
        <Textarea id="entity-edit-description" rows={3} value={description} onChange={(event) => setDescription(event.target.value)} />
      </div>
      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" disabled={saving || (!name.trim() && !entity.email)}>Save changes</Button>
      </div>
    </form>
  );
}

function MergeEntityDialog({ open, matterId, entity, busy, onClose, onMerge }: {
  open: boolean;
  matterId: string;
  entity: MatterEntityRead;
  busy: boolean;
  onClose: () => void;
  onMerge: (target: MatterEntityRead) => void;
}) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const results = useQuery({
    queryKey: ["matter-entity-merge-targets", matterId, entity.entity_type, submitted],
    queryFn: () => {
      const params = new URLSearchParams({ entity_type: entity.entity_type, limit: "20" });
      params.append("status", "CONFIRMED");
      params.append("status", "PROPOSED");
      if (submitted) params.set("q", submitted);
      return coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?${params}`);
    },
    enabled: open,
  });
  const candidates = (results.data?.items ?? []).filter((item) => item.id !== entity.id);
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Merge {entityLabel(entity)} into another {entity.entity_type === "PERSON" ? "person" : "organization"}</DialogTitle>
          <DialogDescription>Its names, email addresses and facts move to the one you keep. You can split names and addresses off again afterwards.</DialogDescription>
        </DialogHeader>
        <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()); }}>
          <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search names and email addresses" aria-label="Search for the entity to keep" />
          <Button type="submit" variant="outline">Search</Button>
        </form>
        <div className="max-h-80 space-y-2 overflow-y-auto">
          {results.isPending ? <TableLoading /> : results.error ? <QueryError message={results.error.message} /> : candidates.length ? candidates.map((candidate) => (
            <div key={candidate.id} className="flex items-center justify-between gap-3 rounded-lg border p-3 text-sm">
              <div className="min-w-0">
                <p className="font-medium">{entityLabel(candidate)}</p>
                <p className="text-xs text-muted-foreground">{[candidate.email, candidate.title].filter(Boolean).join(" · ") || "No email address"} · {candidate.fact_count} {candidate.fact_count === 1 ? "fact" : "facts"}</p>
              </div>
              <Button size="sm" disabled={busy} onClick={() => onMerge(candidate)}>Keep this one</Button>
            </div>
          )) : <p className="text-sm text-muted-foreground">No other matching {entity.entity_type === "PERSON" ? "people" : "organizations"}.</p>}
        </div>
        <DialogFooter><Button variant="ghost" onClick={onClose}>Cancel</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
