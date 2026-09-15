"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bot,
  Check,
  Clock3,
  FileText,
  History,
  LoaderCircle,
  MessageSquareText,
  Save,
  Send,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  AgentActionDecisionResult,
  AgentActionRequestRead,
  AgentConversationRead,
  AgentDefinitionRead,
  AgentMessageRead,
  AgentRunRead,
  AgentTurnCreated,
  MatterDefinitionRead,
  MatterDefinitionRevisionRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

type DraftSourceKind = "PASTE" | "MARKDOWN" | "TEXT" | "USER_EDIT";
type Decision = "APPROVE" | "REJECT";
interface DraftEdit {
  content: string;
  source: DraftSourceKind;
  sourceFilename: string | null;
  basedOnRevision: number | null;
}

export function MatterDefinitionPanel({ matterId }: { matterId: string }) {
  const queryClient = useQueryClient();
  const [draftEdit, setDraftEdit] = useState<DraftEdit | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [selectedConversationId, setSelectedConversationId] = useState<string>("");

  const definition = useQuery({
    queryKey: ["matter-definition", matterId],
    queryFn: () => coreApi<MatterDefinitionRead | null>(`/v1/matters/${matterId}/definition`),
    refetchInterval: 2000,
  });
  const revisions = useQuery({
    queryKey: ["matter-definition-revisions", matterId],
    queryFn: () => coreApi<MatterDefinitionRevisionRead[]>(`/v1/matters/${matterId}/definition/revisions`),
    refetchInterval: 3000,
  });
  const agents = useQuery({
    queryKey: ["matter-agents", matterId],
    queryFn: () => coreApi<AgentDefinitionRead[]>(`/v1/matters/${matterId}/agents`),
  });
  const conversations = useQuery({
    queryKey: ["agent-conversations", matterId],
    queryFn: () => coreApi<AgentConversationRead[]>(`/v1/matters/${matterId}/agent-conversations`),
    refetchInterval: 2000,
  });

  const effectiveConversationId = selectedConversationId || conversations.data?.[0]?.id || "";
  const selectedConversation = conversations.data?.find((item) => item.id === effectiveConversationId);
  const messages = useQuery({
    queryKey: ["agent-messages", effectiveConversationId],
    queryFn: () => coreApi<AgentMessageRead[]>(`/v1/agent-conversations/${effectiveConversationId}/messages`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: effectiveConversationId ? 1500 : false,
  });
  const runs = useQuery({
    queryKey: ["agent-runs", effectiveConversationId],
    queryFn: () => coreApi<AgentRunRead[]>(`/v1/agent-conversations/${effectiveConversationId}/runs`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: effectiveConversationId ? 1500 : false,
  });
  const actions = useQuery({
    queryKey: ["agent-actions", effectiveConversationId],
    queryFn: () => coreApi<AgentActionRequestRead[]>(`/v1/agent-conversations/${effectiveConversationId}/action-requests`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: effectiveConversationId ? 1500 : false,
  });

  const currentRevision = definition.data?.current_revision ?? null;
  const draft = draftEdit?.content ?? definition.data?.revision.content_markdown ?? "";
  const draftSource = draftEdit?.source ?? (definition.data ? "USER_EDIT" : "PASTE");
  const sourceFilename = draftEdit?.sourceFilename ?? null;
  const loadedRevision = draftEdit?.basedOnRevision ?? currentRevision;
  const dirty = draftEdit !== null;

  const saveDraft = useMutation({
    mutationFn: () => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/definition/revisions`, {
      method: "POST",
      body: JSON.stringify({
        content_markdown: draft,
        source_kind: draftSource,
        source_filename: sourceFilename,
        based_on_revision: loadedRevision,
      }),
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["matter-definition", matterId], updated);
      void queryClient.invalidateQueries({ queryKey: ["matter-definition-revisions", matterId] });
      setDraftEdit(null);
      toast.success(`Draft revision ${updated.current_revision} was saved.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The Matter Definition could not be saved."),
  });
  const publish = useMutation({
    mutationFn: (revision: number) => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/definition/revisions/${revision}/publish`, { method: "POST" }),
    onSuccess: (updated) => {
      queryClient.setQueryData(["matter-definition", matterId], updated);
      setPublishOpen(false);
      toast.success(`Revision ${updated.published_revision} is now the published guidance.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The Matter Definition could not be published."),
  });
  const startConversation = useMutation({
    mutationFn: (agentId: string) => coreApi<AgentConversationRead>(`/v1/matters/${matterId}/agent-conversations`, {
      method: "POST",
      body: JSON.stringify({ agent_definition_id: agentId, workflow_type: "MATTER_DEFINITION_SETUP" }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentConversationRead[]>(["agent-conversations", matterId], (current) => [created, ...(current ?? [])]);
      setSelectedConversationId(created.id);
      toast.success("Agent conversation started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The agent conversation could not be started."),
  });
  const submitTurn = useMutation({
    mutationFn: (message: string) => coreApi<AgentTurnCreated>(`/v1/agent-conversations/${selectedConversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentMessageRead[]>(["agent-messages", effectiveConversationId], (current) => [...(current ?? []), created.message]);
      queryClient.setQueryData<AgentRunRead[]>(["agent-runs", effectiveConversationId], (current) => [...(current ?? []), created.run]);
      void queryClient.invalidateQueries({ queryKey: ["agent-conversations", matterId] });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The message could not be sent."),
  });
  const decideAction = useMutation({
    mutationFn: ({ actionId, decision, reason }: { actionId: string; decision: Decision; reason?: string }) =>
      coreApi<AgentActionDecisionResult>(`/v1/agent-action-requests/${actionId}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision, reason: reason || null }),
      }),
    onSuccess: (result) => {
      queryClient.setQueryData<AgentActionRequestRead[]>(["agent-actions", effectiveConversationId], (current) =>
        current?.map((item) => item.id === result.action_request.id ? result.action_request : item),
      );
      if (result.resumed_run) {
        queryClient.setQueryData<AgentRunRead[]>(["agent-runs", effectiveConversationId], (current) => [...(current ?? []), result.resumed_run!]);
      }
      void queryClient.invalidateQueries({ queryKey: ["agent-conversations", matterId] });
      toast.success(result.decision.decision === "APPROVE" ? "Change approved. The agent is applying it." : "Change rejected. The agent will continue without it.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The decision could not be recorded."),
  });

  const newerRevisionAvailable = dirty && currentRevision !== null && loadedRevision !== currentRevision;

  async function importTextFile(file: File) {
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (extension !== "md" && extension !== "markdown" && extension !== "txt") {
      toast.error("This slice accepts Markdown and plain-text files. DOCX ingestion is not available yet.");
      return;
    }
    const content = await file.text();
    setDraftEdit({
      content,
      source: extension === "txt" ? "TEXT" : "MARKDOWN",
      sourceFilename: file.name,
      basedOnRevision: definition.data?.current_revision ?? null,
    });
  }

  if (definition.isPending || revisions.isPending || agents.isPending || conversations.isPending) return <TableLoading />;
  const error = definition.error ?? revisions.error ?? agents.error ?? conversations.error;
  if (error) return <QueryError message={error.message} />;

  return (
    <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(23rem,0.85fr)]">
      <section className="min-w-0 space-y-4" aria-labelledby="matter-definition-heading">
        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-start justify-between gap-3 border-b p-5">
            <div>
              <div className="mb-1 flex items-center gap-2">
                <FileText className="size-4 text-primary" />
                <h2 id="matter-definition-heading" className="font-semibold">Reviewer guidance</h2>
              </div>
              <p className="text-sm text-muted-foreground">The saved Markdown is used by human reviewers and future document-coding agents.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {currentRevision ? <Badge variant="outline">Draft r{currentRevision}</Badge> : <Badge variant="outline">No saved draft</Badge>}
              {definition.data?.published_revision ? <Badge variant="active">Published r{definition.data.published_revision}</Badge> : <Badge variant="accent">Not published</Badge>}
            </div>
          </div>

          {newerRevisionAvailable ? (
            <div role="alert" className="border-b border-conflict/30 bg-conflict/10 px-5 py-3 text-sm text-conflict">
              A newer revision was created while you were editing. Your unsaved text is preserved; reload the latest draft before saving.
            </div>
          ) : null}

          <div className="p-5">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                {sourceFilename ? `Imported from ${sourceFilename}` : dirty ? "Unsaved changes" : "Current draft"}
              </p>
              <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border bg-background px-3 py-2 text-xs font-semibold hover:bg-muted focus-within:ring-2 focus-within:ring-ring">
                <Upload className="size-3.5" />
                Import .md or .txt
                <input
                  type="file"
                  accept=".md,.markdown,.txt,text/markdown,text/plain"
                  className="sr-only"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) void importTextFile(file);
                    event.target.value = "";
                  }}
                />
              </label>
            </div>
            <Textarea
              aria-label="Matter Definition Markdown"
              value={draft}
              onChange={(event) => {
                setDraftEdit((current) => ({
                  content: event.target.value,
                  source: current?.source ?? (definition.data ? "USER_EDIT" : "PASTE"),
                  sourceFilename: current?.sourceFilename ?? null,
                  basedOnRevision: current?.basedOnRevision ?? definition.data?.current_revision ?? null,
                }));
              }}
              placeholder="# Review guidance\n\nDescribe the coding fields, definitions, examples, and decision rules."
              className="min-h-[34rem] resize-y font-mono text-[13px] leading-6"
            />
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <p className="text-xs text-muted-foreground">Saving creates an immutable draft revision. Publishing is a separate action.</p>
              <div className="flex flex-wrap gap-2">
                {newerRevisionAvailable ? (
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => {
                      setDraftEdit(null);
                    }}
                  >
                    Reload latest
                  </Button>
                ) : null}
                <Button
                  type="button"
                  variant="outline"
                  disabled={!definition.data || definition.data.published_revision === definition.data.current_revision || dirty}
                  onClick={() => setPublishOpen(true)}
                >
                  <Sparkles />Publish current draft
                </Button>
                <Button type="button" disabled={!dirty || !draft.trim() || newerRevisionAvailable || saveDraft.isPending} onClick={() => saveDraft.mutate()}>
                  {saveDraft.isPending ? <LoaderCircle className="animate-spin" /> : <Save />}
                  Save draft
                </Button>
              </div>
            </div>
          </div>
        </Card>

        <RevisionHistory revisions={revisions.data ?? []} currentRevision={currentRevision} publishedRevision={definition.data?.published_revision ?? null} />
      </section>

      <AgentWorkspace
        matterId={matterId}
        agents={agents.data ?? []}
        conversations={conversations.data ?? []}
        selectedConversation={selectedConversation}
        selectedConversationId={effectiveConversationId}
        messages={messages.data ?? []}
        runs={runs.data ?? []}
        actions={actions.data ?? []}
        loadingConversation={messages.isPending || runs.isPending || actions.isPending}
        conversationError={messages.error?.message ?? runs.error?.message ?? actions.error?.message}
        starting={startConversation.isPending}
        submitting={submitTurn.isPending}
        deciding={decideAction.isPending}
        onSelectConversation={setSelectedConversationId}
        onStartConversation={(agentId) => startConversation.mutate(agentId)}
        onSubmitTurn={(message) => submitTurn.mutateAsync(message).then(() => undefined)}
        onDecision={(actionId, decision, reason) => decideAction.mutateAsync({ actionId, decision, reason }).then(() => undefined)}
      />

      <Dialog open={publishOpen} onOpenChange={setPublishOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Publish revision {currentRevision}?</DialogTitle>
            <DialogDescription>This revision becomes the active reviewer guidance. The draft history remains unchanged and a later revision can be published separately.</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setPublishOpen(false)}>Cancel</Button>
            <Button type="button" disabled={!currentRevision || publish.isPending} onClick={() => currentRevision && publish.mutate(currentRevision)}>
              {publish.isPending ? <LoaderCircle className="animate-spin" /> : <Sparkles />}
              Publish revision
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function AgentWorkspace({
  agents,
  conversations,
  selectedConversation,
  selectedConversationId,
  messages,
  runs,
  actions,
  loadingConversation,
  conversationError,
  starting,
  submitting,
  deciding,
  onSelectConversation,
  onStartConversation,
  onSubmitTurn,
  onDecision,
}: {
  matterId: string;
  agents: AgentDefinitionRead[];
  conversations: AgentConversationRead[];
  selectedConversation?: AgentConversationRead;
  selectedConversationId: string;
  messages: AgentMessageRead[];
  runs: AgentRunRead[];
  actions: AgentActionRequestRead[];
  loadingConversation: boolean;
  conversationError?: string;
  starting: boolean;
  submitting: boolean;
  deciding: boolean;
  onSelectConversation: (id: string) => void;
  onStartConversation: (agentId: string) => void;
  onSubmitTurn: (message: string) => Promise<void>;
  onDecision: (actionId: string, decision: Decision, reason?: string) => Promise<void>;
}) {
  const [selectedAgentId, setSelectedAgentId] = useState(agents[0]?.id ?? "");
  const [message, setMessage] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const effectiveAgentId = selectedAgentId || agents[0]?.id || "";
  const pendingActions = actions.filter((action) => action.status === "PENDING");
  const latestRun = runs.at(-1);
  const activeRun = Boolean(latestRun && ["QUEUED", "RUNNING"].includes(latestRun.status)) || pendingActions.length > 0;
  const failedRun = latestRun?.status === "FAILED" ? latestRun : undefined;

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages.length, pendingActions.length]);

  async function sendMessage() {
    const content = message.trim();
    if (!content) return;
    await onSubmitTurn(content);
    setMessage("");
  }

  return (
    <Card className="flex min-h-[46rem] min-w-0 flex-col overflow-hidden xl:sticky xl:top-5 xl:max-h-[calc(100vh-2.5rem)]" aria-labelledby="agent-heading">
      <div className="border-b bg-agent/5 p-5">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="mb-1 flex items-center gap-2 text-agent">
              <Bot className="size-4" />
              <h2 id="agent-heading" className="font-semibold">Matter Definition agent</h2>
            </div>
            <p className="text-sm text-muted-foreground">Compare coding fields, improve guidance, and approve every proposed change.</p>
          </div>
          {selectedConversation ? <StatusBadge status={selectedConversation.status} /> : null}
        </div>

        {conversations.length ? (
          <div className="mt-4 flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <label className="mb-1 block text-xs font-medium text-muted-foreground" htmlFor="agent-conversation">Conversation</label>
              <Select value={selectedConversationId} onValueChange={onSelectConversation}>
                <SelectTrigger id="agent-conversation"><SelectValue placeholder="Select a conversation" /></SelectTrigger>
                <SelectContent>
                  {conversations.map((conversation) => (
                    <SelectItem key={conversation.id} value={conversation.id}>
                      {formatDate(conversation.created_at)} · {conversation.status.toLowerCase().replaceAll("_", " ")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {agents.length ? <Button type="button" size="sm" variant="outline" disabled={starting} onClick={() => onStartConversation(effectiveAgentId)}>New</Button> : null}
          </div>
        ) : null}
      </div>

      {!selectedConversation ? (
        <div className="grid flex-1 place-items-center p-6">
          <div className="max-w-sm text-center">
            <span className="mx-auto grid size-12 place-items-center rounded-full bg-agent/10 text-agent"><MessageSquareText className="size-5" /></span>
            <h3 className="mt-4 font-semibold">Start guided setup</h3>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">The agent reads the saved draft and matter metadata. Any proposed edit appears as a separate approval card.</p>
            {agents.length ? (
              <div className="mt-5 space-y-3 text-left">
                <Select value={effectiveAgentId} onValueChange={setSelectedAgentId}>
                  <SelectTrigger aria-label="Agent"><SelectValue placeholder="Select an agent" /></SelectTrigger>
                  <SelectContent>{agents.map((agent) => <SelectItem key={agent.id} value={agent.id}>{agent.name}</SelectItem>)}</SelectContent>
                </Select>
                <Button className="w-full" type="button" disabled={!effectiveAgentId || starting} onClick={() => onStartConversation(effectiveAgentId)}>
                  {starting ? <LoaderCircle className="animate-spin" /> : <Sparkles />}
                  Start agent conversation
                </Button>
              </div>
            ) : (
              <div role="status" className="mt-5 rounded-lg border border-warning/30 bg-warning/10 p-4 text-left text-sm text-warning">
                No active, published agent is available for this matter. A root or tenant administrator must publish a Matter Definition agent first.
              </div>
            )}
          </div>
        </div>
      ) : loadingConversation ? <TableLoading /> : conversationError ? <QueryError message={conversationError} /> : (
        <>
          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-5" aria-live="polite">
            {!messages.length ? (
              <div className="rounded-xl border border-dashed p-5 text-sm leading-6 text-muted-foreground">
                Ask the agent to review the current guidance, identify coding fields, compare enum values, or suggest clearer instructions.
              </div>
            ) : null}
            {messages.map((item) => <ChatMessage key={item.id} message={item} />)}
            {pendingActions.map((action) => (
              <ApprovalCard key={action.id} action={action} deciding={deciding} onDecision={onDecision} />
            ))}
            {activeRun && !pendingActions.length ? (
              <div role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
                <LoaderCircle className="size-4 animate-spin text-agent" />The agent is working…
              </div>
            ) : null}
            {failedRun ? <div role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{failedRun.error_message ?? "The agent run failed."}</div> : null}
          </div>
          <div className="border-t bg-muted/25 p-4">
            {pendingActions.length ? <p className="mb-2 text-xs font-medium text-warning">Review the pending change before sending another message.</p> : null}
            <Textarea
              aria-label="Message the Matter Definition agent"
              value={message}
              disabled={activeRun || submitting || pendingActions.length > 0}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendMessage();
                }
              }}
              placeholder="Ask the agent to review the current guidance…"
              className="min-h-24 resize-none"
            />
            <div className="mt-2 flex items-center justify-between gap-3">
              <p className="text-xs text-muted-foreground">Enter to send · Shift+Enter for a new line</p>
              <Button type="button" size="sm" disabled={!message.trim() || activeRun || submitting || pendingActions.length > 0} onClick={() => void sendMessage()}>
                {submitting ? <LoaderCircle className="animate-spin" /> : <Send />}
                Send
              </Button>
            </div>
          </div>
        </>
      )}
    </Card>
  );
}

