"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Ban, BrainCircuit, Check, FileJson, FileSearch, FlaskConical, LoaderCircle, Play, Plus, Save, Sparkles } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { toast } from "sonner";

import { FactTaskWorkspace } from "@/components/facts/fact-task-workspace";
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
  MatterAnalysisTaskRunRead,
  MatterAnalysisTaskPlaygroundRunRead,
  MatterAnalysisTaskRead,
  MatterAnalysisTaskVersionRead,
  ClassificationModelRead,
  JobExecutionRead,
  MatterDefinitionRead,
  MatterEmbeddingJobRead,
  MetadataDefinitionRead,
  ReviewBatchDocumentRead,
  ReviewBatchRead,
  ReviewDecisionResultRead,
} from "@/generated/models";
import { ApiError, coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const TASK_TYPE_LABELS = {
  QUESTION_ANSWERING: "Question answering",
  CLASSIFICATION: "Classification",
  FACT_DEVELOPMENT: "Fact development",
} as const;

const TASK_TYPE_DISPLAY_LABELS = TASK_TYPE_LABELS;

const DECISION_PROVIDER_LABELS = {
  JEV: "TypeSafe JEV",
  OPENAI_DECISIONS: "OpenAI Decisions",
} as const;

type DecisionProvider = keyof typeof DECISION_PROVIDER_LABELS;

type CreatableTaskType = keyof typeof TASK_TYPE_LABELS;

export function AnalysisTasksPanel({ clientId, matterId }: { clientId?: string; matterId: string }) {
  const queryClient = useQueryClient();
  const [selectedTaskId, setSelectedTaskId] = useState<string>();
  const tasks = useQuery({
    queryKey: ["analysis-tasks", matterId],
    queryFn: () => coreApi<MatterAnalysisTaskRead[]>(`/v1/matters/${matterId}/analysis-tasks`),
    refetchInterval: (query) => query.state.data?.some((task) => task.version.compilation_status === "GENERATING") ? 1500 : false,
  });
  const definitions = useQuery({
    queryKey: ["metadata-definitions", matterId],
    queryFn: () => coreApi<MetadataDefinitionRead[]>(`/v1/matters/${matterId}/metadata-definitions`),
  });
  const classificationModels = useQuery({
    queryKey: ["classification-models", matterId],
    queryFn: () => coreApi<ClassificationModelRead[]>(`/v1/matters/${matterId}/classification-models`),
  });
  const aiAssignableDefinitions = (definitions.data ?? []).filter((definition) => (
    definition.status === "ACTIVE" && definition.value_source === "ASSERTED" && definition.ai_assignable
  ));
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
  if (tasks.error || definitions.error || classificationModels.error) return <QueryError message={(tasks.error ?? definitions.error ?? classificationModels.error)!.message} />;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Analysis tasks</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">Run governed JEV, OpenAI Decisions, statistical-classification, or fact-development analysis against a review batch or the whole matter. Results remain isolated from matter coding.</p>
        </div>
        <CreateAnalysisTaskDialog matterId={matterId} destinationDefinitions={aiAssignableDefinitions} classificationModels={classificationModels.data ?? []} onCreated={updateTask} />
      </div>

      {!tasks.data.length ? (
        <Card className="p-8 text-center">
          <Sparkles className="mx-auto mb-3 size-7 text-primary" />
          <h3 className="font-semibold">No analysis tasks yet</h3>
          <p className="mx-auto mt-2 max-w-xl text-sm text-muted-foreground">Create a question-answering, classification, or fact-development task. Each published version pins the engine configuration used by every run.</p>
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
                    <span>{TASK_TYPE_DISPLAY_LABELS[task.task_type]}</span>
                    <span aria-hidden="true">·</span>
                    <span>v{task.current_version}</span>
                    {task.published_version ? <Badge variant="outline">published v{task.published_version}</Badge> : null}
                  </span>
                </button>
              ))}
            </div>
          </Card>
          {selectedTask ? <AnalysisTaskDetail key={selectedTask.id} clientId={clientId} matterId={matterId} task={selectedTask} definitions={definitions.data ?? []} onTaskUpdated={updateTask} /> : null}
        </div>
      )}
    </div>
  );
}

