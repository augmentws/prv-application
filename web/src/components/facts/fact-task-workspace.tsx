"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, BookOpenCheck, Check, FileJson, ListTree, LoaderCircle, Play, Plus, RotateCcw, Save, Sparkles } from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";
import { toast } from "sonner";

import { JobOutcomesDialog } from "@/components/job-outcomes-dialog";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  FactDevelopmentRunEstimateRead,
  FactDevelopmentRunRead,
  FactDevelopmentSpecification,
  FactIssueMembershipPreviewRead,
  JobExecutionRead,
  MatterAnalysisTaskRead,
  MatterAnalysisTaskVersionRead,
  MatterDefinitionRead,
  ReviewBatchComparisonOptionsRead,
  ReviewBatchRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { FACT_GUIDANCE_HELP } from "@/lib/facts";
import { formatDate } from "@/lib/format";

type CodingSource = "MATTER_METADATA" | "REVIEWER_CODING";
const TERMINAL = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"];

function friendly(value: string) {
  return value.toLowerCase().replaceAll("_", " ");
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function numberOf(value: unknown) {
  return typeof value === "number" ? value : 0;
}

/** The Analysis-tab workspace for one version of a fact-development task. */
export function FactTaskWorkspace({ clientId, matterId, task, version, onTaskUpdated }: {
  clientId?: string;
  matterId: string;
  task: MatterAnalysisTaskRead;
  version: MatterAnalysisTaskVersionRead;
  onTaskUpdated: (task: MatterAnalysisTaskRead) => void;
}) {
  const queryClient = useQueryClient();
  const isCurrent = version.version === task.current_version && task.status === "ACTIVE";
  const editable = isCurrent && version.status === "DRAFT";
  const [definition, setDefinition] = useState(version.definition_markdown);
  const [specificationJson, setSpecificationJson] = useState(version.fact_development_specification ? JSON.stringify(version.fact_development_specification, null, 2) : "");
  const [editingJson, setEditingJson] = useState(false);
  const guidance = useQuery({
    queryKey: ["review-guidance", matterId],
    queryFn: () => coreApi<MatterDefinitionRead[]>(`/v1/matters/${matterId}/guidance`),
  });
  const pinnedGuidance = guidance.data?.find((item) => item.id === version.review_guidance_id);
  const definitionChanged = definition.trim() !== version.definition_markdown.trim();

  function versionUpdated(updated: MatterAnalysisTaskRead, message: string) {
    void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] });
    onTaskUpdated(updated);
    toast.success(message);
  }

  const newDraft = useMutation({
    mutationFn: (overrides: { definition_markdown?: string; review_guidance_revision?: number }) =>
      coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`, {
        method: "POST",
        body: JSON.stringify({ definition_markdown: definition.trim(), based_on_version: task.current_version, ...overrides }),
      }),
    onSuccess: (updated, overrides) => versionUpdated(updated, overrides.review_guidance_revision ? `A new draft pins guidance revision ${overrides.review_guidance_revision}. Generate its issue catalog.` : "A new draft task version was created."),
    onError: (error) => toast.error(error.message),
  });
  const compile = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/compile`, { method: "POST" }),
    onSuccess: (updated) => versionUpdated(updated, "Issue catalog generation was queued."),
    onError: (error) => toast.error(error.message),
  });
  const saveSpecification = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/fact-specification`, {
      method: "PUT",
      body: JSON.stringify({ fact_development_specification: JSON.parse(specificationJson) as FactDevelopmentSpecification }),
    }),
    onSuccess: (updated) => {
      setEditingJson(false);
      versionUpdated(updated, "The issue catalog was validated and saved.");
    },
    onError: (error) => toast.error(error instanceof SyntaxError ? `Invalid JSON: ${error.message}` : error.message),
  });
  const publish = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/publish`, { method: "POST" }),
    onSuccess: (updated) => versionUpdated(updated, `Version ${version.version} was published.`),
    onError: (error) => toast.error(error.message),
  });

  const specification = version.fact_development_specification;
  const report = recordValue(version.validation_report);
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  const failure = typeof report.error === "string" ? report.error : Array.isArray(report.errors) && report.errors.length ? String(recordValue(report.errors[0]).message ?? "") : "";

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2"><BookOpenCheck className="size-4 text-primary" />Review Guidance</CardTitle>
          <p className="text-sm text-muted-foreground">Issues come from one published guidance revision, pinned to this version so later guidance edits never change it.</p>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4 text-sm">
          <div>
            <p className="font-medium">{pinnedGuidance?.name ?? (guidance.isPending ? "Loading…" : "Guidance profile")}</p>
            <p className="text-muted-foreground">Pinned revision {version.review_guidance_revision ?? "—"}{pinnedGuidance?.published_revision ? ` · published revision ${pinnedGuidance.published_revision}` : ""}</p>
          </div>
          {version.review_guidance_pin_stale ? (
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="outline" className="gap-1 text-warning"><AlertTriangle className="size-3" aria-hidden="true" />Newer guidance is published</Badge>
              {isCurrent && pinnedGuidance?.published_revision ? (
                <Button size="sm" variant="outline" disabled={newDraft.isPending} onClick={() => newDraft.mutate({ definition_markdown: version.definition_markdown, review_guidance_revision: pinnedGuidance.published_revision! })}>
                  <RotateCcw aria-hidden="true" />Adopt revision {pinnedGuidance.published_revision}
                </Button>
              ) : null}
            </div>
          ) : <Badge variant="outline">Current</Badge>}
        </CardContent>
      </Card>

      <div className="grid gap-4 2xl:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2"><FileJson className="size-4 text-primary" />Fact guidance</CardTitle>
            <p className="text-sm text-muted-foreground">{FACT_GUIDANCE_HELP} Editing creates a new draft version.</p>
          </CardHeader>
          <CardContent className="space-y-3 p-4">
            <Textarea aria-label="Fact guidance" className="min-h-72 resize-y font-mono text-sm leading-6" value={definition} onChange={(event) => setDefinition(event.target.value)} readOnly={!isCurrent} />
            {isCurrent ? (
              <div className="flex justify-end">
                <Button variant="outline" disabled={!definition.trim() || newDraft.isPending || (!definitionChanged && version.status === "DRAFT")} onClick={() => newDraft.mutate({})}>
                  {newDraft.isPending ? <LoaderCircle className="animate-spin" /> : definitionChanged ? <Save /> : <Plus />}
                  {definitionChanged ? "Save as new draft" : "Create draft revision"}
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader className="border-b">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <CardTitle className="flex items-center gap-2"><ListTree className="size-4 text-primary" />Issue catalog</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">Each issue says which coding marks a document as relevant and how to build facts for it.</p>
              </div>
              <StatusBadge status={version.compilation_status} />
            </div>
          </CardHeader>
          <CardContent className="space-y-3 p-4">
            {version.compilation_status === "GENERATING" ? (
              <div role="status" className="flex items-start gap-3 rounded-lg border bg-muted/20 p-4 text-sm">
                <LoaderCircle className="mt-0.5 size-5 shrink-0 animate-spin text-primary" />
                <div>
                  <p className="font-medium">Generating and validating the issue catalog</p>
                  <p className="mt-1 text-muted-foreground">{String(recordValue(version.compilation_progress).message ?? "The compiler is working.")}</p>
                </div>
              </div>
            ) : editingJson ? (
              <Textarea aria-label="Issue catalog JSON" className="min-h-72 resize-y font-mono text-xs leading-5" value={specificationJson} onChange={(event) => setSpecificationJson(event.target.value)} />
            ) : specification ? <IssueCatalog specification={specification} /> : <p className="text-sm text-muted-foreground">Generate the catalog from the pinned guidance and the fact guidance.</p>}
            {version.compilation_status === "FAILED" && failure ? <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{failure}</p> : null}
            {warnings.length ? <p className="text-xs text-warning">{warnings.length} compiler {warnings.length === 1 ? "warning" : "warnings"}: {warnings.map((item) => String(recordValue(item).message ?? item)).join(" · ")}</p> : null}
            {editable && version.compilation_status !== "GENERATING" ? (
              <div className="flex flex-wrap justify-end gap-2">
                {editingJson ? (
                  <>
                    <Button variant="ghost" onClick={() => setEditingJson(false)}>Cancel</Button>
                    <Button variant="outline" disabled={!specificationJson.trim() || saveSpecification.isPending} onClick={() => saveSpecification.mutate()}><Check />Validate & save</Button>
                  </>
                ) : (
                  <>
                    <Button variant="outline" disabled={compile.isPending || definitionChanged} onClick={() => compile.mutate()}>{compile.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}{specification ? "Regenerate" : "Generate"}</Button>
                    <Button variant="outline" onClick={() => { setSpecificationJson(specification ? JSON.stringify(specification, null, 2) : ""); setEditingJson(true); }}><FileJson />Edit JSON</Button>
                    <Button disabled={version.compilation_status !== "READY" || publish.isPending} onClick={() => publish.mutate()}>{publish.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}Publish version</Button>
                  </>
                )}
              </div>
            ) : null}
            {editable && definitionChanged ? <p className="text-xs text-muted-foreground">Save the changed fact guidance as a new draft before generating its catalog.</p> : null}
          </CardContent>
        </Card>
      </div>

      {specification ? <IssueMatchPreview matterId={matterId} task={task} version={version} /> : null}
      {version.status === "PUBLISHED" && task.published_version === version.version
        ? <FactRunPanel clientId={clientId} matterId={matterId} task={task} />
        : <Card className="p-4 text-sm text-muted-foreground">Publish this version to run fact development with it.</Card>}
    </div>
  );
}

