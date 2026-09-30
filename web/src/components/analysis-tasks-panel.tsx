"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, FileJson, FlaskConical, LoaderCircle, Play, Plus, Save, Sparkles } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  MatterAnalysisTaskCreate,
  MatterAnalysisTaskPlaygroundRunRead,
  MatterAnalysisTaskRead,
  MatterAnalysisTaskVersionRead,
  ReviewBatchDocumentRead,
  ReviewBatchRead,
  ReviewDecisionResultRead,
} from "@/generated/models";
import { ApiError, coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const TASK_TYPE_LABELS = {
  QUESTION_ANSWERING: "Question answering",
} as const;

type CreatableTaskType = keyof typeof TASK_TYPE_LABELS;

export function AnalysisTasksPanel({ matterId }: { matterId: string }) {
  const queryClient = useQueryClient();
  const [selectedTaskId, setSelectedTaskId] = useState<string>();
  const tasks = useQuery({
    queryKey: ["analysis-tasks", matterId],
    queryFn: () => coreApi<MatterAnalysisTaskRead[]>(`/v1/matters/${matterId}/analysis-tasks`),
    refetchInterval: (query) => query.state.data?.some((task) => task.version.compilation_status === "GENERATING") ? 1500 : false,
  });
  const selectedTask = tasks.data?.find((task) => task.id === selectedTaskId) ?? tasks.data?.[0];

  function updateTask(updated: MatterAnalysisTaskRead) {
    queryClient.setQueryData<MatterAnalysisTaskRead[]>(["analysis-tasks", matterId], (current) => {
      if (!current) return [updated];
      return current.some((task) => task.id === updated.id)
        ? current.map((task) => task.id === updated.id ? updated : task)
        : [...current, updated];
    });
    setSelectedTaskId(updated.id);
  }

  if (tasks.isPending) return <TableLoading />;
  if (tasks.error) return <QueryError message={tasks.error.message} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Analysis tasks</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">Version a reviewed task definition together with the Decision Specification that Jev executes. Playground results stay isolated from matter coding.</p>
        </div>
        <CreateAnalysisTaskDialog matterId={matterId} onCreated={updateTask} />
      </div>

      {!tasks.data.length ? (
        <Card className="p-8 text-center">
          <Sparkles className="mx-auto mb-3 size-7 text-primary" />
          <h3 className="font-semibold">No analysis tasks yet</h3>
          <p className="mx-auto mt-2 max-w-xl text-sm text-muted-foreground">Create a question-answering task. Its definition and generated Decision Specification will be governed as one version and evaluated through Jev.</p>
        </Card>
      ) : (
        <div className="grid min-h-[40rem] gap-4 xl:grid-cols-[18rem_minmax(0,1fr)]">
          <Card className="h-fit overflow-hidden p-2">
            <div className="space-y-1" role="list" aria-label="Analysis tasks">
              {tasks.data.map((task) => (
                <button
                  key={task.id}
                  type="button"
                  role="listitem"
                  onClick={() => setSelectedTaskId(task.id)}
                  className={`w-full rounded-lg p-3 text-left outline-none transition focus-visible:ring-2 focus-visible:ring-ring ${selectedTask?.id === task.id ? "bg-primary/10 text-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
                >
                  <span className="block truncate font-semibold">{task.name}</span>
                  <span className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                    <span>{TASK_TYPE_LABELS[task.task_type]}</span>
                    <span aria-hidden="true">·</span>
                    <span>v{task.current_version}</span>
                    {task.published_version ? <Badge variant="outline">published v{task.published_version}</Badge> : null}
                  </span>
                </button>
              ))}
            </div>
          </Card>
          {selectedTask ? <AnalysisTaskDetail key={selectedTask.id} matterId={matterId} task={selectedTask} onTaskUpdated={updateTask} /> : null}
        </div>
      )}
    </div>
  );
}

function CreateAnalysisTaskDialog({ matterId, onCreated }: { matterId: string; onCreated: (task: MatterAnalysisTaskRead) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [description, setDescription] = useState("");
  const [taskType, setTaskType] = useState<CreatableTaskType>("QUESTION_ANSWERING");
  const [definition, setDefinition] = useState("");
  const mutation = useMutation({
    mutationFn: (payload: MatterAnalysisTaskCreate) => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (created) => {
      onCreated(created);
      setOpen(false);
      setName("");
      setKey("");
      setDescription("");
      setDefinition("");
      toast.success(`${created.name} was created.`);
    },
  });

  function changeName(value: string) {
    setName(value);
    if (!key || key === taskKey(name)) setKey(taskKey(value));
  }

  function create() {
    mutation.mutate({
      key,
      name: name.trim(),
      description: description.trim() || null,
      task_type: taskType,
      definition_markdown: definition.trim(),
    });
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Plus />New analysis task</Button></DialogTrigger>
      <DialogContent className="max-w-3xl">
        <DialogHeader><DialogTitle>Create an analysis task</DialogTitle><DialogDescription>The task definition and generated Decision Specification will be versioned and published together.</DialogDescription></DialogHeader>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" htmlFor="analysis-task-name"><Input id="analysis-task-name" value={name} onChange={(event) => changeName(event.target.value)} placeholder="Contract relevance" /></Field>
          <Field label="Stable key" htmlFor="analysis-task-key"><Input id="analysis-task-key" value={key} onChange={(event) => setKey(taskKey(event.target.value))} placeholder="contract_relevance" /></Field>
          <Field label="Task type" htmlFor="analysis-task-type"><Select value={taskType} onValueChange={(value) => setTaskType(value as CreatableTaskType)}><SelectTrigger id="analysis-task-type"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(TASK_TYPE_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></Field>
          <Field label="Description" htmlFor="analysis-task-description"><Input id="analysis-task-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this task decides" /></Field>
        </div>
        <Field label="Task definition" htmlFor="analysis-task-definition"><Textarea id="analysis-task-definition" className="min-h-64 font-mono text-sm" value={definition} onChange={(event) => setDefinition(event.target.value)} placeholder="# Purpose&#10;&#10;Describe the decision criteria, inclusions, exclusions, and ambiguity rules…" /></Field>
        {mutation.error ? <p role="alert" className="text-sm text-destructive">{mutation.error.message}</p> : null}
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)} disabled={mutation.isPending}>Cancel</Button><Button onClick={create} disabled={mutation.isPending || !name.trim() || !key || !definition.trim()}>{mutation.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}{mutation.isPending ? "Creating…" : "Create task"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AnalysisTaskDetail({ matterId, task, onTaskUpdated }: { matterId: string; task: MatterAnalysisTaskRead; onTaskUpdated: (task: MatterAnalysisTaskRead) => void }) {
  const [selectedVersionNumber, setSelectedVersionNumber] = useState(task.current_version);
  const versions = useQuery({
    queryKey: ["analysis-task-versions", matterId, task.id],
    queryFn: () => coreApi<MatterAnalysisTaskVersionRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`),
    refetchInterval: (query) => query.state.data?.some((version) => version.compilation_status === "GENERATING") ? 1500 : false,
  });
  const selectedVersion = versions.data?.find((version) => version.version === selectedVersionNumber)
    ?? (task.version.version === selectedVersionNumber ? task.version : versions.data?.[0] ?? task.version);

  function handleTaskUpdated(updated: MatterAnalysisTaskRead) {
    onTaskUpdated(updated);
    setSelectedVersionNumber(updated.current_version);
  }

  return (
    <div className="min-w-0 space-y-4">
      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2"><h3 className="text-xl font-semibold">{task.name}</h3><StatusBadge status={task.status} /><Badge variant="outline">{TASK_TYPE_LABELS[task.task_type]}</Badge></div>
            <p className="mt-1 text-sm text-muted-foreground">{task.description || "No task description."}</p>
            <p className="mt-2 font-mono text-xs text-muted-foreground">{task.key} · {task.workflow_key}</p>
          </div>
          <div className="w-48 space-y-1.5"><Label htmlFor={`task-version-${task.id}`}>Version</Label><Select value={String(selectedVersion.version)} onValueChange={(value) => setSelectedVersionNumber(Number(value))}><SelectTrigger id={`task-version-${task.id}`}><SelectValue /></SelectTrigger><SelectContent>{(versions.data ?? [task.version]).map((version) => <SelectItem key={version.id} value={String(version.version)}>v{version.version} · {friendly(version.status)}</SelectItem>)}</SelectContent></Select></div>
        </div>
      </Card>
      {versions.isPending ? <TableLoading /> : versions.error ? <QueryError message={versions.error.message} /> : (
        <VersionWorkspace
          key={`${selectedVersion.id}:${selectedVersion.compilation_status}:${selectedVersion.specification_content_hash ?? "none"}`}
          matterId={matterId}
          task={task}
          version={selectedVersion}
          onTaskUpdated={handleTaskUpdated}
        />
      )}
    </div>
  );
}

function VersionWorkspace({ matterId, task, version, onTaskUpdated }: { matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead; onTaskUpdated: (task: MatterAnalysisTaskRead) => void }) {
  const queryClient = useQueryClient();
  const [definition, setDefinition] = useState(version.definition_markdown);
  const [specification, setSpecification] = useState(version.decision_specification ? JSON.stringify(version.decision_specification, null, 2) : "");
  const canCreateDraft = version.version === task.current_version && task.status === "ACTIVE";
  const editable = canCreateDraft && version.status === "DRAFT";
  const definitionEditable = canCreateDraft;
  const definitionChanged = definition.trim() !== version.definition_markdown.trim();
  const canClonePublishedVersion = definitionEditable && version.status === "PUBLISHED";
  const saveDefinition = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`, { method: "POST", body: JSON.stringify({ definition_markdown: definition.trim(), based_on_version: task.current_version }) }),
    onSuccess: (updated) => { void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] }); onTaskUpdated(updated); toast.success("A new draft task version was created."); },
    onError: (error) => toast.error(error.message),
  });
  const compile = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/compile`, { method: "POST" }),
    onSuccess: (updated) => { void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] }); onTaskUpdated(updated); toast.success("Decision Specification generation was queued."); },
    onError: (error) => toast.error(error.message),
  });
  const saveSpecification = useMutation({
    mutationFn: () => {
      const parsed = JSON.parse(specification) as Record<string, unknown>;
      return coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/specification`, {
        method: "PUT",
        body: JSON.stringify({
          decision_specification: parsed,
          input_contract: version.input_contract,
          output_contract: version.output_contract,
          evidence_policy: version.evidence_policy,
          routing_policy: version.routing_policy,
          compiler_skill_definition_version_id: version.compiler_skill_definition_version_id,
          compiler_skill_run_id: version.compiler_skill_run_id,
          compiler_model_configuration: version.compiler_model_configuration,
          validation_report: version.validation_report,
          source_provenance: { ...version.source_provenance, last_manual_specification_edit: new Date().toISOString() },
        }),
      });
    },
    onSuccess: (updated) => { void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] }); onTaskUpdated(updated); toast.success("The Decision Specification was validated and saved."); },
    onError: (error) => toast.error(error instanceof SyntaxError ? `Invalid JSON: ${error.message}` : error.message),
  });
  const publish = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/publish`, { method: "POST" }),
    onSuccess: (updated) => { void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] }); onTaskUpdated(updated); toast.success(`Version ${version.version} was published.`); },
    onError: (error) => toast.error(error.message),
  });

  return (
    <>
      <div className="grid gap-4 2xl:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader className="border-b"><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2"><FileJson className="size-4 text-primary" />Task definition</CardTitle><p className="mt-1 text-sm text-muted-foreground">Human-reviewed instructions. Editing creates a new atomic version.</p></div><Badge variant="outline">{shortHash(version.definition_content_hash)}</Badge></div></CardHeader>
          <CardContent className="space-y-3 p-4">
            <Textarea aria-label="Task definition" className="min-h-[28rem] resize-y font-mono text-sm leading-6" value={definition} onChange={(event) => setDefinition(event.target.value)} readOnly={!definitionEditable} />
            {definitionEditable ? <div className="flex justify-end"><Button variant="outline" onClick={() => saveDefinition.mutate()} disabled={(!definitionChanged && !canClonePublishedVersion) || !definition.trim() || saveDefinition.isPending}>{saveDefinition.isPending ? <LoaderCircle className="animate-spin" /> : canClonePublishedVersion && !definitionChanged ? <Plus /> : <Save />}{saveDefinition.isPending ? "Saving…" : canClonePublishedVersion && !definitionChanged ? "Create draft revision" : "Save as new draft"}</Button></div> : null}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader className="border-b"><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2"><Sparkles className="size-4 text-primary" />Decision Specification</CardTitle><p className="mt-1 text-sm text-muted-foreground">Validated structured questions, scoring criteria, evidence requirements, and routing policy.</p></div><div className="flex items-center gap-2"><StatusBadge status={version.compilation_status} />{version.specification_content_hash ? <Badge variant="outline">{shortHash(version.specification_content_hash)}</Badge> : null}</div></div></CardHeader>
          <CardContent className="space-y-3 p-4">
            {version.compilation_status === "GENERATING" ? <div className="flex min-h-[28rem] items-center justify-center gap-3 text-sm text-muted-foreground"><LoaderCircle className="size-5 animate-spin text-primary" />Generating and validating the Decision Specification…</div> : <Textarea aria-label="Decision Specification JSON" className="min-h-[28rem] resize-y font-mono text-xs leading-5" value={specification} onChange={(event) => setSpecification(event.target.value)} readOnly={!editable} placeholder="Generate a specification from the task definition." />}
            {editable && version.compilation_status !== "GENERATING" ? <div className="flex flex-wrap justify-end gap-2"><Button variant="outline" onClick={() => compile.mutate()} disabled={compile.isPending || definitionChanged}>{compile.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}{version.decision_specification ? "Regenerate" : "Generate"}</Button><Button variant="outline" onClick={() => saveSpecification.mutate()} disabled={!specification.trim() || saveSpecification.isPending}>{saveSpecification.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}Validate & save JSON</Button><Button onClick={() => publish.mutate()} disabled={version.compilation_status !== "READY" || publish.isPending}>{publish.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}{publish.isPending ? "Publishing…" : "Publish version"}</Button></div> : null}
            {editable && definitionChanged ? <p className="text-xs text-muted-foreground">Save the changed definition as a new draft before generating its specification.</p> : null}
            {version.compilation_status === "FAILED" ? <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{compilationError(version.validation_report) || "Specification generation failed. Review the model trace, then regenerate."}</p> : null}
          </CardContent>
        </Card>
      </div>
      <VersionMetadata version={version} />
      {version.status === "PUBLISHED" && version.compilation_status === "READY" ? <PlaygroundPanel matterId={matterId} task={task} version={version} /> : <Card className="p-4 text-sm text-muted-foreground">Publish a ready task version to enable the single-document playground.</Card>}
    </>
  );
}

