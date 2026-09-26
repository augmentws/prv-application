"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, LoaderCircle, MessageSquareText, Plus, Send } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { QueryError } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  AgentConversationRead,
  AgentDefinitionRead,
  AgentMessageRead,
  AgentRunRead,
  AgentTurnCreated,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import type { BatchDocumentReference } from "@/lib/document-references";
import { normalizeParagraphReference } from "@/lib/document-references";
import { formatDate } from "@/lib/format";
import { useAgentChatStream } from "@/hooks/use-agent-chat-stream";
import { cn } from "@/lib/utils";

export function BatchChatPanel({ matterId, batchId, searchReady, onOpenDocument }: {
  matterId: string;
  batchId: string;
  searchReady: boolean;
  onOpenDocument: (reference: BatchDocumentReference) => void;
}) {
  const queryClient = useQueryClient();
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [creating, setCreating] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [message, setMessage] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const chatStream = useAgentChatStream({ matterId, workflowType: "BATCH_CHAT", reviewBatchId: batchId });
  const agents = useQuery({
    queryKey: ["matter-agents", matterId, "BATCH_CHAT"],
    queryFn: () => coreApi<AgentDefinitionRead[]>(`/v1/matters/${matterId}/agents?workflow_type=BATCH_CHAT`),
  });
  const conversations = useQuery({
    queryKey: ["batch-chat-conversations", matterId, batchId],
    queryFn: () => coreApi<AgentConversationRead[]>(`/v1/matters/${matterId}/agent-conversations?workflow_type=BATCH_CHAT&review_batch_id=${batchId}`),
    refetchInterval: chatStream.pollingEnabled ? 2000 : false,
  });
  const effectiveConversationId = creating ? "" : selectedConversationId || conversations.data?.[0]?.id || "";
  const selectedConversation = conversations.data?.find((item) => item.id === effectiveConversationId);
  const messages = useQuery({
    queryKey: ["agent-messages", effectiveConversationId],
    queryFn: () => coreApi<AgentMessageRead[]>(`/v1/agent-conversations/${effectiveConversationId}/messages`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: chatStream.pollingEnabled && effectiveConversationId ? 1500 : false,
  });
  const runs = useQuery({
    queryKey: ["agent-runs", effectiveConversationId],
    queryFn: () => coreApi<AgentRunRead[]>(`/v1/agent-conversations/${effectiveConversationId}/runs`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: chatStream.pollingEnabled && effectiveConversationId ? 1500 : false,
  });
  const latestRun = runs.data?.at(-1);
  const activeRun = Boolean(latestRun && ["QUEUED", "RUNNING"].includes(latestRun.status));

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [messages.data?.length, activeRun]);

  const startConversation = useMutation({
    mutationFn: (title: string) => coreApi<AgentConversationRead>(`/v1/matters/${matterId}/agent-conversations`, {
      method: "POST",
      body: JSON.stringify({
        agent_definition_id: agents.data![0].id,
        title,
        workflow_type: "BATCH_CHAT",
        review_batch_id: batchId,
      }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentConversationRead[]>(["batch-chat-conversations", matterId, batchId], (current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setSelectedConversationId(created.id);
      setCreating(false);
      setNewTitle("");
      toast.success("Batch chat started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The batch chat could not be started."),
  });
  const submitTurn = useMutation({
    mutationFn: (content: string) => coreApi<AgentTurnCreated>(`/v1/agent-conversations/${effectiveConversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({ message: content }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentMessageRead[]>(["agent-messages", effectiveConversationId], (current) => [...(current ?? []).filter((item) => item.id !== created.message.id), created.message]);
      queryClient.setQueryData<AgentRunRead[]>(["agent-runs", effectiveConversationId], (current) => [...(current ?? []).filter((item) => item.id !== created.run.id), created.run]);
      setMessage("");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The question could not be sent."),
  });

  const error = agents.error ?? conversations.error ?? messages.error ?? runs.error;
  if (error) return <div className="p-4"><QueryError message={error.message} /></div>;

  if (!selectedConversation) {
    return <div className="grid flex-1 place-items-center p-5 text-center">
      <div className="w-full max-w-sm">
        <span className="mx-auto grid size-12 place-items-center rounded-full bg-agent/10 text-agent"><MessageSquareText className="size-5" /></span>
        <h3 className="mt-4 font-semibold">Chat with this batch</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">Questions use semantic search limited to this batch, then answer from available document summary artifacts.</p>
        {!searchReady ? <p className="mt-5 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">Batch semantic search is not ready. Chat becomes available after the batch search projection is ready.</p>
          : agents.isPending || conversations.isPending ? <LoaderCircle className="mx-auto mt-5 animate-spin text-muted-foreground" />
            : agents.data?.length ? <div className="mt-5 space-y-2 text-left"><Input value={newTitle} maxLength={200} onChange={(event) => setNewTitle(event.target.value)} placeholder="Chat name" aria-label="Batch chat name" /><div className="flex gap-2">{creating && conversations.data?.length ? <Button className="flex-1" variant="outline" onClick={() => setCreating(false)}>Cancel</Button> : null}<Button className="flex-1" disabled={!newTitle.trim() || startConversation.isPending} onClick={() => startConversation.mutate(newTitle.trim())}>{startConversation.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}Start batch chat</Button></div></div>
            : <p className="mt-5 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">The Batch Chat Agent is not configured. Run the application bootstrap after deploying this version.</p>}
      </div>
    </div>;
  }

  async function sendMessage() {
    const content = message.trim();
    if (!content || activeRun) return;
    await submitTurn.mutateAsync(content);
  }

  return <div className="flex min-h-0 flex-1 flex-col">
    <div className="space-y-2 border-b p-3">
      <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-semibold text-agent"><Bot className="size-4" />Batch Chat Agent <span className="text-xs font-normal text-muted-foreground">{chatStream.state === "live" ? "Live" : chatStream.state === "fallback" ? "Polling" : chatStream.state === "offline" ? "Offline" : "Connecting"}</span></div><StatusBadge status={selectedConversation.status} /></div>
      <div className="flex gap-2"><Select value={effectiveConversationId} onValueChange={setSelectedConversationId}><SelectTrigger className="min-w-0 flex-1" aria-label="Batch chat"><SelectValue /></SelectTrigger><SelectContent>{conversations.data?.map((conversation) => <SelectItem key={conversation.id} value={conversation.id}>{conversation.title ?? formatDate(conversation.created_at)}</SelectItem>)}</SelectContent></Select><Button size="icon" variant="outline" aria-label="New batch chat" onClick={() => setCreating(true)}><Plus /></Button></div>
    </div>
    <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
      {!messages.data?.length ? <div className="rounded-lg border border-dashed p-4 text-sm leading-6 text-muted-foreground">Ask about people, events, themes, communications, or patterns in this batch. Answers are limited to documents with available summaries.</div> : null}
      {messages.data?.map((item) => <div key={item.id} className={cn("flex", item.role === "USER" ? "justify-end" : "justify-start")}><div className={cn("max-w-[90%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-6 [overflow-wrap:anywhere]", item.role === "USER" ? "rounded-br-md bg-primary text-primary-foreground" : "rounded-bl-md border bg-card")}><BatchChatMessageContent content={item.content} linkDocuments={item.role === "ASSISTANT"} onOpenDocument={onOpenDocument} /></div></div>)}
      {activeRun ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin text-agent" />Searching batch summaries…</div> : null}
      {latestRun?.status === "FAILED" ? <div className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{latestRun.error_message ?? "The batch chat run failed."}</div> : null}
    </div>
    <div className="border-t bg-muted/25 p-3"><Textarea value={message} disabled={activeRun || submitTurn.isPending} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendMessage(); } }} placeholder="Ask a question about this batch…" aria-label="Message the Batch Chat Agent" className="min-h-20 resize-none" /><div className="mt-2 flex items-center justify-between gap-2"><p className="text-xs text-muted-foreground">Answers currently use summary artifacts.</p><Button size="sm" disabled={!message.trim() || activeRun || submitTurn.isPending} onClick={() => void sendMessage()}>{submitTurn.isPending ? <LoaderCircle className="animate-spin" /> : <Send />}Send</Button></div></div>
  </div>;
}

function BatchChatMessageContent({ content, linkDocuments, onOpenDocument }: {
  content: string;
  linkDocuments: boolean;
  onOpenDocument: (reference: BatchDocumentReference) => void;
}) {
  if (!linkDocuments) return content;
  const pattern = /\[Document\s+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\s+([^\]]+))?\]/g;
  const parts = [];
  let cursor = 0;
  for (const match of content.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) parts.push(content.slice(cursor, index));
    const documentId = match[1];
    const paragraphReference = normalizeParagraphReference(match[2]);
    parts.push(<button key={`${index}:${documentId}`} type="button" className="rounded bg-primary/10 px-1.5 py-0.5 font-medium text-primary hover:bg-primary/20" onClick={() => onOpenDocument({ documentId, paragraphReference })}>{match[0]}</button>);
    cursor = index + match[0].length;
  }
  if (cursor < content.length) parts.push(content.slice(cursor));
  return parts.length ? parts : content;
}
