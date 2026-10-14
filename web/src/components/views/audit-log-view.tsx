"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, FilterX, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useWorkspace } from "@/components/workspace-context";
import type { AuditRecordPage, AuditRecordRead, UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

interface AuditFilters {
  fromDate: string;
  toDate: string;
  actor: string;
  actionPrefix: string;
}

const EMPTY_FILTERS: AuditFilters = { fromDate: "", toDate: "", actor: "all", actionPrefix: "" };

function localDayBoundary(value: string, nextDay = false) {
  if (!value) return null;
  const parts = value.split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => !Number.isInteger(part))) return null;
  const [year, month, day] = parts;
  const date = new Date(year, month - 1, day + (nextDay ? 1 : 0));
  return Number.isFinite(date.getTime()) ? date.toISOString() : null;
}

function buildAuditPath(tenantId: string, filters: AuditFilters, cursor: string | null) {
  const params = new URLSearchParams({ limit: "50" });
  const createdFrom = localDayBoundary(filters.fromDate);
  const createdTo = localDayBoundary(filters.toDate, true);
  if (createdFrom) params.set("created_from", createdFrom);
  if (createdTo) params.set("created_to", createdTo);
  if (filters.actor === "system") params.set("system_actor", "true");
  else if (filters.actor !== "all") params.set("actor_user_id", filters.actor);
  if (filters.actionPrefix.trim()) params.set("action_prefix", filters.actionPrefix.trim());
  if (cursor) params.set("cursor", cursor);
  return `/v1/tenants/${tenantId}/audit-records?${params.toString()}`;
}