function IssueCatalog({ specification }: { specification: FactDevelopmentSpecification }) {
  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted-foreground">{specification.summary}</p>
      <ul className="divide-y rounded-lg border">
        {specification.issues.map((issue) => (
          <li key={issue.key} className="space-y-1 p-3">
            <p><span className="font-medium">{issue.name}</span> <code className="text-xs text-muted-foreground">{issue.key}</code></p>
            <p className="text-xs text-muted-foreground">
              Coded by {issue.coding_mappings.map((mapping) => `${mapping.metadata_definition_key} = ${String(mapping.value)}${mapping.match_type === "ENUM_VALUE" && mapping.include_descendants !== false ? " (and children)" : ""}`).join("; ")}
            </p>
            <p className="text-xs">{issue.fact_guidance}</p>
          </li>
        ))}
      </ul>
    </div>
  );
}

function useBatchSelection(matterId: string) {
  // Each form using this selection needs its own ids so labels bind to the right controls.
  const idPrefix = useId();
  const [batchId, setBatchId] = useState("");
  const [codingSource, setCodingSource] = useState<CodingSource>("MATTER_METADATA");
  const [reviewerCodingId, setReviewerCodingId] = useState("");
  const batches = useQuery({ queryKey: ["review-batches", matterId], queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`) });
  const reviewers = useQuery({
    queryKey: ["review-batch-comparison-options", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchComparisonOptionsRead>(`/v1/matters/${matterId}/review-batches/${batchId}/comparison-options`),
    enabled: Boolean(batchId) && codingSource === "REVIEWER_CODING",
  });
  const reviewerSources = (reviewers.data?.sources ?? []).filter((source) => source.source_type === "REVIEWER");
  const ready = (batches.data ?? []).filter((batch) => batch.status === "READY");
  const complete = Boolean(batchId) && (codingSource === "MATTER_METADATA" || Boolean(reviewerCodingId));
  const payload = { review_batch_id: batchId, coding_source: codingSource, reviewer_coding_id: codingSource === "REVIEWER_CODING" ? reviewerCodingId : null };
  const fields = (
    <div className="grid gap-3 lg:grid-cols-3">
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-batch`}>Ready batch</Label>
        <Select value={batchId} onValueChange={(value) => { setBatchId(value); setReviewerCodingId(""); }}>
          <SelectTrigger id={`${idPrefix}-batch`}><SelectValue placeholder={batches.isPending ? "Loading batches…" : "Select a batch"} /></SelectTrigger>
          <SelectContent>{ready.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} docs</SelectItem>)}</SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={`${idPrefix}-coding-source`}>Issue coding from</Label>
        <Select value={codingSource} onValueChange={(value) => { setCodingSource(value as CodingSource); setReviewerCodingId(""); }}>
          <SelectTrigger id={`${idPrefix}-coding-source`}><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="MATTER_METADATA">Current matter coding</SelectItem>
            <SelectItem value="REVIEWER_CODING">One reviewer&apos;s batch coding</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {codingSource === "REVIEWER_CODING" ? (
        <div className="space-y-1.5">
          <Label htmlFor={`${idPrefix}-reviewer`}>Reviewer</Label>
          <Select value={reviewerCodingId} onValueChange={setReviewerCodingId} disabled={!batchId}>
            <SelectTrigger id={`${idPrefix}-reviewer`}><SelectValue placeholder={!batchId ? "Select a batch first" : reviewers.isPending ? "Loading reviewers…" : "Select a reviewer"} /></SelectTrigger>
            <SelectContent>{reviewerSources.map((source) => <SelectItem key={source.id} value={source.id}>{source.label}</SelectItem>)}</SelectContent>
          </Select>
        </div>
      ) : null}
    </div>
  );
  return { fields, complete, payload, batches, ready };
}