function VersionMetadata({ version }: { version: MatterAnalysisTaskVersionRead }) {
  const warnings = arrayValue(version.validation_report.warnings);
  const omissions = arrayValue(version.validation_report.omissions);
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <span><strong>Version:</strong> {version.version}</span><StatusBadge status={version.status} /><span className="text-muted-foreground">Created {formatDate(version.created_at)}</span>
        {warnings.length ? <span className="text-amber-600 dark:text-amber-400">{warnings.length} compiler {warnings.length === 1 ? "warning" : "warnings"}</span> : null}
        {omissions.length ? <span className="text-muted-foreground">{omissions.length} documented {omissions.length === 1 ? "omission" : "omissions"}</span> : null}
      </div>
    </Card>
  );
}

function PlaygroundPanel({ matterId, task, version }: { matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead }) {
  const [batchId, setBatchId] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [run, setRun] = useState<MatterAnalysisTaskPlaygroundRunRead>();
  const batches = useQuery({ queryKey: ["analysis-task-playground-batches", matterId], queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`) });
  const readyBatches = batches.data?.filter((batch) => batch.status === "READY") ?? [];
  const documents = useQuery({
    queryKey: ["analysis-task-playground-documents", matterId, batchId],
    queryFn: () => coreApi<ReviewBatchDocumentRead[]>(`/v1/matters/${matterId}/review-batches/${batchId}/documents?limit=500`),
    enabled: Boolean(batchId),
  });
  const start = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskPlaygroundRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/playground-runs`, { method: "POST", body: JSON.stringify({ review_batch_id: batchId, matter_document_id: documentId }) }),
    onSuccess: (created) => { setRun(created); toast.success("The document evaluation was queued."); },
    onError: (error) => toast.error(error.message),
  });
  const runStatus = useQuery({
    queryKey: ["analysis-task-playground-run", matterId, task.id, version.version, run?.workflow_run_id],
    queryFn: () => coreApi<MatterAnalysisTaskPlaygroundRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/playground-runs/${run!.workflow_run_id}`),
    enabled: Boolean(run),
    refetchInterval: (query) => query.state.data && ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(query.state.data.status) ? false : 1000,
  });
  const currentRun = runStatus.data ?? run;
  const result = useQuery({
    queryKey: ["analysis-task-playground-result", currentRun?.review_batch_run_id, currentRun?.matter_document_id],
    queryFn: () => coreApi<ReviewDecisionResultRead>(`/v1/matters/${matterId}/review-batches/${currentRun!.review_batch_id}/runs/${currentRun!.review_batch_run_id}/documents/${currentRun!.matter_document_id}/decision-result`),
    enabled: Boolean(currentRun?.result_id),
  });

  function changeBatch(value: string) { setBatchId(value); setDocumentId(""); setRun(undefined); }

  return (
    <Card>
      <CardHeader className="border-b"><CardTitle className="flex items-center gap-2"><FlaskConical className="size-4 text-primary" />Single-document playground</CardTitle><p className="text-sm text-muted-foreground">Run the published version against one document. Results are stored with full provenance but are not published to matter metadata.</p></CardHeader>
      <CardContent className="space-y-4 p-4">
        {batches.error ? <QueryError message={batches.error.message} /> : <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] lg:items-end">
          <Field label="Ready batch" htmlFor="playground-batch"><Select value={batchId} onValueChange={changeBatch}><SelectTrigger id="playground-batch"><SelectValue placeholder={batches.isPending ? "Loading batches…" : "Select a batch"} /></SelectTrigger><SelectContent>{readyBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field>
          <Field label="Document" htmlFor="playground-document"><Select value={documentId} onValueChange={(value) => { setDocumentId(value); setRun(undefined); }} disabled={!batchId || documents.isPending}><SelectTrigger id="playground-document"><SelectValue placeholder={documents.isPending ? "Loading documents…" : "Select a document"} /></SelectTrigger><SelectContent>{(documents.data ?? []).map((document) => <SelectItem key={document.matter_document_id} value={document.matter_document_id}>#{document.sequence_number} · {document.collection_item_id}</SelectItem>)}</SelectContent></Select></Field>
          <Button onClick={() => start.mutate()} disabled={!batchId || !documentId || start.isPending}>{start.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}{start.isPending ? "Queueing…" : "Run document"}</Button>
        </div>}
        {readyBatches.length === 0 && !batches.isPending ? <p className="text-sm text-muted-foreground">Create or finish building a review batch before using the playground.</p> : null}
        {documents.error ? <QueryError message={documents.error.message} /> : null}
        {currentRun ? <PlaygroundRun run={currentRun} result={result.data} resultPending={result.isPending && result.isFetching} resultError={result.error} /> : null}
      </CardContent>
    </Card>
  );
}

function PlaygroundRun({ run, result, resultPending, resultError }: { run: MatterAnalysisTaskPlaygroundRunRead; result?: ReviewDecisionResultRead; resultPending: boolean; resultError: Error | null }) {
  const active = run.status === "QUEUED" || run.status === "RUNNING";
  return (
    <div className="rounded-xl border bg-muted/20 p-4">
      <div className="flex flex-wrap items-center gap-2"><StatusBadge status={run.status} />{active ? <LoaderCircle className="size-4 animate-spin text-primary" /> : null}<span className="font-mono text-xs text-muted-foreground">{run.workflow_run_id}</span></div>
      {run.error_message ? <p role="alert" className="mt-3 text-sm text-destructive">{run.error_message}</p> : null}
      {resultPending ? <p className="mt-3 text-sm text-muted-foreground">Loading the stored Decision Result…</p> : null}
      {resultError && !(resultError instanceof ApiError && resultError.status === 404) ? <p role="alert" className="mt-3 text-sm text-destructive">{resultError.message}</p> : null}
      {result ? <DecisionResult result={result} /> : null}
    </div>
  );
}

function DecisionResult({ result }: { result: ReviewDecisionResultRead }) {
  return (
    <div className="mt-4 space-y-4">
      <div className="flex flex-wrap items-center gap-2"><StatusBadge status={result.status} />{result.coverage.evidence_complete === false ? <Badge variant="outline"><AlertTriangle className="mr-1 size-3" />Evidence localization pending</Badge> : null}<span className="text-xs text-muted-foreground">{result.provider} · {result.model} · {result.latency_ms.toLocaleString()} ms</span></div>
      <div className="grid gap-3 xl:grid-cols-2">
        <JsonResult title="Answers" value={result.answers} />
        <JsonResult title="Recommendations & routes" value={{ recommendations: result.recommendations, routes: result.routes }} />
      </div>
      <p className="text-xs text-muted-foreground">{result.request_count.toLocaleString()} provider {result.request_count === 1 ? "request" : "requests"} · {result.input_tokens.toLocaleString()} input tokens · {result.output_tokens.toLocaleString()} output tokens</p>
    </div>
  );
}

function JsonResult({ title, value }: { title: string; value: unknown }) {
  return <div className="min-w-0"><h4 className="mb-2 text-sm font-semibold">{title}</h4><pre className="max-h-80 overflow-auto rounded-lg bg-background p-3 text-xs leading-5">{JSON.stringify(value, null, 2)}</pre></div>;
}

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return <div className="space-y-1.5"><Label htmlFor={htmlFor}>{label}</Label>{children}</div>;
}

function taskKey(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^[^a-z]+/, "").slice(0, 100);
}

function shortHash(value: string) { return value.slice(0, 10); }
function friendly(value: string) { return value.toLowerCase().replaceAll("_", " "); }
function arrayValue(value: unknown) { return Array.isArray(value) ? value : []; }
function stringValue(value: unknown) { return typeof value === "string" ? value : undefined; }
function compilationError(report: Record<string, unknown>) {
  const direct = stringValue(report.error);
  if (direct) return direct;
  const first = arrayValue(report.errors)[0];
  return first && typeof first === "object" ? stringValue((first as Record<string, unknown>).message) : undefined;
}