function CreateAnalysisTaskDialog({ matterId, destinationDefinitions, classificationModels, onCreated }: { matterId: string; destinationDefinitions: MetadataDefinitionRead[]; classificationModels: ClassificationModelRead[]; onCreated: (task: MatterAnalysisTaskRead) => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [description, setDescription] = useState("");
  const [taskType, setTaskType] = useState<CreatableTaskType>("QUESTION_ANSWERING");
  const [decisionProvider, setDecisionProvider] = useState<DecisionProvider>("JEV");
  const [definition, setDefinition] = useState("");
  const [destinationIds, setDestinationIds] = useState<string[]>([]);
  const [classificationModelId, setClassificationModelId] = useState("");
  const [guidanceId, setGuidanceId] = useState("");
  const guidance = useQuery({
    queryKey: ["review-guidance", matterId],
    queryFn: () => coreApi<MatterDefinitionRead[]>(`/v1/matters/${matterId}/guidance`),
    enabled: open && taskType === "FACT_DEVELOPMENT",
  });
  const publishedGuidance = (guidance.data ?? []).filter((item) => item.status === "ACTIVE" && item.published_revision);
  const runnableModels = classificationModels.filter((model) => model.status === "ACTIVE" && model.active_model_version_id);
  const selectedModel = runnableModels.find((model) => model.id === classificationModelId);
  const mutation = useMutation({
    mutationFn: (payload: MatterAnalysisTaskCreate) => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks`, { method: "POST", body: JSON.stringify(payload) }),
    onSuccess: (created) => {
      onCreated(created);
      setOpen(false);
      setName("");
      setKey("");
      setDescription("");
      setDefinition("");
      setDecisionProvider("JEV");
      setDestinationIds([]);
      setClassificationModelId("");
      setGuidanceId("");
      toast.success(`${created.name} was created.`);
    },
  });

  function changeName(value: string) {
    setName(value);
    if (!key || key === taskKey(name)) setKey(taskKey(value));
  }

  function create() {
    const classification = taskType === "CLASSIFICATION";
    if (taskType === "FACT_DEVELOPMENT") {
      mutation.mutate({
        key,
        name: name.trim(),
        description: description.trim() || null,
        task_type: taskType,
        definition_markdown: definition.trim(),
        review_guidance_id: guidanceId,
      });
      return;
    }
    mutation.mutate({
      key,
      name: name.trim(),
      description: description.trim() || null,
      task_type: taskType,
      decision_provider: classification ? "JEV" : decisionProvider,
      definition_markdown: classification ? `Apply approved classifier ${selectedModel?.name ?? "model"} to the selected scope.` : definition.trim(),
      destination_metadata_definition_ids: classification && selectedModel ? [selectedModel.target_metadata_definition_id] : destinationIds,
      classification_model_version_id: classification ? selectedModel?.active_model_version_id ?? null : null,
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
          <Field label="Task type" htmlFor="analysis-task-type"><Select value={taskType} onValueChange={(value) => { setTaskType(value as CreatableTaskType); setDestinationIds([]); }}><SelectTrigger id="analysis-task-type"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(TASK_TYPE_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></Field>
          <Field label="Description" htmlFor="analysis-task-description"><Input id="analysis-task-description" value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this task decides" /></Field>
          {taskType === "QUESTION_ANSWERING" ? <Field label="Decision provider" htmlFor="analysis-task-provider"><Select value={decisionProvider} onValueChange={(value) => setDecisionProvider(value as DecisionProvider)}><SelectTrigger id="analysis-task-provider"><SelectValue /></SelectTrigger><SelectContent>{Object.entries(DECISION_PROVIDER_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></Field> : null}
        </div>
        {taskType === "FACT_DEVELOPMENT" ? <div className="space-y-3"><Field label="Review Guidance" htmlFor="analysis-task-guidance"><Select value={guidanceId} onValueChange={setGuidanceId}><SelectTrigger id="analysis-task-guidance"><SelectValue placeholder={guidance.isPending ? "Loading guidance…" : "Select published guidance"} /></SelectTrigger><SelectContent>{publishedGuidance.map((item) => <SelectItem key={item.id} value={item.id}>{item.name} · revision {item.published_revision}</SelectItem>)}</SelectContent></Select></Field>{!guidance.isPending && !publishedGuidance.length ? <p className="text-sm text-muted-foreground">Publish a Review Guidance revision first; its issues define what facts are developed.</p> : null}<Field label="Fact guidance" htmlFor="analysis-task-fact-guidance"><Textarea id="analysis-task-fact-guidance" className="min-h-48 font-mono text-sm" value={definition} onChange={(event) => setDefinition(event.target.value)} placeholder="# Facts&#10;&#10;Record one dated event, decision, or statement per fact. Describe how to date facts and who owns them…" /></Field></div> : taskType === "CLASSIFICATION" ? <div className="space-y-3"><Field label="Activated model version" htmlFor="analysis-task-classifier"><Select value={classificationModelId} onValueChange={setClassificationModelId}><SelectTrigger id="analysis-task-classifier"><SelectValue placeholder="Select an approved, activated classifier" /></SelectTrigger><SelectContent>{runnableModels.map((model) => <SelectItem key={model.id} value={model.id}>{model.name} · {model.target_field.display_name}</SelectItem>)}</SelectContent></Select></Field>{selectedModel ? <p className="rounded-md border bg-muted/20 p-3 text-sm">Destination field: <strong>{selectedModel.target_field.display_name}</strong>. The task version will pin model version <code>{selectedModel.active_model_version_id}</code>.</p> : null}{!runnableModels.length ? <p className="text-sm text-muted-foreground">No activated classification models are available.</p> : null}</div> : <><DestinationFieldPicker definitions={destinationDefinitions} selectedIds={destinationIds} onChange={setDestinationIds} /><Field label="Task definition" htmlFor="analysis-task-definition"><Textarea id="analysis-task-definition" className="min-h-64 font-mono text-sm" value={definition} onChange={(event) => setDefinition(event.target.value)} placeholder="# Purpose&#10;&#10;Describe the decision criteria, inclusions, exclusions, and ambiguity rules…" /></Field></>}
        {mutation.error ? <p role="alert" className="text-sm text-destructive">{mutation.error.message}</p> : null}
        <DialogFooter><Button variant="outline" onClick={() => setOpen(false)} disabled={mutation.isPending}>Cancel</Button><Button onClick={create} disabled={mutation.isPending || !name.trim() || !key || (taskType === "QUESTION_ANSWERING" ? !definition.trim() : taskType === "FACT_DEVELOPMENT" ? !definition.trim() || !guidanceId : !selectedModel)}>{mutation.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}{mutation.isPending ? "Creating…" : "Create task"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AnalysisTaskDetail({ clientId, matterId, task, definitions, onTaskUpdated }: { clientId?: string; matterId: string; task: MatterAnalysisTaskRead; definitions: MetadataDefinitionRead[]; onTaskUpdated: (task: MatterAnalysisTaskRead) => void }) {
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
            <div className="flex flex-wrap items-center gap-2"><h3 className="text-xl font-semibold">{task.name}</h3><StatusBadge status={task.status} /><Badge variant="outline">{TASK_TYPE_DISPLAY_LABELS[task.task_type]}</Badge></div>
            <p className="mt-1 text-sm text-muted-foreground">{task.description || "No task description."}</p>
            <p className="mt-2 font-mono text-xs text-muted-foreground">{task.key} · {task.workflow_key}</p>
          </div>
          <div className="w-48 space-y-1.5"><Label htmlFor={`task-version-${task.id}`}>Version</Label><Select value={String(selectedVersion.version)} onValueChange={(value) => setSelectedVersionNumber(Number(value))}><SelectTrigger id={`task-version-${task.id}`}><SelectValue /></SelectTrigger><SelectContent>{(versions.data ?? [task.version]).map((version) => <SelectItem key={version.id} value={String(version.version)}>v{version.version} · {friendly(version.status)}</SelectItem>)}</SelectContent></Select></div>
        </div>
      </Card>
      {versions.isPending ? <TableLoading /> : versions.error ? <QueryError message={versions.error.message} /> : (
        task.task_type === "FACT_DEVELOPMENT"
          ? <FactTaskWorkspace key={`${selectedVersion.id}:${selectedVersion.compilation_status}:${selectedVersion.specification_content_hash ?? "none"}`} clientId={clientId} matterId={matterId} task={task} version={selectedVersion} onTaskUpdated={handleTaskUpdated} />
          : task.task_type === "CLASSIFICATION"
          ? <ClassificationVersionWorkspace key={selectedVersion.id} clientId={clientId} matterId={matterId} task={task} version={selectedVersion} onTaskUpdated={handleTaskUpdated} />
          : <VersionWorkspace
              key={`${selectedVersion.id}:${selectedVersion.compilation_status}:${selectedVersion.specification_content_hash ?? "none"}`}
              matterId={matterId}
              task={task}
              version={selectedVersion}
              definitions={definitions}
              onTaskUpdated={handleTaskUpdated}
            />
      )}
    </div>
  );
}

function ClassificationVersionWorkspace({ clientId, matterId, task, version, onTaskUpdated }: { clientId?: string; matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead; onTaskUpdated: (task: MatterAnalysisTaskRead) => void }) {
  const queryClient = useQueryClient();
  const publish = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions/${version.version}/publish`, { method: "POST" }),
    onSuccess: (updated) => {
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-versions", matterId, task.id] });
      onTaskUpdated(updated);
      toast.success(`Classification task version ${version.version} was published.`);
    },
    onError: (error) => toast.error(error.message),
  });
  return <div className="space-y-4">
    <Card>
      <CardHeader className="border-b"><CardTitle className="flex items-center gap-2"><BrainCircuit className="size-4 text-primary" />Classification configuration</CardTitle><p className="text-sm text-muted-foreground">This task version pins an approved statistical model. Runs freeze their document population and reuse the durable classification scorer.</p></CardHeader>
      <CardContent className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2"><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Classification model</p><p className="mt-1 break-all font-mono text-sm">{version.classification_model_id}</p></div><div className="rounded-md border p-3"><p className="text-xs text-muted-foreground">Pinned approved version</p><p className="mt-1 break-all font-mono text-sm">{version.classification_model_version_id}</p></div></div>
        <div className="flex flex-wrap items-center justify-between gap-3"><VersionMetadata version={version} />{version.status === "DRAFT" ? <Button onClick={() => publish.mutate()} disabled={publish.isPending}>{publish.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}{publish.isPending ? "Publishing…" : "Publish version"}</Button> : null}</div>
      </CardContent>
    </Card>
    {version.status === "PUBLISHED" ? <ClassificationRunPanel clientId={clientId} matterId={matterId} task={task} version={version} /> : <Card className="p-4 text-sm text-muted-foreground">Publish this task version before running it.</Card>}
  </div>;
}