function ChatMessage({ message }: { message: AgentMessageRead }) {
  const user = message.role === "USER";
  const agent = message.role === "ASSISTANT";
  return (
    <div className={cn("flex", user ? "justify-end" : "justify-start")}>
      <div className={cn(
        "max-w-[88%] rounded-2xl px-4 py-3 text-sm leading-6",
        user ? "rounded-br-md bg-primary text-primary-foreground" : agent ? "rounded-bl-md border bg-card" : "rounded-lg bg-muted text-muted-foreground",
      )}>
        {!user ? <p className="mb-1 text-[11px] font-bold uppercase tracking-[0.08em] text-agent">{agent ? "Agent" : message.role.toLowerCase()}</p> : null}
        <div className="whitespace-pre-wrap [overflow-wrap:anywhere]">{message.content}</div>
      </div>
    </div>
  );
}

function ApprovalCard({ action, deciding, onDecision }: {
  action: AgentActionRequestRead;
  deciding: boolean;
  onDecision: (actionId: string, decision: Decision, reason?: string) => Promise<void>;
}) {
  const [reason, setReason] = useState("");
  const args = action.arguments as Record<string, unknown>;
  const proposedContent = typeof args.content_markdown === "string" ? args.content_markdown : null;
  const proposalReason = typeof args.reason === "string" ? args.reason : null;
  const basedOn = typeof args.based_on_revision === "number" ? args.based_on_revision : null;

  return (
    <section className="rounded-xl border border-warning/40 bg-warning/8 p-4" aria-labelledby={`approval-${action.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[11px] font-bold uppercase tracking-[0.08em] text-warning">Approval required</p>
          <h3 id={`approval-${action.id}`} className="mt-1 font-semibold">{action.summary}</h3>
        </div>
        <Clock3 className="size-4 shrink-0 text-warning" />
      </div>
      {proposalReason ? <p className="mt-3 text-sm leading-6"><span className="font-medium">Reason:</span> {proposalReason}</p> : null}
      {basedOn ? <p className="mt-1 text-xs text-muted-foreground">Based on draft revision {basedOn}</p> : null}
      {proposedContent ? (
        <details className="mt-3 rounded-lg border bg-background">
          <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">Review proposed Markdown</summary>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t p-3 text-xs leading-5 [overflow-wrap:anywhere]">{proposedContent}</pre>
        </details>
      ) : null}
      <Textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Optional note for the agent" className="mt-3 min-h-16 resize-none" />
      <div className="mt-3 flex justify-end gap-2">
        <Button type="button" size="sm" variant="outline" disabled={deciding} onClick={() => void onDecision(action.id, "REJECT", reason)}><X />Reject</Button>
        <Button type="button" size="sm" disabled={deciding} onClick={() => void onDecision(action.id, "APPROVE", reason)}><Check />Approve change</Button>
      </div>
    </section>
  );
}

function RevisionHistory({ revisions, currentRevision, publishedRevision }: {
  revisions: MatterDefinitionRevisionRead[];
  currentRevision: number | null;
  publishedRevision: number | null;
}) {
  return (
    <Card className="p-5">
      <div className="mb-4 flex items-center gap-2"><History className="size-4 text-primary" /><h2 className="font-semibold">Revision history</h2></div>
      {!revisions.length ? <p className="text-sm text-muted-foreground">No revisions have been saved yet.</p> : (
        <ol className="divide-y">
          {revisions.slice(0, 8).map((revision) => (
            <li key={revision.id} className="flex flex-wrap items-center justify-between gap-3 py-3 text-sm first:pt-0 last:pb-0">
              <div>
                <span className="font-semibold">Revision {revision.revision}</span>
                <span className="ml-2 text-muted-foreground">{revision.source_kind.toLowerCase().replaceAll("_", " ")} · {formatDate(revision.created_at)}</span>
              </div>
              <div className="flex gap-1.5">
                {revision.revision === currentRevision ? <Badge variant="outline">current</Badge> : null}
                {revision.revision === publishedRevision ? <Badge variant="active">published</Badge> : null}
                {revision.agent_run_id ? <Badge variant="accent">agent</Badge> : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