function IssueMatchPreview({ matterId, task, version }: { matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead }) {
  const selection = useBatchSelection(matterId);
  const preview = useMutation({
    mutationFn: () => coreApi<FactIssueMembershipPreviewRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/issue-membership-preview`, {
      method: "POST",
      body: JSON.stringify(selection.payload),
    }),
    onError: (error) => toast.error(error.message),
  });
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="text-base">Check issue matches</CardTitle>
        <p className="text-sm text-muted-foreground">Count how many documents in a batch are coded to each issue, without starting a run.</p>
      </CardHeader>
      <CardContent className="space-y-3 p-4">
        {selection.fields}
        <div className="flex justify-end"><Button variant="outline" disabled={!selection.complete || preview.isPending} onClick={() => preview.mutate()}>{preview.isPending ? <LoaderCircle className="animate-spin" /> : null}Count matches</Button></div>
        {preview.data ? (
          <div className="text-sm">
            <p className="mb-2">{preview.data.matched_document_count.toLocaleString()} of {preview.data.batch_document_count.toLocaleString()} documents match at least one issue.</p>
            <ul className="grid gap-1 sm:grid-cols-2">{preview.data.issues.map((issue) => <li key={issue.issue_key} className="flex justify-between rounded-md border px-3 py-1.5"><span>{issue.name}</span><span className="tabular-nums">{issue.document_count.toLocaleString()}</span></li>)}</ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function FactRunPanel({ clientId, matterId, task }: { clientId?: string; matterId: string; task: MatterAnalysisTaskRead }) {
  const queryClient = useQueryClient();
  const selection = useBatchSelection(matterId);
  const runsKey = ["fact-development-runs", matterId, task.id];
  const runs = useQuery({
    queryKey: runsKey,
    queryFn: () => coreApi<FactDevelopmentRunRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs`),
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL.includes(run.status)) ? 3000 : false,
  });
  const estimate = useMutation({
    mutationFn: () => coreApi<FactDevelopmentRunEstimateRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs/estimate`, {
      method: "POST",
      body: JSON.stringify(selection.payload),
    }),
    onError: (error) => toast.error(error.message),
  });
  const launch = useMutation({
    mutationFn: () => coreApi<FactDevelopmentRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs`, {
      method: "POST",
      body: JSON.stringify({ ...selection.payload, idempotency_key: crypto.randomUUID() }),
    }),
    onSuccess: () => {
      estimate.reset();
      void queryClient.invalidateQueries({ queryKey: runsKey });
      toast.success("Fact development was queued.");
    },
    onError: (error) => toast.error(error.message),
  });
  const cancel = useMutation({
    mutationFn: (runId: string) => coreApi<FactDevelopmentRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/fact-runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: runsKey }); toast.success("The run was canceled."); },
    onError: (error) => toast.error(error.message),
  });
  const retry = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/FACT_DEVELOPMENT_ANALYSIS_TASK/${runId}/retry`, { method: "POST" }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: runsKey }); toast.success("The run will resume from where it stopped."); },
    onError: (error) => toast.error(error.message),
  });
  const current = estimate.data;
  const estimateMatches = current && current.review_batch_id === selection.payload.review_batch_id && current.coding_source === selection.payload.coding_source;

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex items-center gap-2"><Play className="size-4 text-primary" />Run fact development</CardTitle>
        <p className="text-sm text-muted-foreground">Agents read each coded document in date order and propose facts for review. Documents an earlier run already read are reused, not read again.</p>
      </CardHeader>
      <CardContent className="space-y-4 p-4">
        {selection.fields}
        <div className="flex justify-end"><Button variant="outline" disabled={!selection.complete || estimate.isPending} onClick={() => estimate.mutate()}>{estimate.isPending ? <LoaderCircle className="animate-spin" /> : null}Estimate</Button></div>
        {current && estimateMatches ? (
          <div className="space-y-3 rounded-lg border bg-muted/20 p-4 text-sm" aria-label="Run estimate">
            <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
              <dt className="text-muted-foreground">Documents in the batch</dt><dd className="tabular-nums">{current.batch_document_count.toLocaleString()}</dd>
              <dt className="text-muted-foreground">Coded to an issue</dt><dd className="tabular-nums">{current.matched_document_count.toLocaleString()}</dd>
              <dt className="text-muted-foreground">Already read by an earlier run</dt><dd className="tabular-nums">{current.previously_extracted_count.toLocaleString()}</dd>
              <dt className="text-muted-foreground">Without usable text</dt><dd className="tabular-nums">{current.missing_text_document_count.toLocaleString()}</dd>
              <dt className="font-medium">Documents to read</dt><dd className="font-medium tabular-nums">{current.documents_to_extract.toLocaleString()}</dd>
              <dt className="text-muted-foreground">Approximate model tokens</dt><dd className="tabular-nums">{current.estimated_input_tokens.toLocaleString()} in · {current.estimated_output_tokens.toLocaleString()} out ({current.extraction_model})</dd>
            </dl>
            <p className="text-xs text-muted-foreground">Token figures are planning estimates, not billing figures.</p>
            <div className="flex justify-end">
              <Button disabled={launch.isPending || current.documents_to_extract === 0} onClick={() => launch.mutate()}>
                {launch.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}
                {current.documents_to_extract === 0 ? "Nothing to read" : `Launch run for ${current.documents_to_extract.toLocaleString()} documents`}
              </Button>
            </div>
          </div>
        ) : null}
        {runs.isPending ? <TableLoading /> : runs.error ? <QueryError message={runs.error.message} /> : runs.data.length ? (
          <div className="space-y-2">
            <h4 className="font-semibold">Runs</h4>
            {runs.data.map((run) => (
              <FactRunCard
                key={run.id}
                clientId={clientId}
                matterId={matterId}
                run={run}
                batchName={selection.batches.data?.find((batch) => batch.id === run.scope_id)?.name ?? "Review batch"}
                busy={cancel.isPending || retry.isPending}
                onCancel={() => cancel.mutate(run.id)}
                onRetry={() => retry.mutate(run.id)}
              />
            ))}
          </div>
        ) : <p className="text-sm text-muted-foreground">No runs yet.</p>}
      </CardContent>
    </Card>
  );
}

function FactRunCard({ clientId, matterId, run, batchName, busy, onCancel, onRetry }: {
  clientId?: string;
  matterId: string;
  run: FactDevelopmentRunRead;
  batchName: string;
  busy: boolean;
  onCancel: () => void;
  onRetry: () => void;
}) {
  const execution = run.execution;
  const active = !TERMINAL.includes(run.status);
  const percent = run.progress_total ? Math.round((run.progress_completed / run.progress_total) * 100) : 0;
  const report = recordValue(run.report);
  const counts = recordValue(report.counts);
  const qc = recordValue(report.qc);
  const canCancel = execution?.available_actions.includes("CANCEL") ?? active;
  const canRetry = execution?.available_actions.includes("RETRY") ?? (run.status === "FAILED" && run.failure_retriable);
  return (
    <div className="rounded-lg border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="text-sm">
          <p className="font-medium">{batchName}</p>
          <p className="mt-1 text-xs text-muted-foreground">{friendly(run.current_stage || run.status)} · {run.progress_completed.toLocaleString()} of {run.progress_total.toLocaleString()} documents · {formatDate(run.created_at)}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={run.status} />
          {execution?.cancel_requested_at && active ? <Badge variant="outline">Cancellation requested</Badge> : null}
          {canCancel ? <Button size="sm" variant="outline" disabled={busy} onClick={onCancel}><Ban />Cancel</Button> : null}
          {canRetry ? <Button size="sm" variant="outline" disabled={busy} onClick={onRetry}><RotateCcw />Resume</Button> : null}
          {!active ? <JobOutcomesDialog matterId={matterId} jobType="FACT_DEVELOPMENT_ANALYSIS_TASK" jobId={run.id} jobName={`Fact development · ${batchName}`} /> : null}
        </div>
      </div>
      {active ? (
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-label={`${run.progress_completed} of ${run.progress_total} documents processed`}>
          <div className="h-full bg-primary transition-[width]" style={{ width: `${Math.min(100, percent)}%` }} />
        </div>
      ) : null}
      {Object.keys(counts).length ? (
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <Badge variant="outline">{numberOf(counts.facts_created).toLocaleString()} facts proposed</Badge>
          <Badge variant="outline">{numberOf(counts.merged_into_existing).toLocaleString()} merged into existing facts</Badge>
          <Badge variant="outline">{numberOf(counts.suggestions_created).toLocaleString()} suggestions</Badge>
          <Badge variant="outline">{numberOf(counts.reused).toLocaleString()} documents reused</Badge>
          {numberOf(qc.entities_awaiting_confirmation) ? <Badge variant="outline">{numberOf(qc.entities_awaiting_confirmation).toLocaleString()} entities to review</Badge> : null}
          {numberOf(counts.invalid_operations) ? <Badge variant="outline" className="text-warning">{numberOf(counts.invalid_operations).toLocaleString()} invalid agent operations</Badge> : null}
          {numberOf(counts.duplicate_check_unavailable) ? <Badge variant="outline" className="text-warning">{numberOf(counts.duplicate_check_unavailable).toLocaleString()} duplicate checks unavailable</Badge> : null}
        </div>
      ) : null}
      {run.failure_summary ? <p className="mt-2 text-sm text-destructive">{run.failure_summary}</p> : null}
      {clientId && !active && numberOf(counts.facts_created) ? (
        <p className="mt-2 text-sm"><Link className="font-medium text-primary hover:underline" href={`/app/clients/${clientId}/matters/${matterId}?tab=facts&ff_status=WORKING`}>Review working facts</Link></p>
      ) : null}
    </div>
  );
}