function ClassificationRunPanel({ clientId, matterId, task, version }: { clientId?: string; matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead }) {
  const queryClient = useQueryClient();
  const [scopeType, setScopeType] = useState<"MATTER" | "REVIEW_BATCH">("MATTER");
  const [batchId, setBatchId] = useState("");
  const [embeddingJobId, setEmbeddingJobId] = useState("");
  const runs = useQuery({
    queryKey: ["analysis-task-runs", matterId, task.id],
    queryFn: () => coreApi<MatterAnalysisTaskRunRead[]>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/runs`),
    refetchInterval: (query) => query.state.data?.some((run) => !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status)) ? 1500 : false,
  });
  const batches = useQuery({ queryKey: ["review-batches", matterId], queryFn: () => coreApi<ReviewBatchRead[]>(`/v1/matters/${matterId}/review-batches`) });
  const embeddings = useQuery({ queryKey: ["matter-embedding-jobs", matterId], queryFn: () => coreApi<MatterEmbeddingJobRead[]>(`/v1/matters/${matterId}/embedding-jobs`) });
  const usableEmbeddings = (embeddings.data ?? []).filter((job) => ["COMPLETED", "COMPLETED_WITH_ERRORS"].includes(job.status) && (!version.classification_embedding_job_id || job.id === version.classification_embedding_job_id));
  const effectiveEmbeddingJobId = usableEmbeddings.some((job) => job.id === embeddingJobId) ? embeddingJobId : usableEmbeddings[0]?.id ?? "";
  const readyBatches = (batches.data ?? []).filter((batch) => batch.status === "READY");
  const start = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/runs`, {
      method: "POST",
      body: JSON.stringify({
        scope_type: scopeType,
        scope_id: scopeType === "MATTER" ? matterId : batchId,
        embedding_job_id: effectiveEmbeddingJobId,
        idempotency_key: crypto.randomUUID(),
      }),
    }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId, task.id] });
      toast.success("Classification Analysis Task was queued.");
    },
    onError: (error) => toast.error(error.message),
  });
  const cancel = useMutation({
    mutationFn: (runId: string) => coreApi<MatterAnalysisTaskRunRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/runs/${runId}/cancel`, { method: "POST" }),
    onSuccess: (canceledRun) => {
      queryClient.setQueryData<MatterAnalysisTaskRunRead[]>(["analysis-task-runs", matterId, task.id], (current) => (
        current?.map((run) => run.id === canceledRun.id ? canceledRun : run) ?? [canceledRun]
      ));
      toast.success("Classification Analysis Task run was canceled.");
    },
    onError: (error) => toast.error(error.message),
  });
  const retry = useMutation({
    mutationFn: (runId: string) => coreApi<JobExecutionRead>(`/v1/jobs/CLASSIFICATION_ANALYSIS_TASK/${runId}/retry`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["analysis-task-runs", matterId, task.id] });
      toast.success("A new execution attempt was queued.");
    },
    onError: (error) => toast.error(error.message),
  });
  const cannotStart = !effectiveEmbeddingJobId || (scopeType === "REVIEW_BATCH" && !batchId) || start.isPending;
  return <Card>
    <CardHeader className="border-b"><CardTitle className="flex items-center gap-2"><Play className="size-4 text-primary" />Run classification</CardTitle><p className="text-sm text-muted-foreground">Score a frozen review batch or artifact-backed whole-matter population. This does not update authoritative matter coding.</p></CardHeader>
    <CardContent className="space-y-5 p-4">
      <div className="grid gap-4 lg:grid-cols-3 lg:items-end">
        <Field label="Document scope" htmlFor="classification-analysis-scope"><Select value={scopeType} onValueChange={(value) => { setScopeType(value as "MATTER" | "REVIEW_BATCH"); setBatchId(""); }}><SelectTrigger id="classification-analysis-scope"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="MATTER">Whole matter</SelectItem><SelectItem value="REVIEW_BATCH">Review batch</SelectItem></SelectContent></Select></Field>
        {scopeType === "REVIEW_BATCH" ? <Field label="Ready batch" htmlFor="classification-analysis-batch"><Select value={batchId} onValueChange={setBatchId}><SelectTrigger id="classification-analysis-batch"><SelectValue placeholder="Select a batch" /></SelectTrigger><SelectContent>{readyBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} · {batch.document_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field> : <div className="rounded-md border bg-muted/20 px-3 py-2 text-sm"><strong>Whole matter</strong><p className="mt-1 text-xs text-muted-foreground">Membership is frozen in an immutable population artifact; no per-document batch rows are created.</p></div>}
        <Field label="Document embeddings" htmlFor="classification-analysis-embeddings"><Select value={effectiveEmbeddingJobId} onValueChange={setEmbeddingJobId}><SelectTrigger id="classification-analysis-embeddings"><SelectValue placeholder="Select completed embeddings" /></SelectTrigger><SelectContent>{usableEmbeddings.map((job) => <SelectItem key={job.id} value={job.id}>{job.embedding_model} · {job.embedding_dimensions} dimensions · {job.processed_count.toLocaleString()} docs</SelectItem>)}</SelectContent></Select></Field>
      </div>
      <div className="flex justify-end"><Button onClick={() => start.mutate()} disabled={cannotStart}>{start.isPending ? <LoaderCircle className="animate-spin" /> : <Play />}{start.isPending ? "Queueing…" : "Run classification"}</Button></div>
      {!usableEmbeddings.length && !embeddings.isPending ? <p className="text-sm text-amber-600">Complete a matter embedding job before running classification.</p> : null}
      {runs.error || batches.error || embeddings.error ? <QueryError message={(runs.error ?? batches.error ?? embeddings.error)!.message} /> : runs.isPending ? <TableLoading /> : runs.data.length ? (
        <div className="space-y-2">
          <h4 className="font-semibold">Runs</h4>
          {runs.data.map((run) => (
            <ClassificationRunCard
              key={run.id}
              clientId={clientId}
              matterId={matterId}
              run={run}
              scopeName={run.scope_type === "MATTER" ? "Whole matter" : readyBatches.find((batch) => batch.id === run.scope_id)?.name ?? "Review batch"}
              canceling={cancel.isPending && cancel.variables === run.id}
              onCancel={() => cancel.mutate(run.id)}
              retrying={retry.isPending && retry.variables === run.id}
              onRetry={() => retry.mutate(run.id)}
            />
          ))}
        </div>
      ) : <p className="text-sm text-muted-foreground">No runs have been started for this task.</p>}
    </CardContent>
  </Card>;
}

function ClassificationRunCard({ clientId, matterId, run, scopeName, canceling, onCancel, retrying, onRetry }: {
  clientId?: string;
  matterId: string;
  run: MatterAnalysisTaskRunRead;
  scopeName: string;
  canceling: boolean;
  onCancel: () => void;
  retrying: boolean;
  onRetry: () => void;
}) {
  const execution = run.execution;
  const domainTerminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(run.status);
  const status = domainTerminal ? run.status : execution?.status ?? run.status;
  const stage = domainTerminal ? run.current_stage : execution?.current_stage ?? run.current_stage;
  const progressCompleted = execution?.progress_completed ?? run.progress_completed;
  const progressTotal = execution?.progress_total ?? run.progress_total;
  const featureProcessed = run.feature_processed_count ?? 0;
  const featureFraction = run.population_document_count ? featureProcessed / run.population_document_count : 0;
  const progress = run.status === "PREPARING_FEATURES"
    ? Math.round(((1 + featureFraction) / run.progress_total) * 100)
    : progressTotal
      ? Math.round((progressCompleted / progressTotal) * 100)
      : 0;
  const active = !["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELED"].includes(status);
  const cancelRequested = Boolean(execution?.cancel_requested_at);
  const canCancel = !domainTerminal && (execution
    ? execution.available_actions.includes("CANCEL")
    : active);
  const canRetry = run.status === "CANCELED" || (run.status === "FAILED" && run.failure_retriable) || (execution?.available_actions.includes("RETRY") ?? false);
  const failureSummary = domainTerminal ? run.failure_summary ?? execution?.failure_summary : execution?.failure_summary ?? run.failure_summary;

  return (
    <div className="rounded-lg border p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-medium">{scopeName}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {friendly(stage ?? status)} · {run.population_document_count.toLocaleString()} documents · {formatDate(run.created_at)}
          </p>
          {execution ? (
            <p className="mt-1 text-xs text-muted-foreground">
              Attempt {execution.attempt_number}
              {execution.last_heartbeat_at ? ` · last update ${formatDate(execution.last_heartbeat_at)}` : ""}
            </p>
          ) : null}
          {featureProcessed > 0 ? (
            <p className="mt-1 text-xs text-muted-foreground">
              {featureProcessed.toLocaleString()} of {run.population_document_count.toLocaleString()} features processed · {(run.feature_ready_count ?? 0).toLocaleString()} ready · {(run.feature_missing_count ?? 0).toLocaleString()} missing · {(run.feature_failed_count ?? 0).toLocaleString()} failed
            </p>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge status={status} />
          {cancelRequested && active ? <Badge variant="outline">Cancellation requested</Badge> : null}
          {canCancel ? (
            <Button size="sm" variant="outline" disabled={canceling} onClick={onCancel}>
              {canceling ? <LoaderCircle className="animate-spin" /> : <Ban />}
              {canceling ? "Canceling…" : "Cancel run"}
            </Button>
          ) : null}
          {canRetry ? (
            <Button size="sm" variant="outline" disabled={retrying} onClick={onRetry}>
              {retrying ? <LoaderCircle className="animate-spin" /> : <Play />}
              {retrying ? "Retrying…" : "Retry run"}
            </Button>
          ) : null}
          {clientId ? <Button asChild size="sm" variant="outline"><Link href={`/app/clients/${clientId}/matters/${matterId}?tab=jobs&job_type=CLASSIFICATION_ANALYSIS&job=${run.id}`}><FileSearch />Open job</Link></Button> : null}
        </div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-label={`${progressCompleted} of ${progressTotal ?? "unknown"} execution stages completed`}>
        <div className="h-full bg-primary transition-[width]" style={{ width: `${Math.min(100, progress)}%` }} />
      </div>
      {failureSummary ? <p className="mt-2 text-sm text-destructive">{failureSummary}</p> : null}
    </div>
  );
}

function VersionWorkspace({ matterId, task, version, definitions, onTaskUpdated }: { matterId: string; task: MatterAnalysisTaskRead; version: MatterAnalysisTaskVersionRead; definitions: MetadataDefinitionRead[]; onTaskUpdated: (task: MatterAnalysisTaskRead) => void }) {
  const queryClient = useQueryClient();
  const [definition, setDefinition] = useState(version.definition_markdown);
  const [decisionProvider, setDecisionProvider] = useState<DecisionProvider>(version.decision_provider);
  const [destinationIds, setDestinationIds] = useState<string[]>(version.destination_metadata_definition_ids ?? []);
  const [destinationSelectionEdited, setDestinationSelectionEdited] = useState(false);
  const [specification, setSpecification] = useState(version.decision_specification ? JSON.stringify(version.decision_specification, null, 2) : "");
  const canCreateDraft = version.version === task.current_version && task.status === "ACTIVE";
  const editable = canCreateDraft && version.status === "DRAFT";
  const definitionEditable = canCreateDraft;
  const definitionChanged = definition.trim() !== version.definition_markdown.trim();
  const providerChanged = decisionProvider !== version.decision_provider;
  const originalDestinationIds = version.destination_metadata_definition_ids ?? [];
  const destinationChanged = destinationSelectionEdited && !sameIds(destinationIds, originalDestinationIds);
  const canClonePublishedVersion = definitionEditable && version.status === "PUBLISHED";
  const saveDefinition = useMutation({
    mutationFn: () => coreApi<MatterAnalysisTaskRead>(`/v1/matters/${matterId}/analysis-tasks/${task.id}/versions`, { method: "POST", body: JSON.stringify({
      definition_markdown: definition.trim(),
      based_on_version: task.current_version,
      decision_provider: decisionProvider,
      ...(destinationSelectionEdited || version.destination_metadata_definition_ids !== null
        ? { destination_metadata_definition_ids: destinationIds }
        : {}),
    }) }),
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
            <DestinationFieldPicker
              definitions={definitions.filter((item) => (item.status === "ACTIVE" && item.value_source === "ASSERTED" && item.ai_assignable) || destinationIds.includes(item.id))}
              selectedIds={destinationIds}
              onChange={(nextIds) => { setDestinationIds(nextIds); setDestinationSelectionEdited(true); }}
              disabled={!definitionEditable}
              legacySelection={version.destination_metadata_definition_ids === null}
            />
            <Field label="Decision provider" htmlFor={`analysis-task-provider-${version.id}`}><Select value={decisionProvider} onValueChange={(value) => setDecisionProvider(value as DecisionProvider)} disabled={!definitionEditable}><SelectTrigger id={`analysis-task-provider-${version.id}`}><SelectValue /></SelectTrigger><SelectContent>{Object.entries(DECISION_PROVIDER_LABELS).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></Field>
            <Textarea aria-label="Task definition" className="min-h-[28rem] resize-y font-mono text-sm leading-6" value={definition} onChange={(event) => setDefinition(event.target.value)} readOnly={!definitionEditable} />
            {definitionEditable ? <div className="flex justify-end"><Button variant="outline" onClick={() => saveDefinition.mutate()} disabled={(!definitionChanged && !destinationChanged && !providerChanged && !canClonePublishedVersion) || !definition.trim() || saveDefinition.isPending}>{saveDefinition.isPending ? <LoaderCircle className="animate-spin" /> : canClonePublishedVersion && !definitionChanged && !destinationChanged && !providerChanged ? <Plus /> : <Save />}{saveDefinition.isPending ? "Saving…" : canClonePublishedVersion && !definitionChanged && !destinationChanged && !providerChanged ? "Create draft revision" : "Save as new draft"}</Button></div> : null}
          </CardContent>
        </Card>

        <Card className="min-w-0">
          <CardHeader className="border-b"><div className="flex flex-wrap items-start justify-between gap-3"><div><CardTitle className="flex items-center gap-2"><Sparkles className="size-4 text-primary" />Decision Specification</CardTitle><p className="mt-1 text-sm text-muted-foreground">Validated structured questions, scoring criteria, evidence requirements, and routing policy.</p></div><div className="flex items-center gap-2"><StatusBadge status={version.compilation_status} />{version.specification_content_hash ? <Badge variant="outline">{shortHash(version.specification_content_hash)}</Badge> : null}</div></div></CardHeader>
          <CardContent className="space-y-3 p-4">
            {version.compilation_status === "GENERATING" ? <CompilationProgress progress={version.compilation_progress} /> : <Textarea aria-label="Decision Specification JSON" className="min-h-[28rem] resize-y font-mono text-xs leading-5" value={specification} onChange={(event) => setSpecification(event.target.value)} readOnly={!editable} placeholder="Generate a specification from the task definition." />}
            {editable && version.compilation_status !== "GENERATING" ? <div className="flex flex-wrap justify-end gap-2"><Button variant="outline" onClick={() => compile.mutate()} disabled={compile.isPending || definitionChanged || destinationChanged || providerChanged}>{compile.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}{version.decision_specification ? "Regenerate" : "Generate"}</Button><Button variant="outline" onClick={() => saveSpecification.mutate()} disabled={!specification.trim() || saveSpecification.isPending}>{saveSpecification.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}Validate & save JSON</Button><Button onClick={() => publish.mutate()} disabled={version.compilation_status !== "READY" || publish.isPending}>{publish.isPending ? <LoaderCircle className="animate-spin" /> : <Check />}{publish.isPending ? "Publishing…" : "Publish version"}</Button></div> : null}
            {editable && (definitionChanged || destinationChanged || providerChanged) ? <p className="text-xs text-muted-foreground">Save the changed definition, destinations, and provider as a new draft before generating its specification.</p> : null}
            {version.compilation_status === "FAILED" ? <p role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{compilationError(version.validation_report) || "Specification generation failed. Review the model trace, then regenerate."}</p> : null}
          </CardContent>
        </Card>
      </div>
      <VersionMetadata version={version} />
      {version.status === "PUBLISHED" && version.compilation_status === "READY" ? <PlaygroundPanel matterId={matterId} task={task} version={version} /> : <Card className="p-4 text-sm text-muted-foreground">Publish a ready task version to enable the single-document playground.</Card>}
    </>
  );
}

function CompilationProgress({ progress }: { progress?: Record<string, unknown> }) {
  const state = progress ?? {};
  const stage = stringValue(state.stage) ?? "QUEUED";
  const message = stringValue(state.message) ?? "Generating and validating the Decision Specification.";
  const validationAttempt = numberValue(state.validation_attempt) ?? 0;
  const sourceReferenceCount = numberValue(state.source_reference_count) ?? 0;
  const destinations = arrayValue(state.destinations).map(recordValue).filter((value): value is Record<string, unknown> => Boolean(value));
  const failures = arrayValue(state.validation_failures).map(recordValue).filter((value): value is Record<string, unknown> => Boolean(value));
  return (
    <div role="status" className="min-h-[28rem] space-y-4 rounded-lg border bg-muted/20 p-5">
      <div className="flex items-start gap-3">
        <LoaderCircle className="mt-0.5 size-5 shrink-0 animate-spin text-primary" />
        <div><p className="font-medium">Generating and validating the Decision Specification</p><p className="mt-1 text-sm text-muted-foreground">{message}</p></div>
      </div>
      <div className="flex flex-wrap gap-2 text-xs"><Badge variant="outline">{friendly(stage)}</Badge><Badge variant="outline">{sourceReferenceCount.toLocaleString()} source references</Badge><Badge variant="outline">{destinations.length.toLocaleString()} destinations</Badge>{validationAttempt ? <Badge variant="outline">candidate {validationAttempt}</Badge> : null}</div>
      {destinations.length ? <div><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Destination plan</p><div className="space-y-2">{destinations.map((destination, index) => <div key={stringValue(destination.key) ?? index} className="flex flex-wrap items-center justify-between gap-2 rounded-md border bg-background px-3 py-2 text-sm"><span><span className="font-medium">{stringValue(destination.display_name) ?? stringValue(destination.key) ?? "Destination"}</span><code className="ml-2 text-xs text-muted-foreground">{stringValue(destination.key)}</code></span><span className="text-xs text-muted-foreground">{friendly(stringValue(destination.cardinality) ?? "")} {friendly(stringValue(destination.type) ?? "")}{numberValue(destination.active_value_count) ? ` · ${numberValue(destination.active_value_count)!.toLocaleString()} active values` : ""}</span></div>)}</div></div> : null}
      {failures.length ? <div><p className="mb-2 text-xs font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">Validation repairs</p><div className="space-y-2">{failures.map((failure, index) => <div key={`${numberValue(failure.attempt) ?? index}`} className="rounded-md bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300"><span className="font-semibold">Candidate {numberValue(failure.attempt) ?? index + 1}:</span> {stringValue(failure.message) ?? "Validation failed; a repaired candidate was requested."}</div>)}</div></div> : null}
      <p className="text-xs text-muted-foreground">This view shows workflow stages, destination coverage, and validation evidence. It does not expose private model reasoning.</p>
    </div>
  );
}

function VersionMetadata({ version }: { version: MatterAnalysisTaskVersionRead }) {
  const warnings = arrayValue(version.validation_report.warnings);
  const omissions = arrayValue(version.validation_report.omissions);
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-sm">
        <span><strong>Version:</strong> {version.version}</span><StatusBadge status={version.status} /><span className="text-muted-foreground">Created {formatDate(version.created_at)}</span>
        <span><strong>Provider:</strong> {DECISION_PROVIDER_LABELS[version.decision_provider]}{version.decision_model ? ` · ${version.decision_model}` : ""}</span>
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

function DestinationFieldPicker({ definitions, selectedIds, onChange, disabled = false, legacySelection = false }: {
  definitions: MetadataDefinitionRead[];
  selectedIds: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
  legacySelection?: boolean;
}) {
  function toggle(id: string, checked: boolean) {
    onChange(checked ? [...selectedIds, id] : selectedIds.filter((value) => value !== id));
  }
  return (
    <fieldset className="space-y-2 rounded-lg border p-3" disabled={disabled}>
      <legend className="px-1 text-sm font-medium">Destination fields</legend>
      <p className="text-xs text-muted-foreground">The compiler may create coding recommendations only for these active, AI-assignable fields.</p>
      {legacySelection ? <p className="text-xs text-amber-600 dark:text-amber-400">This older version inferred destinations from its task definition. Select explicit destinations when creating the next revision.</p> : null}
      {definitions.length ? <div className="grid gap-2 sm:grid-cols-2">{definitions.map((definition) => {
        const available = definition.status === "ACTIVE" && definition.value_source === "ASSERTED" && definition.ai_assignable;
        return <label key={definition.id} className="flex items-start gap-2 rounded-md border bg-background p-2 text-sm"><input type="checkbox" className="mt-0.5 size-4 accent-primary" checked={selectedIds.includes(definition.id)} onChange={(event) => toggle(definition.id, event.target.checked)} /><span><span className="block font-medium">{definition.display_name}</span><code className="text-xs text-muted-foreground">{definition.key}</code>{!available ? <span className="ml-2 text-xs text-destructive">unavailable</span> : null}</span></label>;
      })}</div> : <p className="text-sm text-muted-foreground">No AI-assignable fields are available. Enable AI assignment for a matter-owned metadata field first.</p>}
    </fieldset>
  );
}

function taskKey(value: string) {
  return value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^[^a-z]+/, "").slice(0, 100);
}

function shortHash(value: string) { return value.slice(0, 10); }
function sameIds(left: string[], right: string[]) { return left.length === right.length && left.every((value, index) => value === right[index]); }
function friendly(value: string) { return value.toLowerCase().replaceAll("_", " "); }
function arrayValue(value: unknown) { return Array.isArray(value) ? value : []; }
function recordValue(value: unknown) { return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined; }
function stringValue(value: unknown) { return typeof value === "string" ? value : undefined; }
function numberValue(value: unknown) { return typeof value === "number" ? value : undefined; }
function compilationError(report: Record<string, unknown>) {
  const direct = stringValue(report.error);
  if (direct) return direct;
  const first = arrayValue(report.errors)[0];
  return first && typeof first === "object" ? stringValue((first as Record<string, unknown>).message) : undefined;
}