function formatTimestamp(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

function actionLabel(value: string) {
  return value
    .split(/[._]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function actorLabel(record: AuditRecordRead) {
  if (!record.actor_user_id) return "System";
  return record.actor_display_name ?? record.actor_email ?? record.actor_user_id;
}

export function AuditLogView() {
  const { tenant, selectedTenantId, user, permissionKeys = [] } = useWorkspace();
  const canViewAudit = user.tenant_role === "ADMIN" || user.is_superuser || permissionKeys.includes("tenant.view_audit");
  const [draftFilters, setDraftFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([null]);
  const [selectedRecord, setSelectedRecord] = useState<AuditRecordRead | null>(null);
  const cursor = cursorStack[cursorStack.length - 1];

  const users = useQuery({
    queryKey: ["tenant-users", selectedTenantId],
    queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${selectedTenantId}/users`),
    enabled: canViewAudit,
  });
  const records = useQuery({
    queryKey: ["audit-records", selectedTenantId, filters, cursor],
    queryFn: () => coreApi<AuditRecordPage>(buildAuditPath(selectedTenantId, filters, cursor)),
    enabled: canViewAudit,
  });
  const activeFilterCount = useMemo(
    () => [filters.fromDate, filters.toDate, filters.actor !== "all" ? filters.actor : "", filters.actionPrefix]
      .filter(Boolean).length,
    [filters],
  );

  if (!canViewAudit) {
    return <QueryError message="Audit-log access is required." />;
  }

  function applyFilters() {
    setFilters({ ...draftFilters, actionPrefix: draftFilters.actionPrefix.trim() });
    setCursorStack([null]);
  }

  function clearFilters() {
    setDraftFilters(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
    setCursorStack([null]);
  }

  const data = records.data;
  return (
    <>
      <PageHeader
        eyebrow={tenant.name}
        title="Audit Log"
        description="Review the recorded actions for this tenant. Results are read-only and ordered from newest to oldest."
      />

      <form
        className="mb-5 grid gap-4 rounded-xl border bg-card p-4 lg:grid-cols-[1fr_1fr_1.3fr_1.5fr_auto] lg:items-end"
        onSubmit={(event) => { event.preventDefault(); applyFilters(); }}
      >
        <div className="space-y-2">
          <Label htmlFor="audit-from-date">From date</Label>
          <Input id="audit-from-date" type="date" value={draftFilters.fromDate} onChange={(event) => setDraftFilters((current) => ({ ...current, fromDate: event.target.value }))} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="audit-to-date">To date</Label>
          <Input id="audit-to-date" type="date" value={draftFilters.toDate} onChange={(event) => setDraftFilters((current) => ({ ...current, toDate: event.target.value }))} />
        </div>
        <div className="space-y-2">
          <Label htmlFor="audit-user">User</Label>
          <Select value={draftFilters.actor} onValueChange={(actor) => setDraftFilters((current) => ({ ...current, actor }))}>
            <SelectTrigger id="audit-user"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All users</SelectItem>
              <SelectItem value="system">System</SelectItem>
              {(users.data ?? []).map((tenantUser) => (
                <SelectItem key={tenantUser.id} value={tenantUser.id}>{tenantUser.display_name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="audit-action">Action</Label>
          <Input id="audit-action" placeholder="For example, classification." value={draftFilters.actionPrefix} onChange={(event) => setDraftFilters((current) => ({ ...current, actionPrefix: event.target.value }))} />
        </div>
        <div className="flex gap-2">
          <Button type="submit"><Search />Apply</Button>
          <Button type="button" variant="outline" aria-label="Clear audit filters" onClick={clearFilters} disabled={!activeFilterCount && draftFilters === EMPTY_FILTERS}><FilterX /></Button>
        </div>
      </form>

      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {activeFilterCount ? `${activeFilterCount} active ${activeFilterCount === 1 ? "filter" : "filters"} · ` : ""}
          Up to 50 actions per page
        </p>
        {records.isFetching && !records.isPending ? <span className="text-xs text-muted-foreground">Refreshing…</span> : null}
      </div>

      {records.isPending || users.isPending ? <TableLoading /> : records.error || users.error ? (
        <QueryError message={records.error?.message ?? users.error?.message} />
      ) : (
        <>
          <div className="overflow-hidden rounded-xl border bg-card">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-muted/50">
                  <TableHead className="w-56">Date</TableHead>
                  <TableHead className="w-64">User</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Target</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data?.items.length ? data.items.map((record) => (
                  <TableRow key={record.id} className="cursor-pointer" tabIndex={0} onClick={() => setSelectedRecord(record)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedRecord(record); } }}>
                    <TableCell className="whitespace-nowrap text-muted-foreground">{formatTimestamp(record.created_at)}</TableCell>
                    <TableCell><span className="font-medium">{actorLabel(record)}</span>{record.actor_email && record.actor_display_name ? <span className="block text-xs text-muted-foreground">{record.actor_email}</span> : null}</TableCell>
                    <TableCell><span className="font-medium">{actionLabel(record.action)}</span><code className="block text-xs text-muted-foreground">{record.action}</code></TableCell>
                    <TableCell><span>{record.target_type}</span><code className="block max-w-64 truncate text-xs text-muted-foreground" title={record.target_id}>{record.target_id}</code></TableCell>
                  </TableRow>
                )) : (
                  <TableRow><TableCell colSpan={4} className="h-36 text-center text-muted-foreground">No audit actions match these filters.</TableCell></TableRow>
                )}
              </TableBody>
            </Table>
          </div>

          <div className="mt-4 flex items-center justify-end gap-2">
            <Button type="button" variant="outline" disabled={cursorStack.length === 1 || records.isFetching} onClick={() => setCursorStack((current) => current.slice(0, -1))}><ChevronLeft />Newer</Button>
            <Button type="button" variant="outline" disabled={!data?.next_cursor || records.isFetching} onClick={() => data?.next_cursor && setCursorStack((current) => [...current, data.next_cursor])}>Older<ChevronRight /></Button>
          </div>
        </>
      )}

      <Dialog open={selectedRecord !== null} onOpenChange={(open) => { if (!open) setSelectedRecord(null); }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{selectedRecord ? actionLabel(selectedRecord.action) : "Audit action"}</DialogTitle>
            <DialogDescription>Recorded audit details are read-only.</DialogDescription>
          </DialogHeader>
          {selectedRecord ? (
            <dl className="grid gap-4 text-sm sm:grid-cols-2">
              <div><dt className="font-medium text-muted-foreground">Date</dt><dd className="mt-1">{formatTimestamp(selectedRecord.created_at)}</dd></div>
              <div><dt className="font-medium text-muted-foreground">User</dt><dd className="mt-1">{actorLabel(selectedRecord)}</dd></div>
              <div className="sm:col-span-2"><dt className="font-medium text-muted-foreground">Action</dt><dd className="mt-1 font-mono text-xs">{selectedRecord.action}</dd></div>
              <div><dt className="font-medium text-muted-foreground">Target type</dt><dd className="mt-1">{selectedRecord.target_type}</dd></div>
              <div><dt className="font-medium text-muted-foreground">Target ID</dt><dd className="mt-1 break-all font-mono text-xs">{selectedRecord.target_id}</dd></div>
              <div className="sm:col-span-2"><dt className="font-medium text-muted-foreground">Details</dt><dd className="mt-2"><pre className="max-h-80 overflow-auto rounded-lg bg-muted p-3 text-xs leading-5">{JSON.stringify(selectedRecord.details ?? {}, null, 2)}</pre></dd></div>
            </dl>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
