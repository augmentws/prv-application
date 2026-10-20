"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, LoaderCircle, MessageSquareText, Plus, Send } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { DocumentChatMessageContent } from "@/components/batch-chat-panel";
import { QueryError } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type {
  AgentConversationRead,
  AgentDefinitionRead,
  AgentMessageRead,
  AgentRunRead,
  AgentTurnCreated,
  MatterSearchRequest,
} from "@/generated/models";
import { useAgentChatStream } from "@/hooks/use-agent-chat-stream";
import { coreApi } from "@/lib/api-client";
import type { BatchDocumentReference } from "@/lib/document-references";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

export function normalizeSearchChatScope(search: MatterSearchRequest): MatterSearchRequest {
  return {
    query: search.query ?? null,
    search_mode: search.search_mode ?? "KEYWORD",
    minimum_similarity: search.minimum_similarity ?? null,
    candidate_limit: search.candidate_limit ?? null,
    query_fields: search.query_fields ?? [],
    filters: search.filters ?? [],
    facets: [],
    sort: [],
    offset: 0,
    size: 500,
  };
}

export function searchChatTitle(firstMessage: string): string {
  return Array.from(firstMessage.trim().replace(/\s+/g, " ")).slice(0, 50).join("");
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.entries(value).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export function SearchChatPanel({ matterId, search, resultCount, searchLoading = false, onOpenDocument }: {
  matterId: string;
  search: MatterSearchRequest;
  resultCount: number;
  searchLoading?: boolean;
  onOpenDocument: (reference: BatchDocumentReference) => void;
}) {
  const queryClient = useQueryClient();
  const [selectedConversationId, setSelectedConversationId] = useState("");
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState("");
  const scrollRef = useRef<HTMLDivElement>(null);
  const scope = useMemo(() => normalizeSearchChatScope(search), [search]);
  const scopeKey = useMemo(() => stableJson(scope), [scope]);
  const chatStream = useAgentChatStream({ matterId, workflowType: "SEARCH_CHAT" });
  const agents = useQuery({
    queryKey: ["matter-agents", matterId, "SEARCH_CHAT"],
    queryFn: () => coreApi<AgentDefinitionRead[]>(`/v1/matters/${matterId}/agents?workflow_type=SEARCH_CHAT`),
  });
  const conversations = useQuery({
    queryKey: ["search-chat-conversations", matterId],
    queryFn: () => coreApi<AgentConversationRead[]>(`/v1/matters/${matterId}/agent-conversations?workflow_type=SEARCH_CHAT`),
    refetchInterval: chatStream.pollingEnabled ? 2000 : false,
  });
  const scopedConversations = useMemo(
    () => (conversations.data ?? []).filter((conversation) => stableJson(conversation.search_definition) === scopeKey),
    [conversations.data, scopeKey],
  );
  const effectiveConversationId = creating ? "" : selectedConversationId || scopedConversations[0]?.id || "";
  const selectedConversation = scopedConversations.find((item) => item.id === effectiveConversationId);
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
    mutationFn: async (content: string) => {
      const conversation = await coreApi<AgentConversationRead>(`/v1/matters/${matterId}/agent-conversations`, {
        method: "POST",
        body: JSON.stringify({
          agent_definition_id: agents.data![0].id,
          title: searchChatTitle(content),
          workflow_type: "SEARCH_CHAT",
          search_definition: scope,
        }),
      });
      const turn = await coreApi<AgentTurnCreated>(`/v1/agent-conversations/${conversation.id}/turns`, {
        method: "POST",
        body: JSON.stringify({ message: content }),
      });
      return { conversation, turn };
    },
    onSuccess: ({ conversation: created, turn }) => {
      queryClient.setQueryData<AgentConversationRead[]>(["search-chat-conversations", matterId], (current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      queryClient.setQueryData<AgentMessageRead[]>(["agent-messages", created.id], [turn.message]);
      queryClient.setQueryData<AgentRunRead[]>(["agent-runs", created.id], [turn.run]);
      setSelectedConversationId(created.id);
      setCreating(false);
      setMessage("");
      toast.success("Search chat started.");
    },
    onError: (error) => {
      void queryClient.invalidateQueries({ queryKey: ["search-chat-conversations", matterId] });
      toast.error(error instanceof Error ? error.message : "The search chat or its first question could not be started.");
    },
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
    return <div className="grid min-h-0 flex-1 place-items-center p-5 text-center">
      <div className="w-full max-w-sm">
        <span className="mx-auto grid size-12 place-items-center rounded-full bg-agent/10 text-agent"><MessageSquareText className="size-5" /></span>
        <h3 className="mt-4 font-semibold">Chat with these search results</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">{searchLoading ? "Wait for the current search to finish before starting a conversation." : <>The conversation is fixed to the current {resultCount.toLocaleString()} search {resultCount === 1 ? "result" : "results"} and uses document text plus available summaries.</>}</p>
        {agents.isPending || conversations.isPending ? <LoaderCircle className="mx-auto mt-5 animate-spin text-muted-foreground" />
          : agents.data?.length ? <div className="mt-5 space-y-2 text-left"><Textarea value={message} maxLength={2000} disabled={startConversation.isPending} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); if (message.trim()) startConversation.mutate(message.trim()); } }} placeholder="Ask your first question…" aria-label="Starting message Search Chat" className="min-h-24 resize-none" /><p className="text-xs text-muted-foreground">The first 50 characters become the chat title.</p><div className="flex gap-2">{creating && scopedConversations.length ? <Button className="flex-1" variant="outline" onClick={() => { setCreating(false); setMessage(""); }}>Cancel</Button> : null}<Button className="flex-1" disabled={!message.trim() || !resultCount || searchLoading || startConversation.isPending} onClick={() => startConversation.mutate(message.trim())}>{startConversation.isPending ? <LoaderCircle className="animate-spin" /> : <Plus />}Start search chat</Button></div></div>
            : <p className="mt-5 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">The document chat agent is not configured.</p>}
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
      <div className="flex items-center justify-between gap-2"><div className="flex items-center gap-2 text-sm font-semibold text-agent"><Bot className="size-4" />Search Chat <span className="text-xs font-normal text-muted-foreground">{chatStream.state === "live" ? "Live" : chatStream.state === "fallback" ? "Polling" : chatStream.state === "offline" ? "Offline" : "Connecting"}</span></div><StatusBadge status={selectedConversation.status} /></div>
      <div className="flex gap-2"><Select value={effectiveConversationId} onValueChange={setSelectedConversationId}><SelectTrigger className="min-w-0 flex-1" aria-label="Previous search chats"><SelectValue /></SelectTrigger><SelectContent>{scopedConversations.map((conversation) => <SelectItem key={conversation.id} value={conversation.id}>{conversation.title ?? formatDate(conversation.created_at)}</SelectItem>)}</SelectContent></Select><Button size="sm" variant="outline" aria-label="New search chat" onClick={() => setCreating(true)}><Plus />New</Button></div>
      <p className="text-xs text-muted-foreground">Scope frozen to this search definition · currently {resultCount.toLocaleString()} results.</p>
    </div>
    <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4" aria-live="polite">
      {!messages.data?.length ? <div className="rounded-lg border border-dashed p-4 text-sm leading-6 text-muted-foreground">Ask about people, events, themes, communications, or patterns within these results.</div> : null}
      {messages.data?.map((item) => <div key={item.id} className={cn("flex", item.role === "USER" ? "justify-end" : "justify-start")}><div className={cn("max-w-[90%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-6 [overflow-wrap:anywhere]", item.role === "USER" ? "rounded-br-md bg-primary text-primary-foreground" : "rounded-bl-md border bg-card")}><DocumentChatMessageContent content={item.content} linkDocuments={item.role === "ASSISTANT"} onOpenDocument={onOpenDocument} /></div></div>)}
      {activeRun ? <div className="flex items-center gap-2 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin text-agent" />Searching these results…</div> : null}
      {latestRun?.status === "FAILED" ? <div className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">{latestRun.error_message ?? "The search chat run failed."}</div> : null}
    </div>
    <div className="border-t bg-muted/25 p-3"><Textarea value={message} disabled={activeRun || submitTurn.isPending} onChange={(event) => setMessage(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendMessage(); } }} placeholder="Ask about these results…" aria-label="Message Search Chat" className="min-h-20 resize-none" /><div className="mt-2 flex items-center justify-end"><Button size="sm" disabled={!message.trim() || activeRun || submitTurn.isPending} onClick={() => void sendMessage()}>{submitTurn.isPending ? <LoaderCircle className="animate-spin" /> : <Send />}Send</Button></div></div>
  </div>;
}
