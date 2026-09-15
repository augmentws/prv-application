"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, ChevronLeft, ChevronRight, FileText, Save, SkipForward, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useMemo, useState } from "react";
import { toast } from "sonner";

import { BrandMark } from "@/components/brand-mark";
import { DocumentViewerSurface } from "@/components/document-viewer-dialog";
import { QueryError } from "@/components/query-state";
import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import type {
  CollectionItemRead,
  MatterRead,
  ReviewBatchCodingFieldRead,
  ReviewBatchDocumentCodingRead,
  ReviewBatchDocumentRead,
  ReviewBatchRead,
  ReviewBatchRunDocumentValues,
  ReviewBatchRunProgressRead,
  ReviewBatchRunRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 100;

interface SnapshotOption {
  key: string;
  label: string;
  active?: boolean;
}

interface SnapshotDefinition {
  key: string;
  display_name: string;
  description?: string | null;
  type: "TEXT" | "LONG_TEXT" | "INTEGER" | "DECIMAL" | "BOOLEAN" | "DATE" | "DATETIME" | "ENUM" | "JSON";
  cardinality: "SINGLE" | "MULTIPLE";
  allowed_values: SnapshotOption[];
}

function snapshot(field: ReviewBatchCodingFieldRead): SnapshotDefinition {
  const value = field.definition_snapshot;
  const allowed = Array.isArray(value.allowed_values)
    ? value.allowed_values.filter((item): item is SnapshotOption => Boolean(item && typeof item === "object" && "key" in item && "label" in item))
    : [];
  return {
    key: typeof value.key === "string" ? value.key : field.metadata_definition_id,
    display_name: typeof value.display_name === "string" ? value.display_name : "Coding field",
    description: typeof value.description === "string" ? value.description : null,
    type: typeof value.type === "string" ? value.type as SnapshotDefinition["type"] : "TEXT",
    cardinality: value.cardinality === "MULTIPLE" ? "MULTIPLE" : "SINGLE",
    allowed_values: allowed,
  };
}

function parseValue(value: string, definition: SnapshotDefinition): unknown {
  if (definition.type === "INTEGER") {
    const parsed = Number(value);
    if (!Number.isSafeInteger(parsed)) throw new Error(`${definition.display_name} must be a whole number.`);
    return parsed;
  }
  if (definition.type === "DECIMAL") {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) throw new Error(`${definition.display_name} must be a number.`);
    return parsed;
  }
  if (definition.type === "BOOLEAN") return value === "true";
  if (definition.type === "JSON") {
    const parsed = JSON.parse(value) as unknown;
    if (parsed === null || typeof parsed !== "object") throw new Error(`${definition.display_name} must be a JSON object or array.`);
    return parsed;
  }
  if (definition.type === "DATETIME") {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) throw new Error(`${definition.display_name} must be a date and time.`);
    return parsed.toISOString();
  }
  return value;
}

function editorValue(value: unknown, definition: SnapshotDefinition) {
  if (value === null || value === undefined) return "";
  if (definition.type === "JSON") return JSON.stringify(value, null, 2);
  if (definition.type === "DATETIME") {
    const parsed = new Date(String(value));
    if (!Number.isNaN(parsed.valueOf())) return parsed.toISOString().slice(0, 16);
  }
  return String(value);
}

function statusLabel(status: ReviewBatchDocumentRead["review_status"]) {
  return status.toLowerCase().replace("_", " ");
}

