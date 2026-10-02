"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import DiffMatchPatch from "diff-match-patch";
import {
  Archive,
  ArchiveRestore,
  Check,
  ChevronDown,
  ClipboardCheck,
  Clock3,
  Copy,
  FileText,
  GitCompareArrows,
  History,
  LoaderCircle,
  MessageSquareText,
  PencilLine,
  Plus,
  Save,
  Send,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { MatterDefinitionAssessmentPanel } from "@/components/matter-definition-assessment-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
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
import { type AgentChatStreamState, useAgentChatStream } from "@/hooks/use-agent-chat-stream";
import { cn } from "@/lib/utils";

type DraftSourceKind = "PASTE" | "MARKDOWN" | "TEXT" | "USER_EDIT";
type Decision = "APPROVE" | "REJECT";
type MatterDefinitionSidePanel = "agent" | "assessment";
interface DraftEdit {
  content: string;
  source: DraftSourceKind;
  sourceFilename: string | null;
  basedOnRevision: number | null;
}

export function MatterDefinitionPanel({ matterId }: { matterId: string }) {
  const queryClient = useQueryClient();
  const [selectedGuidanceId, setSelectedGuidanceId] = useState("");
  const [guidanceDialog, setGuidanceDialog] = useState<"create" | "clone" | "edit" | null>(null);
  const [guidanceKey, setGuidanceKey] = useState("");
  const [guidanceName, setGuidanceName] = useState("");
  const [guidanceDescription, setGuidanceDescription] = useState("");
  const [guidanceInitialContent, setGuidanceInitialContent] = useState("");
  const [guidanceInitialSource, setGuidanceInitialSource] = useState<DraftSourceKind>("PASTE");
  const [guidanceInitialFilename, setGuidanceInitialFilename] = useState<string | null>(null);
  const [draftEdit, setDraftEdit] = useState<DraftEdit | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [selectedConversationId, setSelectedConversationId] = useState<string>("");
  const [sidePanel, setSidePanel] = useState<MatterDefinitionSidePanel>("agent");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selectedRevisionNumber, setSelectedRevisionNumber] = useState<number | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [toolHeaderElement, setToolHeaderElement] = useState<HTMLDivElement | null>(null);
  const sidePanelId = useId();
  const chatStream = useAgentChatStream({ matterId, workflowType: "MATTER_DEFINITION_SETUP" });

  const guidance = useQuery({
    queryKey: ["matter-guidance", matterId],
    queryFn: () => coreApi<MatterDefinitionRead[]>(`/v1/matters/${matterId}/guidance`),
    refetchInterval: 2000,
  });
  const effectiveGuidanceId = selectedGuidanceId
    || guidance.data?.find((item) => item.status === "ACTIVE")?.id
    || guidance.data?.[0]?.id
    || "";
  const selectedDefinition = guidance.data?.find((item) => item.id === effectiveGuidanceId) ?? null;
  const definition = {
    data: selectedDefinition,
    isPending: guidance.isPending,
    error: guidance.error,
  };
  const revisions = useQuery({
    queryKey: ["matter-guidance-revisions", matterId, effectiveGuidanceId],
    queryFn: () => coreApi<MatterDefinitionRevisionRead[]>(`/v1/matters/${matterId}/guidance/${effectiveGuidanceId}/revisions`),
    enabled: Boolean(effectiveGuidanceId),
    refetchInterval: 3000,
  });
  const agents = useQuery({
    queryKey: ["matter-agents", matterId],
    queryFn: () => coreApi<AgentDefinitionRead[]>(`/v1/matters/${matterId}/agents`),
  });
  const conversationListPath = `/v1/matters/${matterId}/agent-conversations?workflow_type=MATTER_DEFINITION_SETUP${effectiveGuidanceId ? `&guidance_id=${effectiveGuidanceId}` : ""}`;
  const conversations = useQuery({
    queryKey: ["agent-conversations", matterId, effectiveGuidanceId],
    queryFn: () => coreApi<AgentConversationRead[]>(conversationListPath),
    refetchInterval: chatStream.pollingEnabled ? 2000 : false,
  });

  const effectiveConversationId = selectedConversationId || conversations.data?.[0]?.id || "";
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
  const actions = useQuery({
    queryKey: ["agent-actions", effectiveConversationId],
    queryFn: () => coreApi<AgentActionRequestRead[]>(`/v1/agent-conversations/${effectiveConversationId}/action-requests`),
    enabled: Boolean(effectiveConversationId),
    refetchInterval: chatStream.pollingEnabled && effectiveConversationId ? 1500 : false,
  });

  const currentRevision = definition.data?.current_revision ?? null;
  const selectedRevision = selectedRevisionNumber === null
    ? null
    : revisions.data?.find((revision) => revision.revision === selectedRevisionNumber) ?? null;
  const draft = draftEdit?.content ?? definition.data?.revision.content_markdown ?? "";
  const displayedContent = selectedRevision?.content_markdown ?? draft;
  const draftSource = draftEdit?.source ?? (definition.data ? "USER_EDIT" : "PASTE");
  const sourceFilename = draftEdit?.sourceFilename ?? null;
  const loadedRevision = draftEdit?.basedOnRevision ?? currentRevision;
  const dirty = draftEdit !== null;

  const updateGuidanceCache = (updated: MatterDefinitionRead) => {
    queryClient.setQueryData<MatterDefinitionRead[]>(["matter-guidance", matterId], (current) =>
      current?.map((item) => item.id === updated.id ? updated : item),
    );
  };

  const saveDraft = useMutation({
    mutationFn: () => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/guidance/${effectiveGuidanceId}/revisions`, {
      method: "POST",
      body: JSON.stringify({
        content_markdown: draft,
        source_kind: draftSource,
        source_filename: sourceFilename,
        based_on_revision: loadedRevision,
      }),
    }),
    onSuccess: (updated) => {
      updateGuidanceCache(updated);
      void queryClient.invalidateQueries({ queryKey: ["matter-guidance-revisions", matterId, effectiveGuidanceId] });
      setDraftEdit(null);
      toast.success(`Draft revision ${updated.current_revision} was saved.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The Matter Definition could not be saved."),
  });
  const publish = useMutation({
    mutationFn: (revision: number) => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/guidance/${effectiveGuidanceId}/revisions/${revision}/publish`, { method: "POST" }),
    onSuccess: (updated) => {
      updateGuidanceCache(updated);
      setPublishOpen(false);
      toast.success(`Revision ${updated.published_revision} is now the published guidance.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The Matter Definition could not be published."),
  });
  const createGuidance = useMutation({
    mutationFn: () => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/guidance`, {
      method: "POST",
      body: JSON.stringify({
        key: guidanceKey.trim(),
        name: guidanceName.trim(),
        description: guidanceDescription.trim() || null,
        content_markdown: guidanceInitialContent.trim(),
        source_kind: guidanceInitialSource,
        source_filename: guidanceInitialFilename,
      }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterDefinitionRead[]>(["matter-guidance", matterId], (current) => [...(current ?? []), created]);
      setSelectedGuidanceId(created.id);
      resetSelectedGuidanceState();
      setGuidanceDialog(null);
      toast.success(`${created.name} was created.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Review Guidance could not be created."),
  });
  const cloneGuidance = useMutation({
    mutationFn: () => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/guidance/${effectiveGuidanceId}/clone`, {
      method: "POST",
      body: JSON.stringify({
        key: guidanceKey.trim(),
        name: guidanceName.trim(),
        description: guidanceDescription.trim() || null,
        source_revision: selectedRevisionNumber ?? definition.data?.current_revision ?? null,
      }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<MatterDefinitionRead[]>(["matter-guidance", matterId], (current) => [...(current ?? []), created]);
      setSelectedGuidanceId(created.id);
      resetSelectedGuidanceState();
      setGuidanceDialog(null);
      toast.success(`${created.name} was cloned.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Review Guidance could not be cloned."),
  });
  const updateGuidance = useMutation({
    mutationFn: () => coreApi<MatterDefinitionRead>(`/v1/matters/${matterId}/guidance/${effectiveGuidanceId}`, {
      method: "PATCH",
      body: JSON.stringify({
        name: guidanceName.trim(),
        description: guidanceDescription.trim() || null,
      }),
    }),
    onSuccess: (updated) => {
      updateGuidanceCache(updated);
      setGuidanceDialog(null);
      toast.success("Review Guidance details were updated.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Review Guidance could not be updated."),
  });
  const setGuidanceStatus = useMutation({
    mutationFn: (nextStatus: "ARCHIVED" | "ACTIVE") => coreApi<MatterDefinitionRead>(
      `/v1/matters/${matterId}/guidance/${effectiveGuidanceId}/${nextStatus === "ARCHIVED" ? "archive" : "restore"}`,
      { method: "POST" },
    ),
    onSuccess: (updated) => {
      updateGuidanceCache(updated);
      toast.success(updated.status === "ARCHIVED" ? `${updated.name} was archived.` : `${updated.name} was restored.`);
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Review Guidance status could not be changed."),
  });
  const startConversation = useMutation({
    mutationFn: ({ agentId, title }: { agentId: string; title: string }) => coreApi<AgentConversationRead>(`/v1/matters/${matterId}/agent-conversations`, {
      method: "POST",
      body: JSON.stringify({
        agent_definition_id: agentId,
        title,
        workflow_type: "MATTER_DEFINITION_SETUP",
        matter_definition_id: effectiveGuidanceId || null,
      }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentConversationRead[]>(["agent-conversations", matterId, effectiveGuidanceId], (current) => [created, ...(current ?? []).filter((item) => item.id !== created.id)]);
      setSelectedConversationId(created.id);
      toast.success("Chat started.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The chat could not be started."),
  });
  const renameConversation = useMutation({
    mutationFn: ({ conversationId, title }: { conversationId: string; title: string }) => coreApi<AgentConversationRead>(`/v1/agent-conversations/${conversationId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData<AgentConversationRead[]>(["agent-conversations", matterId, effectiveGuidanceId], (current) =>
        current?.map((conversation) => conversation.id === updated.id ? updated : conversation),
      );
      toast.success("Chat renamed.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The chat could not be renamed."),
  });
  const submitTurn = useMutation({
    mutationFn: (message: string) => coreApi<AgentTurnCreated>(`/v1/agent-conversations/${effectiveConversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
    onSuccess: (created) => {
      queryClient.setQueryData<AgentMessageRead[]>(["agent-messages", effectiveConversationId], (current) => [...(current ?? []).filter((item) => item.id !== created.message.id), created.message]);
      queryClient.setQueryData<AgentRunRead[]>(["agent-runs", effectiveConversationId], (current) => [...(current ?? []).filter((item) => item.id !== created.run.id), created.run]);
      void queryClient.invalidateQueries({ queryKey: ["agent-conversations", matterId, effectiveGuidanceId] });
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
        queryClient.setQueryData<AgentRunRead[]>(["agent-runs", effectiveConversationId], (current) => [...(current ?? []).filter((item) => item.id !== result.resumed_run!.id), result.resumed_run!]);
      }
      void queryClient.invalidateQueries({ queryKey: ["agent-conversations", matterId, effectiveGuidanceId] });
      toast.success(result.decision.decision === "APPROVE" ? "Change approved. The agent is applying it." : "Change rejected. The agent will continue without it.");
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "The decision could not be recorded."),
  });

  const newerRevisionAvailable = dirty && currentRevision !== null && loadedRevision !== currentRevision;

  function resetSelectedGuidanceState() {
    setDraftEdit(null);
    setSelectedConversationId("");
    setSelectedRevisionNumber(null);
    setHistoryOpen(false);
    setCompareOpen(false);
  }

  function selectGuidance(id: string) {
    if (id === effectiveGuidanceId) return;
    if (dirty) toast.info("Unsaved changes were discarded when you changed guidance profiles.");
    resetSelectedGuidanceState();
    setSelectedGuidanceId(id);
  }

  function suggestedKey(value: string) {
    return value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/^[^a-z]+/, "")
      .slice(0, 100);
  }

  function openGuidanceDialog(mode: "create" | "clone" | "edit") {
    setGuidanceDialog(mode);
    if (mode === "edit" && definition.data) {
      setGuidanceKey(definition.data.key);
      setGuidanceName(definition.data.name);
      setGuidanceDescription(definition.data.description ?? "");
      setGuidanceInitialContent("");
      setGuidanceInitialSource("PASTE");
      setGuidanceInitialFilename(null);
      return;
    }
    const name = mode === "clone" && definition.data ? `${definition.data.name} copy` : "";
    setGuidanceName(name);
    setGuidanceKey(suggestedKey(name));
    setGuidanceDescription(mode === "clone" ? definition.data?.description ?? "" : "");
    setGuidanceInitialContent(mode === "create" ? "# Reviewer guidance\n\n" : "");
    setGuidanceInitialSource("PASTE");
    setGuidanceInitialFilename(null);
  }

  async function importTextFile(file: File) {
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (extension !== "md" && extension !== "markdown" && extension !== "txt") {
      toast.error("This slice accepts Markdown and plain-text files. DOCX ingestion is not available yet.");
      return;
    }
    const content = await file.text();
    const source = extension === "txt" ? "TEXT" : "MARKDOWN";
    if (!definition.data) {
      const importedName = file.name.replace(/\.(?:md|markdown|txt)$/i, "");
      setGuidanceName(importedName);
      setGuidanceKey(suggestedKey(importedName));
      setGuidanceDescription("");
      setGuidanceInitialContent(content);
      setGuidanceInitialSource(source);
      setGuidanceInitialFilename(file.name);
      setGuidanceDialog("create");
      return;
    }
    setDraftEdit({
      content,
      source,
      sourceFilename: file.name,
      basedOnRevision: definition.data?.current_revision ?? null,
    });
  }

  if (definition.isPending || revisions.isLoading || agents.isPending || conversations.isPending) return <TableLoading />;
  const error = definition.error ?? revisions.error ?? agents.error ?? conversations.error;
  if (error) return <QueryError message={error.message} />;

  return (
    <div className="matter-definition-workspace grid min-w-0 gap-5" role="region" aria-label="Matter Definition workspace">
      <section className="matter-definition-guidance-frame min-h-0 min-w-0" aria-labelledby="matter-definition-heading">
        <div className="grid h-full min-h-0 grid-cols-[13rem_minmax(0,1fr)] gap-3">
        <Card className="flex min-h-0 flex-col overflow-hidden" aria-label="Review Guidance profiles">
          <div className="flex min-h-14 items-center justify-between gap-2 border-b px-3">
            <p className="text-xs font-bold uppercase tracking-[0.08em] text-muted-foreground">Guidance</p>
            <Button type="button" size="icon" className="size-8" aria-label="Create Review Guidance" onClick={() => openGuidanceDialog("create")}>
              <Plus />
            </Button>
          </div>
          <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2">
            {(guidance.data ?? []).map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={item.id === effectiveGuidanceId}
                onClick={() => selectGuidance(item.id)}
                className={cn(
                  "w-full rounded-md border px-3 py-2 text-left outline-none transition hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring",
                  item.id === effectiveGuidanceId && "border-primary/40 bg-primary/8",
                  item.status === "ARCHIVED" && "opacity-65",
                )}
              >
                <span className="block truncate text-sm font-semibold">{item.name}</span>
                <span className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
                  <span>r{item.current_revision}</span>
                  <span>{item.status === "ARCHIVED" ? "archived" : item.published_revision ? `published r${item.published_revision}` : "draft"}</span>
                </span>
              </button>
            ))}
            {!guidance.data?.length ? (
              <p className="p-2 text-xs leading-5 text-muted-foreground">Create guidance for a review stage or purpose.</p>
            ) : null}
          </div>
          {definition.data ? (
            <div className="grid grid-cols-3 gap-1 border-t p-2">
              <Button type="button" size="icon" variant="ghost" aria-label="Edit Review Guidance details" onClick={() => openGuidanceDialog("edit")}><PencilLine /></Button>
              <Button type="button" size="icon" variant="ghost" aria-label="Clone Review Guidance" onClick={() => openGuidanceDialog("clone")}><Copy /></Button>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                aria-label={definition.data.status === "ARCHIVED" ? "Restore Review Guidance" : "Archive Review Guidance"}
                disabled={setGuidanceStatus.isPending}
                onClick={() => setGuidanceStatus.mutate(definition.data?.status === "ARCHIVED" ? "ACTIVE" : "ARCHIVED")}
              >
                {definition.data.status === "ARCHIVED" ? <ArchiveRestore /> : <Archive />}
              </Button>
            </div>
          ) : null}
        </Card>
        <Card className="matter-definition-guidance-card flex min-h-0 flex-col overflow-hidden">
          <div className="flex min-h-14 shrink-0 flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
            <div className="flex min-w-0 flex-wrap items-center gap-3">
              <div className="flex items-center gap-2">
                <FileText className="size-4 text-primary" />
                <h2 id="matter-definition-heading" className="font-semibold">Reviewer guidance</h2>
              </div>
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
            <div className="flex flex-wrap items-center gap-2">
              {currentRevision ? (
                <button
                  type="button"
                  className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`Show revision history from current draft r${currentRevision}`}
                  aria-expanded={historyOpen}
                  onClick={() => {
                    setSelectedRevisionNumber(null);
                    setCompareOpen(false);
                    setHistoryOpen(true);
                  }}
                >
                  <Badge variant="outline">{definition.data?.name} · Draft r{currentRevision}</Badge>
                </button>
              ) : <Badge variant="outline">No saved draft</Badge>}
              {definition.data?.published_revision ? (
                <button
                  type="button"
                  className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  aria-label={`Show published revision r${definition.data.published_revision} in revision history`}
                  aria-expanded={historyOpen}
                  onClick={() => {
                    setSelectedRevisionNumber(
                      definition.data?.published_revision === currentRevision
                        ? null
                        : definition.data?.published_revision ?? null,
                    );
                    setCompareOpen(definition.data?.published_revision !== currentRevision);
                    setHistoryOpen(true);
                  }}
                >
                  <Badge variant="active">Published r{definition.data.published_revision}</Badge>
                </button>
              ) : <Badge variant="accent">Not published</Badge>}
              {dirty ? <Badge variant="accent">Unsaved changes</Badge> : null}
            </div>
          </div>

          <div id="matter-definition-editor" className="flex min-h-0 flex-1 flex-col">
            {newerRevisionAvailable ? (
              <div role="alert" className="border-b border-conflict/30 bg-conflict/10 px-5 py-3 text-sm text-conflict">
                A newer revision was created while you were editing. Your unsaved text is preserved; reload the latest draft before saving.
              </div>
            ) : null}

            <div className="flex min-h-0 flex-1 flex-col p-5">
              <div className="mb-3 flex shrink-0 flex-wrap items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2">
                  <button
                    type="button"
                    className="flex items-center gap-1 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                    aria-expanded={historyOpen}
                    aria-controls="matter-definition-revision-history"
                    onClick={() => setHistoryOpen((open) => !open)}
                  >
                    {selectedRevision && compareOpen
                      ? `Revision ${selectedRevision.revision} → current draft`
                      : selectedRevision
                        ? `Revision ${selectedRevision.revision}`
                        : "Current draft"}
                    <ChevronDown className={cn("size-3.5 transition-transform", historyOpen && "rotate-180")} />
                  </button>
                  {!selectedRevision && sourceFilename ? <span className="truncate text-xs text-muted-foreground">Imported from {sourceFilename}</span> : null}
                  {selectedRevision
                    && definition.data?.published_revision !== null
                    && definition.data?.published_revision !== undefined
                    && selectedRevision.revision === definition.data.published_revision
                    ? <Badge variant="active">Published</Badge>
                    : null}
                </div>
                <div className="flex flex-wrap gap-2">
                  {selectedRevision ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      onClick={() => setCompareOpen((open) => !open)}
                    >
                      {compareOpen ? <FileText /> : <GitCompareArrows />}
                      {compareOpen ? "View revision" : "Compare with current"}
                    </Button>
                  ) : null}
                  {newerRevisionAvailable ? (
                    <Button
                      type="button"
                      size="sm"
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
                    size="sm"
                    variant="outline"
                    disabled={Boolean(selectedRevision) || !definition.data || definition.data.status === "ARCHIVED" || definition.data.published_revision === definition.data.current_revision || dirty}
                    onClick={() => setPublishOpen(true)}
                  >
                    <Sparkles />Publish current draft
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    disabled={Boolean(selectedRevision) || !definition.data || definition.data.status === "ARCHIVED" || !dirty || !draft.trim() || newerRevisionAvailable || saveDraft.isPending}
                    onClick={() => saveDraft.mutate()}
                  >
                    {saveDraft.isPending ? <LoaderCircle className="animate-spin" /> : <Save />}
                    Save draft
                  </Button>
                </div>
              </div>
              {historyOpen ? (
                <div id="matter-definition-revision-history" className="mb-3 shrink-0 rounded-lg border bg-muted/25 p-3" aria-label="Revision history">
                  <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.08em] text-muted-foreground">
                    <History className="size-3.5" />Revision history
                  </div>
                  <div className="flex max-h-28 flex-wrap gap-2 overflow-y-auto">
                    {currentRevision ? (
                      <Button
                        type="button"
                        size="sm"
                        variant={selectedRevisionNumber === null ? "default" : "outline"}
                        aria-pressed={selectedRevisionNumber === null}
                        onClick={() => {
                          setSelectedRevisionNumber(null);
                          setCompareOpen(false);
                        }}
                      >
                        Current draft · r{currentRevision}
                      </Button>
                    ) : null}
                    {[...(revisions.data ?? [])]
                      .sort((left, right) => right.revision - left.revision)
                      .filter((revision) => revision.revision !== currentRevision)
                      .map((revision) => (
                        <Button
                          key={revision.id}
                          type="button"
                          size="sm"
                          variant={selectedRevisionNumber === revision.revision ? "default" : "outline"}
                          aria-pressed={selectedRevisionNumber === revision.revision}
                          onClick={() => {
                            setSelectedRevisionNumber(revision.revision);
                            setCompareOpen(true);
                          }}
                        >
                          Revision {revision.revision} · {formatDate(revision.created_at)}
                          {revision.revision === definition.data?.published_revision ? " · published" : ""}
                        </Button>
                      ))}
                  </div>
                </div>
              ) : null}
              {selectedRevision && compareOpen ? (
                <MatterDefinitionDiff
                  previous={selectedRevision}
                  currentContent={draft}
                  currentRevision={currentRevision}
                  currentIsUnsaved={dirty}
                />
              ) : (
                <Textarea
                  aria-label="Matter Definition Markdown"
                  value={displayedContent}
                  readOnly={Boolean(selectedRevision) || !definition.data || definition.data.status === "ARCHIVED"}
                  onChange={(event) => {
                    if (selectedRevision) return;
                    setDraftEdit((current) => ({
                      content: event.target.value,
                      source: current?.source ?? (definition.data ? "USER_EDIT" : "PASTE"),
                      sourceFilename: current?.sourceFilename ?? null,
                      basedOnRevision: current?.basedOnRevision ?? definition.data?.current_revision ?? null,
                    }));
                  }}
                  placeholder="# Review guidance\n\nDescribe the coding fields, definitions, examples, and decision rules."
                  className={cn(
                    "matter-definition-guidance-text min-h-[34rem] flex-1 resize-none overflow-y-auto font-mono text-[13px] leading-6",
                    selectedRevision && "bg-muted/35",
                  )}
                />
              )}
            </div>
          </div>
        </Card>
        </div>
      </section>

      <aside className="matter-definition-tools-frame min-h-[46rem] min-w-0" aria-label="Matter Definition chat and assessment">
        <Card className="matter-definition-tools-card flex min-h-[46rem] min-w-0 flex-col overflow-hidden">
          <div className="flex min-h-12 shrink-0 items-center gap-2 border-b px-2">
            <div className="flex shrink-0 self-stretch items-end" role="tablist" aria-label="Matter Definition tools">
              <button
                id={`${sidePanelId}-agent`}
                type="button"
                role="tab"
                aria-selected={sidePanel === "agent"}
                aria-controls={`${sidePanelId}-panel`}
                onClick={() => setSidePanel("agent")}
                className={sidePanelTabClass(sidePanel === "agent")}
              >
                <MessageSquareText className="size-4" />Chat
              </button>
              <button
                id={`${sidePanelId}-assessment`}
                type="button"
                role="tab"
                aria-selected={sidePanel === "assessment"}
                aria-controls={`${sidePanelId}-panel`}
                onClick={() => setSidePanel("assessment")}
                className={sidePanelTabClass(sidePanel === "assessment")}
              >
                <ClipboardCheck className="size-4" />Assessment
              </button>
            </div>
            {sidePanel === "agent" ? <div ref={setToolHeaderElement} className="ml-auto flex min-w-0 flex-1 items-center justify-end gap-2 py-1.5" aria-label="Chat controls" /> : null}
          </div>
          <div
            id={`${sidePanelId}-panel`}
            role="tabpanel"
            aria-labelledby={`${sidePanelId}-${sidePanel}`}
            className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
          >
            {sidePanel === "agent" ? (
              <AgentWorkspace
                embedded
                toolbarElement={toolHeaderElement}
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
                renaming={renameConversation.isPending}
                submitting={submitTurn.isPending}
                deciding={decideAction.isPending}
                streamState={chatStream.state}
                onSelectConversation={setSelectedConversationId}
                onStartConversation={(agentId, title) => startConversation.mutate({ agentId, title })}
                onRenameConversation={(conversationId, title) => renameConversation.mutate({ conversationId, title })}
                onSubmitTurn={(message) => submitTurn.mutateAsync(message).then(() => undefined)}
                onDecision={(actionId, decision, reason) => decideAction.mutateAsync({ actionId, decision, reason }).then(() => undefined)}
              />
            ) : (
              <div className="min-h-0 flex-1 overflow-hidden">
                <MatterDefinitionAssessmentPanel
                  embedded
                  matterId={matterId}
                  guidanceId={effectiveGuidanceId}
                  guidanceName={definition.data?.name ?? "Review Guidance"}
                  guidanceArchived={definition.data?.status === "ARCHIVED"}
                  revisions={revisions.data ?? []}
                  publishedRevision={definition.data?.published_revision ?? null}
                />
              </div>
            )}
          </div>
        </Card>
      </aside>

      <Dialog open={guidanceDialog !== null} onOpenChange={(open) => { if (!open) setGuidanceDialog(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {guidanceDialog === "create" ? "Create Review Guidance" : guidanceDialog === "clone" ? "Clone Review Guidance" : "Edit Review Guidance"}
            </DialogTitle>
            <DialogDescription>
              {guidanceDialog === "clone"
                ? `Create an independent copy of ${definition.data?.name ?? "this guidance"} revision ${selectedRevisionNumber ?? definition.data?.current_revision ?? ""}. Later edits will not be shared.`
                : guidanceDialog === "edit"
                  ? "Update the profile name and purpose. Its stable key and revision history do not change."
                  : "Create independently versioned guidance for a review stage or purpose."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <label className="text-sm font-medium" htmlFor="guidance-name">Name</label>
              <Input
                id="guidance-name"
                className="mt-1"
                value={guidanceName}
                placeholder="Privilege Review"
                onChange={(event) => {
                  setGuidanceName(event.target.value);
                  if (guidanceDialog !== "edit") setGuidanceKey(suggestedKey(event.target.value));
                }}
              />
            </div>
            <div>
              <label className="text-sm font-medium" htmlFor="guidance-key">Stable key</label>
              <Input
                id="guidance-key"
                className="mt-1 font-mono"
                value={guidanceKey}
                disabled={guidanceDialog === "edit"}
                placeholder="privilege_review"
                onChange={(event) => setGuidanceKey(event.target.value.toLowerCase())}
              />
              <p className="mt-1 text-xs text-muted-foreground">Lowercase letters, numbers, and underscores. The key cannot change after creation.</p>
            </div>
            <div>
              <label className="text-sm font-medium" htmlFor="guidance-description">Purpose</label>
              <Textarea id="guidance-description" className="mt-1 min-h-20" value={guidanceDescription} onChange={(event) => setGuidanceDescription(event.target.value)} />
            </div>
            {guidanceDialog === "create" ? (
              <div>
                <label className="text-sm font-medium" htmlFor="guidance-initial-content">Initial guidance</label>
                {guidanceInitialFilename ? <p className="mt-1 text-xs text-muted-foreground">Imported from {guidanceInitialFilename}</p> : null}
                <Textarea id="guidance-initial-content" className="mt-1 min-h-36 font-mono text-xs" value={guidanceInitialContent} onChange={(event) => setGuidanceInitialContent(event.target.value)} />
              </div>
            ) : null}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setGuidanceDialog(null)}>Cancel</Button>
            <Button
              type="button"
              disabled={
                !guidanceName.trim()
                || (guidanceDialog !== "edit" && (!/^[a-z][a-z0-9_]{0,99}$/.test(guidanceKey.trim()) || (guidanceDialog === "create" && !guidanceInitialContent.trim())))
                || createGuidance.isPending
                || cloneGuidance.isPending
                || updateGuidance.isPending
              }
              onClick={() => {
                if (guidanceDialog === "create") createGuidance.mutate();
                else if (guidanceDialog === "clone") cloneGuidance.mutate();
                else updateGuidance.mutate();
              }}
            >
              {createGuidance.isPending || cloneGuidance.isPending || updateGuidance.isPending ? <LoaderCircle className="animate-spin" /> : guidanceDialog === "clone" ? <Copy /> : guidanceDialog === "edit" ? <Save /> : <Plus />}
              {guidanceDialog === "create" ? "Create guidance" : guidanceDialog === "clone" ? "Create clone" : "Save details"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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

function MatterDefinitionDiff({
  previous,
  currentContent,
  currentRevision,
  currentIsUnsaved,
}: {
  previous: MatterDefinitionRevisionRead;
  currentContent: string;
  currentRevision: number | null;
  currentIsUnsaved: boolean;
}) {
  const currentLabel = currentIsUnsaved
    ? `Unsaved current draft${currentRevision ? ` based on r${currentRevision}` : ""}`
    : `Current draft${currentRevision ? ` r${currentRevision}` : ""}`;
  const identical = previous.content_markdown === currentContent;
  const differences = useMemo(() => {
    const matcher = new DiffMatchPatch();
    const result = matcher.diff_main(previous.content_markdown, currentContent, true);
    matcher.diff_cleanupSemantic(result);
    return result;
  }, [currentContent, previous.content_markdown]);

  return (
    <section
      role="region"
      aria-label={`Revision ${previous.revision} to ${currentLabel} diff`}
      className="flex min-h-[34rem] flex-1 flex-col overflow-hidden rounded-md border bg-background"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b bg-muted/25 px-4 py-2 text-xs">
        <p className="font-semibold">Revision {previous.revision} → {currentLabel}</p>
        <div className="flex items-center gap-3 text-muted-foreground" aria-label="Diff legend">
          <span><span className="font-bold text-destructive">−</span> Removed</span>
          <span><span className="font-bold text-success">+</span> Added</span>
        </div>
      </div>
      {identical ? (
        <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
          No differences between these revisions.
        </div>
      ) : (
        <pre className="flex-1 overflow-auto whitespace-pre-wrap p-4 font-mono text-[12px] leading-5 [overflow-wrap:anywhere]" tabIndex={0}>
          {differences.map(([operation, text], index) => {
            if (operation === DiffMatchPatch.DIFF_DELETE) {
              return <del key={index} data-diff="delete" className="bg-destructive/15 text-destructive decoration-destructive/60">{text}</del>;
            }
            if (operation === DiffMatchPatch.DIFF_INSERT) {
              return <ins key={index} data-diff="insert" className="bg-success/15 text-success decoration-success/60">{text}</ins>;
            }
            return <span key={index}>{text}</span>;
          })}
        </pre>
      )}
    </section>
  );
}

function sidePanelTabClass(active: boolean) {
  return cn(
    "relative flex h-10 items-center gap-2 px-3 text-sm font-semibold text-muted-foreground outline-none transition hover:text-foreground focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
    active && "text-primary after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:bg-accent",
  );
}

function AgentWorkspace({
  embedded,
  toolbarElement,
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
  renaming,
  submitting,
  deciding,
  streamState,
  onSelectConversation,
  onStartConversation,
  onRenameConversation,
  onSubmitTurn,
  onDecision,
}: {
  embedded?: boolean;
  toolbarElement?: HTMLElement | null;
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
  renaming: boolean;
  submitting: boolean;
  deciding: boolean;
  streamState: AgentChatStreamState;
  onSelectConversation: (id: string) => void;
  onStartConversation: (agentId: string, title: string) => void;
  onRenameConversation: (conversationId: string, title: string) => void;
  onSubmitTurn: (message: string) => Promise<void>;
  onDecision: (actionId: string, decision: Decision, reason?: string) => Promise<void>;
}) {
  const [selectedAgentId, setSelectedAgentId] = useState(agents[0]?.id ?? "");
  const [message, setMessage] = useState("");
  const [newConversationOpen, setNewConversationOpen] = useState(false);
  const [newConversationTitle, setNewConversationTitle] = useState("");
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
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

  function openRename() {
    setRenameTitle(selectedConversation?.title ?? "");
    setRenameOpen(true);
  }

  function startNamedConversation() {
    const title = newConversationTitle.trim();
    if (!effectiveAgentId || !title) return;
    onStartConversation(effectiveAgentId, title);
    setNewConversationTitle("");
    setNewConversationOpen(false);
  }

  function renameConversation() {
    const title = renameTitle.trim();
    if (!selectedConversation || !title) return;
    onRenameConversation(selectedConversation.id, title);
    setRenameOpen(false);
  }

  const toolbar = (
    <>
      <span className="hidden text-xs text-muted-foreground sm:inline" title="Chat update connection">
        {streamState === "live" ? "Live" : streamState === "fallback" ? "Polling" : streamState === "offline" ? "Offline" : "Connecting"}
      </span>
      {conversations.length ? (
        <Select value={selectedConversationId} onValueChange={onSelectConversation}>
          <SelectTrigger id="agent-conversation" className="min-w-0 max-w-64 flex-1" aria-label="Chat"><SelectValue placeholder="Select a chat" /></SelectTrigger>
          <SelectContent>
            {conversations.map((conversation) => (
              <SelectItem key={conversation.id} value={conversation.id}>
                {conversation.title ?? formatDate(conversation.created_at)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : agents.length ? (
        <Select value={effectiveAgentId} onValueChange={setSelectedAgentId}>
          <SelectTrigger className="min-w-0 max-w-64 flex-1" aria-label="Chat agent"><SelectValue placeholder="Select a chat agent" /></SelectTrigger>
          <SelectContent>{agents.map((agent) => <SelectItem key={agent.id} value={agent.id}>{agent.name}</SelectItem>)}</SelectContent>
        </Select>
      ) : <p className="min-w-0 flex-1 truncate text-right text-xs text-muted-foreground">No chat agent</p>}
      {selectedConversation ? <Button type="button" size="icon" className="size-8 shrink-0" variant="outline" disabled={renaming} aria-label="Rename chat" onClick={openRename}><PencilLine /></Button> : null}
      {agents.length ? <Button type="button" size="sm" className="shrink-0" variant="outline" disabled={starting} onClick={() => setNewConversationOpen(true)}>New</Button> : null}
    </>
  );

  return (
    <Card className={cn("flex min-h-[46rem] min-w-0 flex-col overflow-hidden", embedded && "h-full min-h-0 rounded-none border-0 shadow-none")} aria-label="Matter Definition chat">
      {toolbarElement ? createPortal(toolbar, toolbarElement) : <div className="flex shrink-0 items-center gap-2 border-b p-2">{toolbar}</div>}

      {!selectedConversation ? (
        <div className="grid flex-1 place-items-center p-4">
          <div className="max-w-sm text-center text-sm text-muted-foreground">
            {agents.length ? "Select New to start a Matter Definition chat." : "No active, published chat agent is available for this matter."}
          </div>
        </div>
      ) : loadingConversation ? <TableLoading /> : conversationError ? <QueryError message={conversationError} /> : (
        <>
          <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto p-5" aria-live="polite">
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
          <div className="border-t bg-muted/25 p-2">
            {pendingActions.length ? <p className="mb-2 px-1 text-xs font-medium text-warning">Review the pending change before sending another message.</p> : null}
            <div className="flex items-end gap-2 rounded-xl border bg-background p-1 focus-within:ring-2 focus-within:ring-ring">
              <Textarea
                aria-label="Message the Matter Definition chat"
                value={message}
                disabled={activeRun || submitting || pendingActions.length > 0}
                onChange={(event) => setMessage(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
                placeholder="Compare coding fields, improve guidance, or ask the chat to review the current Matter Definition…"
                className="min-h-[4.5rem] flex-1 resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
              />
              <Button type="button" size="sm" className="mb-1 mr-1 shrink-0" disabled={!message.trim() || activeRun || submitting || pendingActions.length > 0} onClick={() => void sendMessage()}>
                {submitting ? <LoaderCircle className="animate-spin" /> : <Send />}
                Send
              </Button>
            </div>
          </div>
        </>
      )}
      <Dialog open={newConversationOpen} onOpenChange={setNewConversationOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Start a new chat</DialogTitle>
            <DialogDescription>Give this Matter Definition chat a name so it is easy to find later.</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="new-chat-title">Chat name</label>
            <Input id="new-chat-title" autoFocus maxLength={200} value={newConversationTitle} onChange={(event) => setNewConversationTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); startNamedConversation(); } }} placeholder="Responsiveness questions" />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setNewConversationOpen(false)}>Cancel</Button>
            <Button type="button" disabled={!newConversationTitle.trim() || !effectiveAgentId || starting} onClick={startNamedConversation}>{starting ? <LoaderCircle className="animate-spin" /> : <Sparkles />}Start chat</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename chat</DialogTitle>
            <DialogDescription>Change how this chat appears in the Matter Definition conversation list.</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="rename-chat-title">Chat name</label>
            <Input id="rename-chat-title" autoFocus maxLength={200} value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); renameConversation(); } }} />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setRenameOpen(false)}>Cancel</Button>
            <Button type="button" disabled={!renameTitle.trim() || renaming} onClick={renameConversation}>{renaming ? <LoaderCircle className="animate-spin" /> : <PencilLine />}Rename</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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
