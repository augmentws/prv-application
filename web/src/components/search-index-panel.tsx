"use client";

import { Activity, AlertTriangle, Clock3, Database, LoaderCircle, RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";

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
  onRetryFailed: () => Promise<void>;
  retryingFailed: boolean;
  retryableOperationCount?: number;
  retryableDocumentCount?: number;
}

type IndexHealth = "NOT_CREATED" | "BUILDING" | "ACTION_REQUIRED" | "READY" | "BEHIND" | "FAILED" | "MISSING" | "UNAVAILABLE";
interface RebuildProgress {
  phase: string;
  processedDocuments: number;
  totalDocuments: number;
  indexName?: string;
  documentsPerSecond?: number;
  estimatedCompletionAt?: string;
  updatedAt?: string;
}

export function SearchIndexPanel({ coreDocumentCount, indexes, operations, onRebuild, rebuilding, onConfirmReindex, confirmingReindex, onRetryFailed, retryingFailed, retryableOperationCount, retryableDocumentCount }: SearchIndexPanelProps) {
  const activeIndex = indexes.find((index) => index.status === "ACTIVE");
  const latestIndex = indexes[0];
  const activeOperation = operations.find((operation) => operation.status === "QUEUED" || operation.status === "RUNNING");
  const awaitingOperation = operations.find((operation) => operation.status === "AWAITING_USER");
  const latestStructuralOperation = operations.find((operation) => operation.kind === "REBUILD" || operation.kind === "SCHEMA_SYNC");
  const failedOperations = operations.filter((operation) => operation.status === "FAILED");
  const failedDocumentUpserts = failedOperations.filter((operation) => operation.kind === "DOCUMENT_UPSERT");
  const failedDocumentCount = failedDocumentUpserts.reduce((count, operation) => {
    const documentIds = operation.payload.document_ids;
    return count + (Array.isArray(documentIds) ? documentIds.length : 0);
  }, 0);
  const requeueOperationCount = retryableOperationCount ?? failedDocumentUpserts.length;
  const requeueDocumentCount = retryableDocumentCount ?? failedDocumentCount;
  const rebuildProgress = activeOperation?.kind === "REBUILD" ? parseRebuildProgress(activeOperation.payload.progress) : undefined;
  const [observedDocumentsPerSecond, setObservedDocumentsPerSecond] = useState<number>();
  const priorProgressSample = useRef<{ operationId: string; processedDocuments: number; updatedAt: number } | undefined>(undefined);
  useEffect(() => {
    let updateTimer: ReturnType<typeof setTimeout> | undefined;
    if (!activeOperation || !rebuildProgress) {
      priorProgressSample.current = undefined;
      updateTimer = setTimeout(() => setObservedDocumentsPerSecond(undefined), 0);
      return () => clearTimeout(updateTimer);
    }
    const updatedAt = rebuildProgress.updatedAt ? Date.parse(rebuildProgress.updatedAt) : Number.NaN;
    const prior = priorProgressSample.current;
    if (
      prior
      && prior.operationId === activeOperation.id
      && Number.isFinite(updatedAt)
      && updatedAt > prior.updatedAt
      && rebuildProgress.processedDocuments > prior.processedDocuments
    ) {
      const instantaneousRate = (
        (rebuildProgress.processedDocuments - prior.processedDocuments)
        / ((updatedAt - prior.updatedAt) / 1000)
      );
      updateTimer = setTimeout(() => setObservedDocumentsPerSecond((current) => current
        ? (current * 0.7) + (instantaneousRate * 0.3)
        : instantaneousRate), 0);
    } else if (!prior && rebuildProgress.documentsPerSecond) {
      updateTimer = setTimeout(() => setObservedDocumentsPerSecond(rebuildProgress.documentsPerSecond), 0);
    } else if (!prior && activeOperation.started_at && Number.isFinite(updatedAt) && rebuildProgress.processedDocuments > 0) {
      const elapsedSeconds = (updatedAt - Date.parse(activeOperation.started_at)) / 1000;
      if (elapsedSeconds > 0) {
        updateTimer = setTimeout(
          () => setObservedDocumentsPerSecond(rebuildProgress.processedDocuments / elapsedSeconds),
          0,
        );
      }
    }
    if (Number.isFinite(updatedAt)) {
      priorProgressSample.current = {
        operationId: activeOperation.id,
        processedDocuments: rebuildProgress.processedDocuments,
        updatedAt,
      };
    }
    return () => {
      if (updateTimer) clearTimeout(updateTimer);
    };
  }, [activeOperation, rebuildProgress]);
  const indexedDocumentCount = activeIndex?.live_document_count
    ?? (activeIndex?.physical_index_exists === false ? 0 : activeIndex?.document_count ?? 0);
  const countDelta = coreDocumentCount - indexedDocumentCount;
  const health = deriveHealth({ activeIndex, activeOperation, awaitingOperation, latestStructuralOperation, countDelta });

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Search index</h2>
          <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">Monitor the matter&apos;s rebuildable search projection. Core remains authoritative while indexing work runs.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {requeueOperationCount ? <RetryFailedDialog operationCount={requeueOperationCount} documentCount={requeueDocumentCount} onRetry={onRetryFailed} retrying={retryingFailed} /> : null}
          <RebuildDialog onRebuild={onRebuild} rebuilding={rebuilding} />
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard icon={Activity} label="Status"><HealthBadge health={health} /><p className="mt-3 text-sm text-muted-foreground">{healthDescription(health, countDelta)}</p></MetricCard>
        <MetricCard icon={Database} label="Indexed documents"><p className="text-3xl font-semibold tabular-nums">{indexedDocumentCount.toLocaleString()} <span className="text-lg font-medium text-muted-foreground">/ {coreDocumentCount.toLocaleString()}</span></p><p className="mt-2 text-sm text-muted-foreground">{indexCountDescription(activeIndex, countDelta)}</p></MetricCard>
        <MetricCard icon={RefreshCw} label="Active generation"><p className="text-3xl font-semibold tabular-nums">{activeIndex ? `#${activeIndex.generation}` : "—"}</p><p className="mt-2 text-sm text-muted-foreground">{latestIndex && !activeIndex ? `Generation #${latestIndex.generation} is ${friendlyStatus(latestIndex.status)}.` : "Only the active generation is searched."}</p></MetricCard>
        <MetricCard icon={Clock3} label="Last synchronized"><p className="text-2xl font-semibold">{activeIndex ? formatDate(activeIndex.activated_at ?? activeIndex.updated_at) : "—"}</p><p className="mt-2 text-sm text-muted-foreground">{activeOperation ? `${operationLabel(activeOperation.kind)} is ${friendlyStatus(activeOperation.status)}.` : failedOperations.length ? `${failedOperations.length} failed ${failedOperations.length === 1 ? "operation" : "operations"} in recent history.` : "No indexing work is currently running."}</p></MetricCard>
      </div>

      {activeOperation ? <Card className="border-accent/50 bg-accent/5 p-4"><div className="flex items-start gap-3"><LoaderCircle className="mt-0.5 size-4 shrink-0 animate-spin text-accent-foreground" /><div className="min-w-0 flex-1"><p className="font-semibold">{operationLabel(activeOperation.kind)} {friendlyStatus(activeOperation.status)}</p>{rebuildProgress ? <RebuildProgressView progress={rebuildProgress} observedDocumentsPerSecond={observedDocumentsPerSecond} /> : <p className="mt-1 text-sm text-muted-foreground">Started {formatDate(activeOperation.started_at ?? activeOperation.created_at)} · attempt {activeOperation.attempt_count.toLocaleString()}</p>}</div></div></Card> : null}
      {health === "MISSING" ? <Card className="border-destructive/30 bg-destructive/5 p-4"><div className="flex items-start gap-3"><AlertTriangle className="mt-0.5 size-5 text-destructive" /><div><p className="font-semibold">The active index is missing from OpenSearch</p><p className="mt-1 text-sm text-muted-foreground">Core still has generation history, but the configured OpenSearch server does not have the physical index and active alias. Start a full rebuild to recreate it.</p></div></div></Card> : null}
      {health === "UNAVAILABLE" ? <Card className="border-accent/50 bg-accent/5 p-4"><div className="flex items-start gap-3"><AlertTriangle className="mt-0.5 size-5 text-accent-foreground" /><div><p className="font-semibold">OpenSearch status could not be verified</p><p className="mt-1 text-sm text-muted-foreground">{activeIndex?.verification_error ?? "The configured OpenSearch server is unavailable."}</p></div></div></Card> : null}
      {awaitingOperation ? <ReindexConfirmationCard operation={awaitingOperation} onConfirm={onConfirmReindex} confirming={confirmingReindex} /> : null}

      <section aria-labelledby="generation-history-heading">
        <div className="mb-3 flex items-center justify-between gap-3"><div><h3 id="generation-history-heading" className="font-semibold">Current generation</h3><p className="mt-1 text-sm text-muted-foreground">After a replacement is activated, obsolete physical indexes and generation records are removed automatically.</p></div></div>
        <Card className="overflow-hidden">
          <Table>
            <TableHeader><TableRow><TableHead>Generation</TableHead><TableHead>Status</TableHead><TableHead>Documents</TableHead><TableHead>Schema</TableHead><TableHead>Updated</TableHead></TableRow></TableHeader>
            <TableBody>
              {indexes.length ? indexes.map((index) => <TableRow key={index.id}><TableCell className="font-semibold">#{index.generation}</TableCell><TableCell><IndexStatusBadge index={index} /></TableCell><TableCell className="tabular-nums">{(index.live_document_count ?? (index.physical_index_exists === false ? 0 : index.document_count)).toLocaleString()}</TableCell><TableCell><code className="text-xs text-muted-foreground">{shortHash(index.schema_hash)}</code></TableCell><TableCell className="whitespace-nowrap text-muted-foreground">{formatDate(index.updated_at)}</TableCell></TableRow>) : <TableRow><TableCell colSpan={5} className="h-24 text-center text-muted-foreground">No search index has been created for this matter.</TableCell></TableRow>}
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

function RetryFailedDialog({ operationCount, documentCount, onRetry, retrying }: {
  operationCount: number;
  documentCount: number;
  onRetry: () => Promise<void>;
  retrying: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string>();

  async function retry() {
    setError(undefined);
    try {
      await onRetry();
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The failed search jobs could not be requeued.");
    }
  }

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => { setOpen(nextOpen); if (!nextOpen) setError(undefined); }}>
      <DialogTrigger asChild><Button><RotateCcw />Requeue failed jobs</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Requeue failed or interrupted document updates?</DialogTitle><DialogDescription>This will retry {operationCount.toLocaleString()} failed or interrupted {operationCount === 1 ? "job" : "jobs"} covering {documentCount.toLocaleString()} {documentCount === 1 ? "document" : "documents"}. Existing indexed records are updated without creating duplicates.</DialogDescription></DialogHeader>
        {error ? <p role="alert" className="mt-4 text-sm text-destructive">{error}</p> : null}
        <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={retrying}>Cancel</Button><Button type="button" onClick={() => void retry()} disabled={retrying}>{retrying ? <LoaderCircle className="animate-spin" /> : <RotateCcw />}{retrying ? "Requeueing…" : "Confirm requeue"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RebuildProgressView({ progress, observedDocumentsPerSecond }: { progress: RebuildProgress; observedDocumentsPerSecond?: number }) {
  const percent = progress.totalDocuments > 0
    ? Math.min(100, Math.round((progress.processedDocuments / progress.totalDocuments) * 100))
    : 0;
  const documentsPerSecond = progress.documentsPerSecond ?? observedDocumentsPerSecond;
  const estimatedCompletionAt = progress.estimatedCompletionAt
    ?? (documentsPerSecond && progress.updatedAt && progress.totalDocuments > progress.processedDocuments
      ? new Date(Date.parse(progress.updatedAt) + ((progress.totalDocuments - progress.processedDocuments) / documentsPerSecond) * 1000).toISOString()
      : undefined);
  const remainingSeconds = documentsPerSecond && progress.totalDocuments > progress.processedDocuments
    ? (progress.totalDocuments - progress.processedDocuments) / documentsPerSecond
    : estimatedCompletionAt && progress.updatedAt
      ? Math.max(0, (Date.parse(estimatedCompletionAt) - Date.parse(progress.updatedAt)) / 1000)
      : undefined;
  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
        <span>{rebuildPhaseLabel(progress.phase)}</span>
        <span className="tabular-nums">
          {progress.totalDocuments > 0
            ? `${progress.processedDocuments.toLocaleString()} / ${progress.totalDocuments.toLocaleString()} documents · ${percent}%`
            : "Preparing document count…"}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label="Search index rebuild progress"
        aria-valuemin={0}
        aria-valuemax={progress.totalDocuments || 1}
        aria-valuenow={progress.processedDocuments}
        className="h-2 overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} />
      </div>
      <p className="text-sm text-muted-foreground">
        {documentsPerSecond
          ? `${documentsPerSecond.toFixed(1)} docs/sec${remainingSeconds !== undefined ? ` · Estimated completion in ${formatRemainingDuration(remainingSeconds)}` : ""}`
          : "Calculating throughput and estimated completion…"}
      </p>
      {progress.indexName ? <p className="truncate font-mono text-xs text-muted-foreground">{progress.indexName}</p> : null}
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
    MISSING: "border-destructive/20 bg-destructive/10 text-destructive",
    UNAVAILABLE: "border-accent/50 bg-accent/20 text-accent-foreground",
  };
  return <Badge variant="outline" className={styles[health]}>{health === "NOT_CREATED" ? "Not created" : health === "ACTION_REQUIRED" ? "Action required" : friendlyStatus(health)}</Badge>;
}

function IndexStatusBadge({ index }: { index: SearchIndexGenerationRead }) {
  if (index.verification_error) return <Badge variant="accent">Unverified</Badge>;
  if (index.physical_index_exists === false || index.alias_points_to_index === false) return <Badge className="bg-destructive/10 text-destructive">Missing</Badge>;
  if (index.status === "ACTIVE") return <Badge variant="active">Active</Badge>;
  if (index.status === "CREATING") return <Badge variant="accent">Creating</Badge>;
  if (index.status === "FAILED") return <Badge className="bg-destructive/10 text-destructive">Failed</Badge>;
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
  if (activeIndex.verification_error) return "UNAVAILABLE";
  if (activeIndex.physical_index_exists === false || activeIndex.alias_points_to_index === false) return "MISSING";
  if (countDelta !== 0) return "BEHIND";
  return "READY";
}

function healthDescription(health: IndexHealth, countDelta: number) {
  if (health === "NOT_CREATED") return "Request a rebuild to make this matter searchable.";
  if (health === "BUILDING") return "A projection workflow is currently in progress.";
  if (health === "ACTION_REQUIRED") return "A schema change needs confirmation before the matter is reindexed.";
  if (health === "FAILED") return "The latest index build failed. Review the operation details and retry.";
  if (health === "MISSING") return "The recorded active generation is not present on the configured OpenSearch server.";
  if (health === "UNAVAILABLE") return "The configured OpenSearch server could not be checked.";
  if (health === "BEHIND") return countDelta > 0 ? `${countDelta.toLocaleString()} ${countDelta === 1 ? "document is" : "documents are"} awaiting projection.` : "The index count does not match Core.";
  return "The active projection matches the Core document count.";
}

function countDescription(delta: number) {
  if (delta === 0) return "Index and Core counts match.";
  if (delta > 0) return `${delta.toLocaleString()} pending ${delta === 1 ? "document" : "documents"}.`;
  return `${Math.abs(delta).toLocaleString()} more ${Math.abs(delta) === 1 ? "document" : "documents"} than Core; rebuild recommended.`;
}

function indexCountDescription(index: SearchIndexGenerationRead | undefined, delta: number) {
  if (index?.verification_error) return "Live OpenSearch count unavailable.";
  if (index?.physical_index_exists === false) return `No live index found; Core previously recorded ${index.document_count.toLocaleString()} indexed documents.`;
  return countDescription(delta);
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

function parseRebuildProgress(value: unknown): RebuildProgress | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const progress = value as Record<string, unknown>;
  if (typeof progress.phase !== "string") return undefined;
  return {
    phase: progress.phase,
    processedDocuments: typeof progress.processed_documents === "number" ? progress.processed_documents : 0,
    totalDocuments: typeof progress.total_documents === "number" ? progress.total_documents : 0,
    indexName: typeof progress.index_name === "string" ? progress.index_name : undefined,
    documentsPerSecond: typeof progress.documents_per_second === "number" && progress.documents_per_second > 0 ? progress.documents_per_second : undefined,
    estimatedCompletionAt: typeof progress.estimated_completion_at === "string" ? progress.estimated_completion_at : undefined,
    updatedAt: typeof progress.updated_at === "string" ? progress.updated_at : undefined,
  };
}

function rebuildPhaseLabel(phase: string) {
  const labels: Record<string, string> = {
    QUEUED: "Waiting for a workflow worker",
    CREATING_INDEX: "Creating the physical index",
    INDEXING_DOCUMENTS: "Indexing matter documents",
    REFRESHING_INDEX: "Refreshing the completed index",
    ACTIVATING_ALIAS: "Activating the new index",
    CLEANING_UP: "Removing obsolete index generations",
    COMPLETED: "Rebuild completed",
  };
  return labels[phase] ?? friendlyStatus(phase);
}

function formatRemainingDuration(seconds: number) {
  if (seconds < 3600) {
    const minutes = Math.max(1, Math.ceil(seconds / 60));
    return `${minutes} ${minutes === 1 ? "minute" : "minutes"}`;
  }
  return `${(seconds / 3600).toFixed(1)} hours`;
}

function friendlyStatus(status: string) {
  return status.toLowerCase().replaceAll("_", " ").replace(/^./, (character) => character.toUpperCase());
}

function shortHash(value: string) {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}