export function BatchReviewWorkspace({ matterId, batchId, initialDocumentId }: {
  matterId: string;
  batchId: string;
  initialDocumentId?: string;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [selectedDocumentId, setSelectedDocumentId] = useState(initialDocumentId ?? "");

  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const batch = useQuery({ queryKey: ["review-batch", matterId, batchId], queryFn: () => coreApi<ReviewBatchRead>(`/v1/matters/${matterId}/review-batches/${batchId}`) });
  const run = useQuery({
    queryKey: ["review-batch-run", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchRunRead>(`/v1/matters/${matterId}/review-batches/${batchId}/review-run`, { method: "POST" }),
    enabled: batch.data?.status === "READY",
    retry: false,
  });
  const documents = useQuery({
    queryKey: ["review-batch-documents", matterId, batchId, run.data?.id, offset],
    queryFn: () => coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?run_id=${run.data!.id}&offset=${offset}&limit=${PAGE_SIZE}`),
    enabled: Boolean(run.data?.id),
  });
  const progress = useQuery({
    queryKey: ["review-batch-progress", matterId, batchId, run.data?.id],
    queryFn: () => coreApi<ReviewBatchRunProgressRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/progress`),
    enabled: Boolean(run.data?.id),
  });

  const effectiveDocumentId = documents.data?.some((document) => document.matter_document_id === selectedDocumentId)
    ? selectedDocumentId
    : documents.data?.find((document) => document.review_status === "NOT_STARTED")?.matter_document_id
      ?? documents.data?.[0]?.matter_document_id
      ?? "";
  const selectedDocument = documents.data?.find((document) => document.matter_document_id === effectiveDocumentId);
  const collectionItem = useQuery({
    queryKey: ["collection-item", selectedDocument?.collection_item_id],
    queryFn: () => coreApi<CollectionItemRead>(`/v1/collection-items/${selectedDocument!.collection_item_id}`),
    enabled: Boolean(selectedDocument?.collection_item_id),
  });
  const coding = useQuery({
    queryKey: ["review-batch-document-coding", matterId, batchId, run.data?.id, effectiveDocumentId],
    queryFn: () => coreApi<ReviewBatchDocumentCodingRead>(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}`),
    enabled: Boolean(run.data?.id && effectiveDocumentId),
  });

  const selectDocument = (documentId: string) => {
    setSelectedDocumentId(documentId);
    router.replace(`/review/matters/${matterId}?batch=${batchId}&document=${documentId}`, { scroll: false });
  };

  const refreshReview = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["review-batch-documents", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-progress", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-document-coding", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batch-run", matterId, batchId] }),
      queryClient.invalidateQueries({ queryKey: ["review-batches", matterId] }),
    ]);
  };

  const moveNext = () => {
    const index = documents.data?.findIndex((document) => document.matter_document_id === effectiveDocumentId) ?? -1;
    const next = documents.data?.[index + 1];
    if (next) selectDocument(next.matter_document_id);
    else if (offset + PAGE_SIZE < (batch.data?.document_count ?? 0)) {
      setOffset((current) => current + PAGE_SIZE);
      setSelectedDocumentId("");
    }
  };

  const save = useMutation({
    mutationFn: (payload: ReviewBatchRunDocumentValues) => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}/values`, { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document coding saved.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document coding could not be saved."),
  });
  const skip = useMutation({
    mutationFn: () => coreApi(`/v1/matters/${matterId}/review-batches/${batchId}/runs/${run.data!.id}/documents/${effectiveDocumentId}/skip`, { method: "POST" }),
    onSuccess: async () => {
      await refreshReview();
      toast.success("Document skipped.");
      moveNext();
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The document could not be skipped."),
  });

  const currentIndex = documents.data?.findIndex((document) => document.matter_document_id === effectiveDocumentId) ?? -1;
  const processed = (progress.data?.completed_count ?? 0) + (progress.data?.skipped_count ?? 0);
  const percent = progress.data?.document_count ? Math.round((processed / progress.data.document_count) * 100) : 0;
  const fatalError = matter.error ?? batch.error ?? run.error ?? documents.error;

  if (fatalError) return <main className="grid h-dvh place-items-center p-6"><QueryError message={fatalError.message} /></main>;

  return (
    <main id="main-content" className="flex h-dvh min-h-[36rem] flex-col overflow-hidden bg-background">
      <header className="shrink-0 border-b bg-card shadow-sm">
        <div className="flex min-h-14 flex-wrap items-center gap-3 px-3 py-2">
          <BrandMark className="size-8 shrink-0 rounded-lg" />
          <Button asChild variant="ghost" size="sm"><Link href={matter.data ? `/app/clients/${matter.data.client_id}/matters/${matterId}?tab=batches` : "/app/clients"}><ArrowLeft />Batches</Link></Button>
          <div className="min-w-0 flex-1 border-l pl-3">
            <h1 className="truncate text-sm font-semibold">{batch.data?.name ?? "Opening batch…"}</h1>
            <p className="truncate text-xs text-muted-foreground">{matter.data?.name ?? "Batch review"}</p>
          </div>
          {run.data?.status === "COMPLETED" ? <Badge variant="accent"><Check />Review complete</Badge> : null}
          <ThemeToggle />
        </div>
        <div className="flex h-10 items-center gap-3 border-t px-3 text-xs text-muted-foreground">
          <div className="h-1.5 min-w-24 flex-1 overflow-hidden rounded-full bg-muted" aria-label={`${percent}% reviewed`}><div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${percent}%` }} /></div>
          <span className="shrink-0 tabular-nums">{processed.toLocaleString()} of {(progress.data?.document_count ?? batch.data?.document_count ?? 0).toLocaleString()} reviewed · {percent}%</span>
        </div>
      </header>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <aside className="flex w-80 shrink-0 flex-col border-r bg-card" aria-label="Batch documents">
          <div className="flex h-11 shrink-0 items-center justify-between border-b px-3">
            <h2 className="text-sm font-semibold">Documents</h2>
            <span className="text-xs text-muted-foreground">{offset + 1}–{Math.min(offset + PAGE_SIZE, batch.data?.document_count ?? 0)}</span>
          </div>
          {documents.isPending ? <div className="space-y-2 p-3">{Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-16" />)}</div>
            : documents.data?.length ? <ol className="min-h-0 flex-1 overflow-y-auto">{documents.data.map((document) => {
              const selected = document.matter_document_id === effectiveDocumentId;
              return <li key={document.matter_document_id} className="border-b"><button type="button" onClick={() => selectDocument(document.matter_document_id)} aria-current={selected ? "true" : undefined} className={cn("w-full border-l-[3px] px-3 py-3 text-left outline-none hover:bg-muted/70 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring", selected ? "border-l-accent bg-primary/8" : "border-l-transparent")}><div className="flex items-center gap-3"><span className="w-8 shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{document.sequence_number}</span><div className="min-w-0 flex-1"><p className="truncate text-sm font-semibold">Document {document.sequence_number.toLocaleString()}</p><p className="truncate text-xs text-muted-foreground">{document.collection_item_id}</p></div><Badge variant={document.review_status === "COMPLETED" ? "accent" : "outline"}>{statusLabel(document.review_status)}</Badge></div></button></li>;
            })}</ol>
              : <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">This batch has no documents.</div>}
          <div className="flex h-12 shrink-0 items-center justify-between border-t px-2">
            <Button variant="ghost" size="sm" disabled={!offset} onClick={() => { setOffset(Math.max(0, offset - PAGE_SIZE)); setSelectedDocumentId(""); }}><ChevronLeft />Previous</Button>
            <Button variant="ghost" size="sm" disabled={offset + PAGE_SIZE >= (batch.data?.document_count ?? 0)} onClick={() => { setOffset(offset + PAGE_SIZE); setSelectedDocumentId(""); }}>Next<ChevronRight /></Button>
          </div>
        </aside>

        <section className="flex min-h-0 min-w-0 flex-1 bg-background" aria-label="Selected document">
          {collectionItem.isPending && selectedDocument ? <div className="w-full space-y-3 p-5">{Array.from({ length: 10 }, (_, index) => <Skeleton key={index} className="h-5" />)}</div>
            : collectionItem.error ? <div className="w-full p-5"><QueryError message={collectionItem.error.message} /></div>
              : collectionItem.data ? <DocumentViewerSurface item={collectionItem.data} className="h-full w-full" />
                : <div className="grid h-full w-full place-items-center p-8 text-center"><div><FileText className="mx-auto text-muted-foreground" /><p className="mt-3 font-semibold">Select a document</p></div></div>}
        </section>

        <aside className="flex w-[23rem] shrink-0 flex-col border-l bg-card" aria-label="Batch coding">
          <div className="flex h-11 shrink-0 items-center justify-between border-b px-3"><h2 className="text-sm font-semibold">Coding</h2>{coding.data ? <Badge variant="outline">{statusLabel(coding.data.review_status)}</Badge> : null}</div>
          {coding.isPending && effectiveDocumentId ? <div className="space-y-3 p-4">{Array.from({ length: 6 }, (_, index) => <Skeleton key={index} className="h-16" />)}</div>
            : coding.error ? <div className="p-4"><QueryError message={coding.error.message} /></div>
              : coding.data && batch.data && run.data ? <BatchCodingForm
                key={`${effectiveDocumentId}:${coding.dataUpdatedAt}`}
                batch={batch.data}
                coding={coding.data}
                disabled={run.data.status !== "RUNNING"}
                saving={save.isPending || skip.isPending}
                onSave={(payload) => save.mutateAsync(payload).then(() => undefined)}
                onSkip={() => skip.mutateAsync().then(() => undefined)}
              />
                : <div className="grid flex-1 place-items-center p-5 text-center text-sm text-muted-foreground">Select a document to begin coding.</div>}
        </aside>
      </div>
      <div className="flex h-11 shrink-0 items-center justify-center gap-2 border-t bg-card px-3">
        <Button variant="ghost" size="sm" disabled={currentIndex <= 0} onClick={() => documents.data?.[currentIndex - 1] && selectDocument(documents.data[currentIndex - 1].matter_document_id)}><ChevronLeft />Previous document</Button>
        <Button variant="ghost" size="sm" disabled={currentIndex < 0 || currentIndex >= (documents.data?.length ?? 0) - 1} onClick={() => documents.data?.[currentIndex + 1] && selectDocument(documents.data[currentIndex + 1].matter_document_id)}>Next document<ChevronRight /></Button>
      </div>
    </main>
  );
}

function BatchCodingForm({ batch, coding, disabled, saving, onSave, onSkip }: {
  batch: ReviewBatchRead;
  coding: ReviewBatchDocumentCodingRead;
  disabled: boolean;
  saving: boolean;
  onSave: (payload: ReviewBatchRunDocumentValues) => Promise<void>;
  onSkip: () => Promise<void>;
}) {
  const fields = useMemo(() => {
    const unique = new Map<string, { field: ReviewBatchCodingFieldRead; definition: SnapshotDefinition }>();
    for (const group of batch.coding_groups) {
      for (const field of group.fields) {
        if (!unique.has(field.metadata_definition_id)) {
          unique.set(field.metadata_definition_id, { field, definition: snapshot(field) });
        }
      }
    }
    return [...unique.values()];
  }, [batch.coding_groups]);
  const initial = useMemo(() => Object.fromEntries(fields.map(({ field, definition }) => [
    field.metadata_definition_id,
    coding.values.filter((value) => value.metadata_definition_id === field.metadata_definition_id).map((value) => editorValue(value.value, definition)),
  ])), [coding.values, fields]);
  const [drafts, setDrafts] = useState<Record<string, string[]>>(initial);
  const dirty = JSON.stringify(drafts) !== JSON.stringify(initial);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    try {
      await onSave({
        matter_document_id: coding.matter_document_id,
        fields: fields.map(({ field, definition }) => ({
          metadata_definition_id: field.metadata_definition_id,
          values: (drafts[field.metadata_definition_id] ?? []).filter((value) => value.trim()).map((value) => parseValue(value, definition)),
          confidence: null,
        })),
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Check the entered values.");
    }
  };

  return <form className="flex min-h-0 flex-1 flex-col overflow-hidden" onSubmit={submit}>
    <div className="min-h-0 flex-1 divide-y overflow-y-auto">
      {batch.coding_groups.map((group) => <section key={group.id} className="p-3"><h3 className="mb-3 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">{group.display_name}</h3><div className="space-y-4">{group.fields.map((field) => {
        const definition = snapshot(field);
        const visible = coding.reviewer_values.filter((value) => value.metadata_definition_id === field.metadata_definition_id);
        return <div key={field.id}><label className="text-xs font-medium text-muted-foreground" htmlFor={`batch-field-${field.id}`}>{definition.display_name}</label>{definition.description ? <p className="mt-0.5 text-xs text-muted-foreground">{definition.description}</p> : null}<div className="mt-1.5"><BatchFieldInput id={`batch-field-${field.id}`} definition={definition} values={drafts[field.metadata_definition_id] ?? []} disabled={disabled || saving} onChange={(values) => setDrafts((current) => ({ ...current, [field.metadata_definition_id]: values }))} /></div>{visible.map((value) => <div key={value.review_batch_run_id} className="mt-2 rounded-md border bg-muted/35 px-2.5 py-2 text-xs"><span className="font-medium">{value.actor_user.display_name}</span><span className="text-muted-foreground"> · {value.values.map(String).join(", ") || "Not set"}</span></div>)}</div>;
      })}</div></section>)}
      {!fields.length ? <p className="p-4 text-sm text-muted-foreground">This batch has no coding groups. Saving will still mark the document reviewed.</p> : null}
    </div>
    <div className="flex shrink-0 items-center justify-between gap-2 border-t p-3"><Button type="button" variant="ghost" size="sm" disabled={disabled || saving} onClick={onSkip}><SkipForward />Skip</Button><div className="flex items-center gap-2"><span className="text-xs text-muted-foreground">{dirty ? "Unsaved changes" : coding.review_status === "COMPLETED" ? "Saved" : "Ready"}</span><Button type="submit" size="sm" disabled={disabled || saving}><Save />{saving ? "Saving…" : "Save & next"}</Button></div></div>
  </form>;
}

function BatchFieldInput({ id, definition, values, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  if (definition.cardinality === "MULTIPLE") return <MultiValueInput id={id} definition={definition} values={values} disabled={disabled} onChange={onChange} />;
  return <SingleValueInput id={id} definition={definition} value={values[0] ?? ""} disabled={disabled} onChange={(value) => onChange([value])} />;
}

function MultiValueInput({ id, definition, values, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  const [entry, setEntry] = useState("");
  const add = () => {
    if (!entry.trim() || values.includes(entry)) return;
    onChange([...values, entry]);
    setEntry("");
  };
  return <div><div className="flex flex-wrap gap-1.5">{values.map((value) => <Badge key={value} variant="outline"><span>{value}</span><button type="button" disabled={disabled} className="ml-1" aria-label={`Remove ${value}`} onClick={() => onChange(values.filter((item) => item !== value))}><X className="size-3" /></button></Badge>)}</div><div className="mt-2 flex gap-2"><SingleValueInput id={id} definition={definition} value={entry} disabled={disabled} onChange={setEntry} /><Button type="button" variant="outline" size="sm" disabled={disabled || !entry.trim()} onClick={add}>Add</Button></div></div>;
}

function SingleValueInput({ id, definition, value, disabled, onChange }: {
  id: string;
  definition: SnapshotDefinition;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  if (definition.type === "ENUM") return <Select value={value || "__empty"} disabled={disabled} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id}><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem>{definition.allowed_values.filter((option) => option.active !== false).map((option) => <SelectItem key={option.key} value={option.key}>{option.label}</SelectItem>)}</SelectContent></Select>;
  if (definition.type === "BOOLEAN") return <Select value={value || "__empty"} disabled={disabled} onValueChange={(next) => onChange(next === "__empty" ? "" : next)}><SelectTrigger id={id}><SelectValue placeholder="Select a value" /></SelectTrigger><SelectContent><SelectItem value="__empty">Not set</SelectItem><SelectItem value="true">Yes</SelectItem><SelectItem value="false">No</SelectItem></SelectContent></Select>;
  if (definition.type === "LONG_TEXT" || definition.type === "JSON") return <Textarea id={id} disabled={disabled} className="min-h-20" value={value} onChange={(event) => onChange(event.target.value)} />;
  const type = definition.type === "INTEGER" || definition.type === "DECIMAL" ? "number" : definition.type === "DATE" ? "date" : definition.type === "DATETIME" ? "datetime-local" : "text";
  return <Input id={id} disabled={disabled} type={type} step={definition.type === "DECIMAL" ? "any" : undefined} value={value} onChange={(event) => onChange(event.target.value)} />;
}
