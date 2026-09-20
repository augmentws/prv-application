"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, LoaderCircle, Play, Search, Workflow } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { QueryError } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  MatterDefinitionAssessmentCreate,
  MatterDefinitionAssessmentQueryRead,
  MatterDefinitionAssessmentQuestionRead,
  MatterDefinitionAssessmentRead,
  MatterDefinitionRevisionRead,
  WorkflowExecutionRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"]);
const STAGES = ["PLANNING", "RETRIEVING", "BUILDING_BATCH", "SUMMARIZING", "SYNTHESIZING", "COMPLETED"];

function objectValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function MatterDefinitionAssessmentPanel({ matterId, revisions, publishedRevision }: { matterId: string; revisions: MatterDefinitionRevisionRead[]; publishedRevision: number | null }) {
  const queryClient = useQueryClient();
  const [launchOpen, setLaunchOpen] = useState(false);
  const [selectedId, setSelectedId] = useState("");
  const [revision, setRevision] = useState(String(revisions[0]?.revision ?? 1));
  const [maximum, setMaximum] = useState("500");
  const [controlSize, setControlSize] = useState("0");
  const [warningAcknowledged, setWarningAcknowledged] = useState(false);

  const runs = useQuery({
    queryKey: ["definition-assessments", matterId],
    queryFn: () => coreApi<MatterDefinitionAssessmentRead[]>(`/v1/matters/${matterId}/definition-assessments`),
    refetchInterval: (query) => query.state.data?.some((run) => !TERMINAL.has(run.status)) ? 2000 : false,
  });
  const effectiveId = selectedId || runs.data?.[0]?.id || "";
  const selected = runs.data?.find((run) => run.id === effectiveId);
  const queries = useQuery({
    queryKey: ["definition-assessment-queries", matterId, effectiveId],
    queryFn: () => coreApi<MatterDefinitionAssessmentQueryRead[]>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/queries`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });
  const questions = useQuery({
    queryKey: ["definition-assessment-questions", matterId, effectiveId],
    queryFn: () => coreApi<MatterDefinitionAssessmentQuestionRead[]>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/questions`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });
  const execution = useQuery({
    queryKey: ["definition-assessment-execution", matterId, effectiveId],
    queryFn: () => coreApi<WorkflowExecutionRead>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/execution`),
    enabled: Boolean(effectiveId),
    refetchInterval: selected && !TERMINAL.has(selected.status) ? 2500 : false,
  });

  const launch = useMutation({
    mutationFn: (payload: MatterDefinitionAssessmentCreate) => coreApi<MatterDefinitionAssessmentRead>(`/v1/matters/${matterId}/definition-assessments`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["definition-assessments", matterId] });
      setSelectedId(created.id);
      setLaunchOpen(false);
      toast.success("Corpus assessment started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The assessment could not be started."),
  });
  const updateQuestion = useMutation({
    mutationFn: ({ id, status, answer }: { id: string; status: "ANSWERED" | "DISMISSED"; answer?: string }) => coreApi<MatterDefinitionAssessmentQuestionRead>(`/v1/matters/${matterId}/definition-assessments/${effectiveId}/questions/${id}`, { method: "PUT", body: JSON.stringify({ status, answer: answer || null }) }),
    onSuccess: async () => queryClient.invalidateQueries({ queryKey: ["definition-assessment-questions", matterId, effectiveId] }),
    onError: (error) => toast.error(error instanceof Error ? error.message : "The clarification could not be updated."),
  });

  const requested = Math.max(1, Number(maximum) || 500);
  const report = objectValue(selected?.synthesis_result);
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const stageIndex = selected ? Math.max(0, STAGES.indexOf(selected.status === "COMPLETED_WITH_ERRORS" ? "COMPLETED" : selected.status)) : 0;
  const definitionState = publishedRevision === Number(revision) ? "Published" : "Draft";
  const usage = execution.data;
  const cacheRatio = usage?.input_tokens ? Math.round((usage.cached_input_tokens / usage.input_tokens) * 100) : 0;

  const submitLaunch = () => {
    launch.mutate({
      revision: Number(revision),
      maximum_document_count: requested,
      control_sample_size: Math.max(0, Number(controlSize) || 0),
      acknowledge_large_run_warning: requested > 1000 && warningAcknowledged,
    });
  };

  return <Card className="overflow-hidden">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
      <div><div className="flex items-center gap-2"><Search className="size-4 text-primary" /><h2 className="font-semibold">Assess against corpus</h2></div><p className="mt-1 text-sm text-muted-foreground">Build a frozen diagnostic batch, analyze each document, and surface guidance gaps.</p></div>
      <Button type="button" onClick={() => setLaunchOpen(true)} disabled={!revisions.length}><Play />New assessment</Button>
    </div>
    {runs.error ? <div className="p-5"><QueryError message={runs.error.message} /></div> : runs.isPending ? <p className="p-5 text-sm text-muted-foreground">Loading assessment history…</p> : !runs.data?.length ? <p className="p-5 text-sm text-muted-foreground">No corpus assessments have been run for this Matter Definition.</p> : <div className="space-y-5 p-5">
      <div className="flex flex-wrap items-center gap-3">
        <Select value={effectiveId} onValueChange={setSelectedId}><SelectTrigger className="max-w-md"><SelectValue /></SelectTrigger><SelectContent>{runs.data.map((run) => <SelectItem key={run.id} value={run.id}>{formatDate(run.created_at)} · {run.status.toLowerCase().replaceAll("_", " ")}</SelectItem>)}</SelectContent></Select>
        {selected ? <StatusBadge status={selected.status} /> : null}
        {selected?.review_batch_id ? <Button asChild size="sm" variant="outline"><Link href={`/review/matters/${matterId}?batch=${selected.review_batch_id}`}><ExternalLink />Open batch</Link></Button> : null}
      </div>

      {selected ? <>
        <div className="grid gap-2 sm:grid-cols-3 xl:grid-cols-6">{STAGES.map((stage, index) => <div key={stage} className={`rounded-md border px-2.5 py-2 text-center text-[11px] font-semibold ${index <= stageIndex ? "border-primary/40 bg-primary/8 text-primary" : "text-muted-foreground"}`}>{stage.toLowerCase().replaceAll("_", " ")}</div>)}</div>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Metric label="Selected" value={selected.selected_count} />
          <Metric label="Summarized" value={selected.summarized_count} />
          <Metric label="Skipped / failed" value={`${selected.skipped_count} / ${selected.failed_count}`} />
          <Metric label="Estimated input" value={selected.estimated_input_tokens?.toLocaleString() ?? "Pending"} />
          <Metric label="Cache reads" value={`${cacheRatio}%`} />
        </div>
        {selected.coverage_snapshot ? <div className={`rounded-lg border p-3 text-sm ${objectValue(selected.coverage_snapshot).status === "INSUFFICIENT" ? "border-conflict/40 bg-conflict/8" : "bg-muted/30"}`}><p className="font-semibold">Coverage: {String(objectValue(selected.coverage_snapshot).status ?? "pending").toLowerCase()}</p><p className="mt-1 text-xs text-muted-foreground">{selected.summarized_count} successful of {selected.selected_count} selected · {selected.partial_coverage_count} partial · {selected.invalid_result_count} invalid</p></div> : null}

        {queries.data?.length ? <section><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Retrieval plan</h3><div className="space-y-2">{queries.data.map((item) => <div key={item.id} className="rounded-lg border p-3"><div className="flex items-center justify-between gap-3"><p className="text-sm font-semibold">{item.criterion_label}</p><Badge variant="outline">{item.result_count} results</Badge></div><p className="mt-1 text-xs text-muted-foreground">{item.rationale}</p></div>)}</div></section> : null}

        {report.narrative ? <section className="rounded-lg border bg-muted/20 p-4"><h3 className="font-semibold">Assessment report</h3><p className="mt-2 whitespace-pre-wrap text-sm leading-6">{String(report.narrative)}</p>{findings.length ? <div className="mt-3 space-y-2">{findings.map((finding, index) => <pre key={index} className="whitespace-pre-wrap rounded-md bg-background p-2 text-xs">{typeof finding === "string" ? finding : JSON.stringify(finding, null, 2)}</pre>)}</div> : null}</section> : null}

        {questions.data?.length ? <section><h3 className="mb-2 text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Clarification questions</h3><div className="space-y-3">{questions.data.map((question) => <ClarificationQuestion key={question.id} question={question} disabled={updateQuestion.isPending} onUpdate={(status, answer) => updateQuestion.mutate({ id: question.id, status, answer })} />)}</div></section> : null}

        {usage ? <details className="rounded-lg border"><summary className="cursor-pointer p-3 text-sm font-semibold"><span className="inline-flex items-center gap-2"><Workflow className="size-4" />Workflow execution · {usage.request_count} model requests</span></summary><div className="border-t p-3"><div className="mb-3 grid gap-2 sm:grid-cols-4"><Metric label="Input tokens" value={usage.input_tokens.toLocaleString()} /><Metric label="Cached input" value={usage.cached_input_tokens.toLocaleString()} /><Metric label="Cache writes" value={usage.cache_write_tokens.toLocaleString()} /><Metric label="Output tokens" value={usage.output_tokens.toLocaleString()} /></div><ol className="space-y-2">{usage.steps.map((step) => <li key={step.id} className="flex items-center justify-between rounded-md bg-muted/30 px-3 py-2 text-xs"><span><strong>{step.ordinal}. {step.role_key.replaceAll("_", " ")}</strong> · {step.completed_count}/{step.total_count}</span><StatusBadge status={step.status} /></li>)}</ol><div className="mt-3 border-t pt-3"><p className="text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Skill runs & artifacts</p><p className="mt-1 text-xs text-muted-foreground">{usage.skill_runs.length} runs · {usage.skill_runs.filter((run) => run.output_artifact_id).length} immutable output artifacts · {usage.skill_runs.filter((run) => run.status === "FAILED").length} failures</p>{usage.skill_runs.filter((run) => run.output_artifact_id || run.error_message).length ? <div className="mt-2 max-h-40 space-y-1 overflow-auto">{usage.skill_runs.filter((run) => run.output_artifact_id || run.error_message).map((run) => <div key={run.id} className="rounded bg-muted/30 px-2 py-1.5 font-mono text-[10px]"><span>{run.scope_type.toLowerCase()}</span>{run.output_artifact_id ? <span className="block truncate text-muted-foreground" title={run.output_artifact_id}>artifact {run.output_artifact_id}</span> : null}{run.error_message ? <span className="block text-destructive">{run.error_message}</span> : null}</div>)}</div> : null}</div></div></details> : null}
      </> : null}
    </div>}

    <Dialog open={launchOpen} onOpenChange={setLaunchOpen}><DialogContent><DialogHeader><DialogTitle>Assess Matter Definition against corpus</DialogTitle><DialogDescription>The selected revision and workflow bindings are pinned for the full run. The resulting batch and analyses remain isolated from matter-wide metadata.</DialogDescription></DialogHeader><div className="space-y-4">
      <div><label className="text-sm font-medium" htmlFor="assessment-revision">Definition revision</label><Select value={revision} onValueChange={setRevision}><SelectTrigger id="assessment-revision" className="mt-1"><SelectValue /></SelectTrigger><SelectContent>{revisions.map((item) => <SelectItem key={item.id} value={String(item.revision)}>Revision {item.revision}{publishedRevision === item.revision ? " · published" : " · draft"}</SelectItem>)}</SelectContent></Select><p className="mt-1 text-xs text-muted-foreground">{definitionState} revision</p></div>
      <div className="grid grid-cols-2 gap-3"><div><label className="text-sm font-medium" htmlFor="assessment-maximum">Maximum documents</label><Input id="assessment-maximum" className="mt-1" type="number" min={1} value={maximum} onChange={(event) => { setMaximum(event.target.value); setWarningAcknowledged(false); }} /></div><div><label className="text-sm font-medium" htmlFor="assessment-control">Control sample</label><Input id="assessment-control" className="mt-1" type="number" min={0} value={controlSize} onChange={(event) => setControlSize(event.target.value)} /></div></div>
      {requested > 1000 ? <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-conflict/40 bg-conflict/8 p-3 text-sm"><input className="mt-1 size-4 accent-primary" type="checkbox" checked={warningAcknowledged} onChange={(event) => setWarningAcknowledged(event.target.checked)} /><span><strong className="flex items-center gap-1"><AlertTriangle className="size-4" />Large analysis</strong>Continue with {requested.toLocaleString()} documents. This can use substantial model capacity.</span></label> : null}
    </div><DialogFooter><Button type="button" variant="outline" onClick={() => setLaunchOpen(false)}>Cancel</Button><Button type="button" disabled={launch.isPending || (requested > 1000 && !warningAcknowledged)} onClick={submitLaunch}>{launch.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}Start assessment</Button></DialogFooter></DialogContent></Dialog>
  </Card>;
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return <div className="rounded-lg border bg-card p-3"><p className="text-[11px] font-bold uppercase tracking-[0.08em] text-muted-foreground">{label}</p><p className="mt-1 text-lg font-semibold tabular-nums">{value}</p></div>;
}

function ClarificationQuestion({ question, disabled, onUpdate }: { question: MatterDefinitionAssessmentQuestionRead; disabled: boolean; onUpdate: (status: "ANSWERED" | "DISMISSED", answer?: string) => void }) {
  const [answer, setAnswer] = useState(question.answer ?? "");
  const evidenceCount = useMemo(() => question.evidence.length, [question.evidence]);
  return <div className="rounded-lg border p-3"><div className="flex flex-wrap items-start justify-between gap-2"><p className="max-w-3xl text-sm font-semibold">{question.question}</p><div className="flex gap-1"><Badge variant="outline">{question.priority.toLowerCase()}</Badge><StatusBadge status={question.status} /></div></div><p className="mt-1 text-xs leading-5 text-muted-foreground">{question.rationale}{evidenceCount ? ` · ${evidenceCount} evidence reference${evidenceCount === 1 ? "" : "s"}` : ""}</p>{question.status === "OPEN" ? <div className="mt-3 flex items-end gap-2"><Textarea className="min-h-20" placeholder="Answer this clarification…" value={answer} onChange={(event) => setAnswer(event.target.value)} /><div className="flex shrink-0 flex-col gap-2"><Button size="sm" disabled={disabled || !answer.trim()} onClick={() => onUpdate("ANSWERED", answer)}>Answer</Button><Button size="sm" variant="ghost" disabled={disabled} onClick={() => onUpdate("DISMISSED")}>Dismiss</Button></div></div> : question.answer ? <p className="mt-3 rounded-md bg-muted/40 p-2 text-sm">{question.answer}</p> : null}</div>;
}
