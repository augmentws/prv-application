"use client";

import { Activity, AlertTriangle, Clock3, Database, LoaderCircle, RefreshCw } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import type { SearchIndexGenerationRead, SearchProjectionOperationRead } from "@/generated/models";
import { formatDate } from "@/lib/format";

interface SearchIndexPanelProps {
  coreDocumentCount: number;
  indexes: SearchIndexGenerationRead[];
  operations: SearchProjectionOperationRead[];
  onRebuild: () => Promise<void>;
  rebuilding: boolean;
  onConfirmReindex: (operationId: string) => Promise<void>;
  confirmingReindex: boolean;
}

type IndexHealth = "NOT_CREATED" | "BUILDING" | "ACTION_REQUIRED" | "READY" | "BEHIND" | "FAILED";

export function SearchIndexPanel({ coreDocumentCount, indexes, operations, onRebuild, rebuilding, onConfirmReindex, confirmingReindex }: SearchIndexPanelProps) {
  const activeIndex = indexes.find((index) => index.status === "ACTIVE");
  const latestIndex = indexes[0];
  const activeOperation = operations.find((operation) => operation.status === "QUEUED" || operation.status === "RUNNING");
  const awaitingOperation = operations.find((operation) => operation.status === "AWAITING_USER");
  const latestStructuralOperation = operations.find((operation) => operation.kind === "REBUILD" || operation.kind === "SCHEMA_SYNC");
  const failedOperations = operations.filter((operation) => operation.status === "FAILED");
  const indexedDocumentCount = activeIndex?.document_count ?? 0;
  const countDelta = coreDocumentCount - indexedDocumentCount;
  const health = deriveHealth({ activeIndex, activeOperation, awaitingOperation, latestStructuralOperation, countDelta });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Search index</h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">Monitor the matter&apos;s rebuildable search projection. Core remains authoritative while indexing work runs.</p>
        </div>
        <RebuildDialog onRebuild={onRebuild} rebuilding={rebuilding} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard icon={Activity} label="Status"><HealthBadge health={health} /><p className="mt-3 text-sm text-muted-foreground">{healthDescription(health, countDelta)}</p></MetricCard>
        <MetricCard icon={Database} label="Indexed documents"><p className="text-3xl font-semibold tabular-nums">{indexedDocumentCount.toLocaleString()} <span className="text-lg font-medium text-muted-foreground">/ {coreDocumentCount.toLocaleString()}</span></p><p className="mt-2 text-sm text-muted-foreground">{countDescription(countDelta)}</p></MetricCard>
        <MetricCard icon={RefreshCw} label="Active generation"><p className="text-3xl font-semibold tabular-nums">{activeIndex ? `#${activeIndex.generation}` : "—"}</p><p className="mt-2 text-sm text-muted-foreground">{latestIndex && !activeIndex ? `Generation #${latestIndex.generation} is ${friendlyStatus(latestIndex.status)}.` : "Only the active generation is searched."}</p></MetricCard>
        <MetricCard icon={Clock3} label="Last synchronized"><p className="text-2xl font-semibold">{activeIndex ? formatDate(activeIndex.activated_at ?? activeIndex.updated_at) : "—"}</p><p className="mt-2 text-sm text-muted-foreground">{activeOperation ? `${operationLabel(activeOperation.kind)} is ${friendlyStatus(activeOperation.status)}.` : failedOperations.length ? `${failedOperations.length} failed ${failedOperations.length === 1 ? "operation" : "operations"} in recent history.` : "No indexing work is currently running."}</p></MetricCard>
      </div>

      {activeOperation ? <Card className="border-accent/50 bg-accent/5 p-4"><div className="flex items-start gap-3"><LoaderCircle className="mt-0.5 size-4 animate-spin text-accent-foreground" /><div><p className="font-semibold">{operationLabel(activeOperation.kind)} {friendlyStatus(activeOperation.status)}</p><p className="mt-1 text-sm text-muted-foreground">Started {formatDate(activeOperation.started_at ?? activeOperation.created_at)} · attempt {activeOperation.attempt_count.toLocaleString()}</p></div></div></Card> : null}
      {awaitingOperation ? <ReindexConfirmationCard operation={awaitingOperation} onConfirm={onConfirmReindex} confirming={confirmingReindex} /> : null}

      <section aria-labelledby="generation-history-heading">
        <div className="mb-3 flex items-center justify-between gap-3"><div><h3 id="generation-history-heading" className="font-semibold">Current generation</h3><p className="mt-1 text-sm text-muted-foreground">After a replacement is activated, obsolete physical indexes and generation records are removed automatically.</p></div></div>
        <Card className="overflow-hidden">
          <Table>
            <TableHeader><TableRow><TableHead>Generation</TableHead><TableHead>Status</TableHead><TableHead>Documents</TableHead><TableHead>Schema</TableHead><TableHead>Updated</TableHead></TableRow></TableHeader>
            <TableBody>
              {indexes.length ? indexes.map((index) => <TableRow key={index.id}><TableCell className="font-semibold">#{index.generation}</TableCell><TableCell><IndexStatusBadge status={index.status} /></TableCell><TableCell className="tabular-nums">{index.document_count.toLocaleString()}</TableCell><TableCell><code className="text-xs text-muted-foreground">{shortHash(index.schema_hash)}</code></TableCell><TableCell className="whitespace-nowrap text-muted-foreground">{formatDate(index.updated_at)}</TableCell></TableRow>) : <TableRow><TableCell colSpan={5} className="h-24 text-center text-muted-foreground">No search index has been created for this matter.</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
      </section>

      <section aria-labelledby="operation-history-heading">
        <div className="mb-3"><h3 id="operation-history-heading" className="font-semibold">Recent operations</h3><p className="mt-1 text-sm text-muted-foreground">Asynchronous schema, rebuild, and document projection work.</p></div>
        <Card className="overflow-hidden">
          <Table>
            <TableHeader><TableRow><TableHead>Operation</TableHead><TableHead>Status</TableHead><TableHead>Attempts</TableHead><TableHead>Created</TableHead><TableHead>Details</TableHead></TableRow></TableHeader>
            <TableBody>
              {operations.length ? operations.slice(0, 10).map((operation) => <TableRow key={operation.id}><TableCell className="font-semibold">{operationLabel(operation.kind)}</TableCell><TableCell><OperationStatusBadge status={operation.status} /></TableCell><TableCell className="tabular-nums">{operation.attempt_count.toLocaleString()}</TableCell><TableCell className="whitespace-nowrap text-muted-foreground">{formatDate(operation.created_at)}</TableCell><TableCell className="max-w-sm text-sm text-muted-foreground">{operation.error_message ?? (operation.completed_at ? `Completed ${formatDate(operation.completed_at)}` : "—")}</TableCell></TableRow>) : <TableRow><TableCell colSpan={5} className="h-24 text-center text-muted-foreground">No search operations have been recorded.</TableCell></TableRow>}
            </TableBody>
          </Table>
        </Card>
      </section>
    </div>
  );
}

function ReindexConfirmationCard({ operation, onConfirm, confirming }: {
  operation: SearchProjectionOperationRead;
  onConfirm: (operationId: string) => Promise<void>;
  confirming: boolean;
}) {
  const [open, setOpen] = useState(false);
  const schemaChange = operation.payload.schema_change;
  const reasonValues = schemaChange && typeof schemaChange === "object" && !Array.isArray(schemaChange)
    ? (schemaChange as Record<string, unknown>).reasons
    : undefined;
  const reasons = Array.isArray(reasonValues)
    ? reasonValues.filter((reason): reason is string => typeof reason === "string")
    : [];
  return <Card className="border-accent/50 bg-accent/5 p-4"><div className="flex flex-wrap items-start justify-between gap-4"><div className="flex max-w-3xl items-start gap-3"><AlertTriangle className="mt-0.5 size-5 shrink-0 text-accent-foreground" /><div><p className="font-semibold">Search changes require a full reindex</p><p className="mt-1 text-sm text-muted-foreground">The active index remains available. Confirm before the system builds a replacement.</p>{reasons.length ? <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}</div></div><Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button><AlertTriangle />Review and confirm</Button></DialogTrigger><DialogContent><DialogHeader><DialogTitle>Build a replacement search index?</DialogTitle><DialogDescription>This operation must reread and reindex every matter document. The current index remains searchable until the replacement is ready.</DialogDescription></DialogHeader>{reasons.length ? <ul className="list-disc space-y-2 pl-5 text-sm text-muted-foreground">{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul> : null}<DialogFooter><Button variant="outline" onClick={() => setOpen(false)} disabled={confirming}>Cancel</Button><Button onClick={() => void onConfirm(operation.id).then(() => setOpen(false))} disabled={confirming}>{confirming ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}{confirming ? "Queuing…" : "Confirm full reindex"}</Button></DialogFooter></DialogContent></Dialog></div></Card>;
}

function RebuildDialog({ onRebuild, rebuilding }: { onRebuild: () => Promise<void>; rebuilding: boolean }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string>();

  async function rebuild() {
    setError(undefined);
    try {
      await onRebuild();
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The rebuild could not be requested.");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) setError(undefined); }}>
      <DialogTrigger asChild><Button variant="outline"><RefreshCw />Rebuild entire index</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Rebuild the search index?</DialogTitle><DialogDescription>This creates and populates a new physical index from authoritative matter data. The current active index remains searchable until the replacement is ready.</DialogDescription></DialogHeader>
        <div className="flex gap-3 rounded-lg bg-muted/60 p-4 text-sm"><AlertTriangle className="mt-0.5 size-4 shrink-0 text-accent-foreground" /><p>Large matters may take time to rebuild. You can leave this page and return to monitor the operation.</p></div>
        {error ? <p role="alert" className="mt-4 text-sm text-destructive">{error}</p> : null}
        <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={rebuilding}>Cancel</Button><Button type="button" onClick={() => void rebuild()} disabled={rebuilding}>{rebuilding ? <LoaderCircle className="animate-spin" /> : <RefreshCw />}{rebuilding ? "Requesting rebuild…" : "Start rebuild"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MetricCard({ icon: Icon, label, children }: { icon: typeof Activity; label: string; children: React.ReactNode }) {
  return <Card><CardHeader><span className="mb-2 grid size-9 place-items-center rounded-lg bg-secondary text-primary"><Icon className="size-4" /></span><CardTitle className="text-sm text-muted-foreground">{label}</CardTitle></CardHeader><CardContent>{children}</CardContent></Card>;
}

function HealthBadge({ health }: { health: IndexHealth }) {
  const styles: Record<IndexHealth, string> = {
    NOT_CREATED: "border-border bg-background text-muted-foreground",
    BUILDING: "border-accent/40 bg-accent/20 text-accent-foreground",
    ACTION_REQUIRED: "border-accent/50 bg-accent/20 text-accent-foreground",
    READY: "border-success/20 bg-success/12 text-success",
    BEHIND: "border-accent/40 bg-accent/20 text-accent-foreground",
    FAILED: "border-destructive/20 bg-destructive/10 text-destructive",
  };
  return <Badge variant="outline" className={styles[health]}>{health === "NOT_CREATED" ? "Not created" : health === "ACTION_REQUIRED" ? "Action required" : friendlyStatus(health)}</Badge>;
}

function IndexStatusBadge({ status }: { status: SearchIndexGenerationRead["status"] }) {
  if (status === "ACTIVE") return <Badge variant="active">Active</Badge>;
  if (status === "CREATING") return <Badge variant="accent">Creating</Badge>;
  if (status === "FAILED") return <Badge className="bg-destructive/10 text-destructive">Failed</Badge>;
  return <Badge variant="outline">Retired</Badge>;
}

function OperationStatusBadge({ status }: { status: SearchProjectionOperationRead["status"] }) {
  if (status === "COMPLETED") return <Badge variant="active">Completed</Badge>;
  if (status === "QUEUED" || status === "RUNNING") return <Badge variant="accent">{friendlyStatus(status)}</Badge>;
  if (status === "AWAITING_USER") return <Badge variant="accent">Action required</Badge>;
  return <Badge className="bg-destructive/10 text-destructive">Failed</Badge>;
}

function deriveHealth({ activeIndex, activeOperation, awaitingOperation, latestStructuralOperation, countDelta }: {
  activeIndex?: SearchIndexGenerationRead;
  activeOperation?: SearchProjectionOperationRead;
  awaitingOperation?: SearchProjectionOperationRead;
  latestStructuralOperation?: SearchProjectionOperationRead;
  countDelta: number;
}): IndexHealth {
  if (activeOperation) return "BUILDING";
  if (awaitingOperation) return "ACTION_REQUIRED";
  if (latestStructuralOperation?.status === "FAILED" && (!activeIndex || latestStructuralOperation.created_at > activeIndex.updated_at)) return "FAILED";
  if (!activeIndex) return "NOT_CREATED";
  if (countDelta !== 0) return "BEHIND";
  return "READY";
}

function healthDescription(health: IndexHealth, countDelta: number) {
  if (health === "NOT_CREATED") return "Request a rebuild to make this matter searchable.";
  if (health === "BUILDING") return "A projection workflow is currently in progress.";
  if (health === "ACTION_REQUIRED") return "A schema change needs confirmation before the matter is reindexed.";
  if (health === "FAILED") return "The latest index build failed. Review the operation details and retry.";
  if (health === "BEHIND") return countDelta > 0 ? `${countDelta.toLocaleString()} ${countDelta === 1 ? "document is" : "documents are"} awaiting projection.` : "The index count does not match Core.";
  return "The active projection matches the Core document count.";
}

function countDescription(delta: number) {
  if (delta === 0) return "Index and Core counts match.";
  if (delta > 0) return `${delta.toLocaleString()} pending ${delta === 1 ? "document" : "documents"}.`;
  return `${Math.abs(delta).toLocaleString()} more ${Math.abs(delta) === 1 ? "document" : "documents"} than Core; rebuild recommended.`;
}

function operationLabel(kind: SearchProjectionOperationRead["kind"]) {
  const labels: Record<SearchProjectionOperationRead["kind"], string> = {
    SCHEMA_SYNC: "Schema sync",
    REBUILD: "Full rebuild",
    DOCUMENT_UPSERT: "Document update",
    DOCUMENT_DELETE: "Document removal",
  };
  return labels[kind];
}

function friendlyStatus(status: string) {
  return status.toLowerCase().replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function shortHash(value: string) {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}
