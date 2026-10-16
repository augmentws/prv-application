"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Bot, Building2, CheckCircle2, Plus, Search, UserRound, XCircle } from "lucide-react";
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
import type { MatterEntityCreate, MatterEntityPage, MatterEntityRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { entityDetailHref } from "@/lib/facts";

export type EntitiesTab = "directory" | "proposed";
const PAGE_SIZE = 50;
const ENTITY_STATUS_CLASSES: Record<string, string> = {
  PROPOSED: "border border-pending/40 bg-pending/10 text-pending",
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

/** Names of the matter's organizations, for showing a person's organization. */
export function useOrganizationNames(matterId: string, enabled = true) {
  const organizations = useQuery({
    queryKey: ["matter-entity-organizations", matterId],
    queryFn: () => coreApi<MatterEntityPage>(`/v1/matters/${matterId}/entities?entity_type=ORGANIZATION&limit=500`),
    enabled,
    staleTime: 60_000,
  });
  const names = useMemo(() => new Map((organizations.data?.items ?? []).map((item) => [item.id, item.display_name])), [organizations.data]);
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
      toast.success(`${entity.display_name} ${action === "confirm" ? "confirmed" : "rejected"}`);
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
      toast.success(`${entity.display_name} added`);
      setCreating(false);
      refresh();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The entity could not be added"),
  });

  const columns: ColumnDef<MatterEntityRead>[] = [
    {
      id: "name",
      header: "Name",
      size: 260,
      cell: ({ row }) => (
        <div className="min-w-0">
          <Link href={entityDetailHref(clientId, matterId, row.original.id)} className="font-medium hover:text-primary">{row.original.display_name}</Link>
          {row.original.created_by_workflow_run_id ? <span className="ml-2 inline-flex items-center gap-1 text-xs text-agent"><Bot className="size-3" aria-hidden="true" />Agent</span> : null}
          {row.original.variants?.length ? <p className="truncate text-xs text-muted-foreground">Also: {row.original.variants.map((variant) => variant.email ?? variant.display_name).join(", ")}</p> : null}
        </div>
      ),
    },
    { id: "type", header: "Type", size: 140, cell: ({ row }) => <EntityTypeLabel type={row.original.entity_type} /> },
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
              <Button size="sm" variant="outline" disabled={review.isPending} onClick={() => review.mutate({ entity: row.original, action: "confirm" })} aria-label={`Confirm ${row.original.display_name}`}>
                <CheckCircle2 aria-hidden="true" />Confirm
              </Button>
              <Button size="sm" variant="ghost" disabled={review.isPending} onClick={() => setRejecting(row.original)} aria-label={`Reject ${row.original.display_name}`}>
                <XCircle aria-hidden="true" />Reject
              </Button>
            </div>
          ),
        } satisfies ColumnDef<MatterEntityRead>]
      : []),
  ];

  return (
    <div className="space-y-3">
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
          <DialogTitle>Reject {entity?.display_name}?</DialogTitle>
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
              display_name: name.trim(),
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
            <Input id="entity-name" required value={name} onChange={(event) => setName(event.target.value)} />
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
            <Button type="submit" disabled={busy || !name.trim()}>Add</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
