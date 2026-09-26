"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

type WorkflowType = "MATTER_DEFINITION_SETUP" | "BATCH_CHAT";
export type AgentChatStreamState = "connecting" | "live" | "reconnecting" | "fallback" | "offline";

interface AgentLifecycleEvent {
  schema_version: number;
  matter_sequence: number;
  conversation_id?: string;
}

interface ParsedEvent {
  event: string;
  id?: string;
  data: string;
}

class CleanReconnect extends Error {}

function streamingConfigured() {
  const configured = process.env.NEXT_PUBLIC_AGENT_STREAMING_ENABLED;
  if (configured !== undefined) return configured === "true";
  return process.env.NODE_ENV !== "test";
}

function parseEvent(block: string): ParsedEvent | null {
  if (!block || block.startsWith(":")) return null;
  let event = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  return { event, id, data: data.join("\n") };
}

function delay(milliseconds: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

export function useAgentChatStream({ matterId, workflowType, reviewBatchId }: {
  matterId: string;
  workflowType: WorkflowType;
  reviewBatchId?: string;
}) {
  const queryClient = useQueryClient();
  const enabled = useMemo(() => streamingConfigured(), []);
  const [state, setState] = useState<AgentChatStreamState>(enabled ? "connecting" : "fallback");
  const cursorRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled || !matterId) {
      return;
    }
    const controller = new AbortController();
    cursorRef.current = null;
    let failures = 0;
    let pendingConversationList = false;
    const pendingMessages = new Set<string>();
    const pendingRuns = new Set<string>();
    const pendingActions = new Set<string>();

    const conversationKey = workflowType === "BATCH_CHAT"
      ? ["batch-chat-conversations", matterId, reviewBatchId]
      : ["agent-conversations", matterId];

    async function hydrate() {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: conversationKey }),
        queryClient.invalidateQueries({ queryKey: ["agent-messages"] }),
        queryClient.invalidateQueries({ queryKey: ["agent-runs"] }),
        queryClient.invalidateQueries({ queryKey: ["agent-actions"] }),
      ]);
    }

    function queueLifecycle(eventName: string, event: AgentLifecycleEvent) {
      const conversationId = event.conversation_id;
      if (eventName.startsWith("conversation.")) pendingConversationList = true;
      if (!conversationId) return;
      if (eventName === "message.created") pendingMessages.add(conversationId);
      if (eventName.startsWith("run.")) pendingRuns.add(conversationId);
      if (eventName.startsWith("action_request.")) pendingActions.add(conversationId);
    }

    async function flushLifecycle() {
      const work: Promise<unknown>[] = [];
      if (pendingConversationList) work.push(queryClient.invalidateQueries({ queryKey: conversationKey }));
      for (const id of pendingMessages) work.push(queryClient.invalidateQueries({ queryKey: ["agent-messages", id] }));
      for (const id of pendingRuns) work.push(queryClient.invalidateQueries({ queryKey: ["agent-runs", id] }));
      for (const id of pendingActions) work.push(queryClient.invalidateQueries({ queryKey: ["agent-actions", id] }));
      pendingConversationList = false;
      pendingMessages.clear();
      pendingRuns.clear();
      pendingActions.clear();
      await Promise.all(work);
    }

    async function handleFrame(frame: ParsedEvent) {
      const data = frame.data ? JSON.parse(frame.data) as AgentLifecycleEvent & { reason?: string } : null;
      if (!data || data.schema_version !== 1) {
        await hydrate();
        cursorRef.current = null;
        throw new Error("Unsupported agent stream schema");
      }
      if (frame.event === "stream.ready") {
        await hydrate();
        cursorRef.current = data.matter_sequence;
        failures = 0;
        setState("live");
        return;
      }
      if (frame.event === "stream.checkpoint") {
        await flushLifecycle();
        cursorRef.current = data.matter_sequence;
        failures = 0;
        setState("live");
        return;
      }
      if (frame.event === "snapshot.required") {
        await hydrate();
        cursorRef.current = data.matter_sequence;
        throw new CleanReconnect(`Agent stream snapshot required: ${data.reason ?? "unknown"}`);
      }
      if (frame.event === "stream.rotate") {
        await flushLifecycle();
        cursorRef.current = data.matter_sequence;
        throw new CleanReconnect("Agent stream rotation requested");
      }
      const sequence = Number(frame.id ?? data.matter_sequence);
      if (!Number.isSafeInteger(sequence) || sequence <= (cursorRef.current ?? -1)) return;
      queueLifecycle(frame.event, data);
    }

    async function connect() {
      const params = new URLSearchParams({ workflow_type: workflowType });
      if (reviewBatchId) params.set("review_batch_id", reviewBatchId);
      if (cursorRef.current !== null) params.set("after", String(cursorRef.current));
      const response = await fetch(`/api/core-stream/v1/matters/${matterId}/agent-events?${params}`, {
        headers: { accept: "text/event-stream" },
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`Agent stream failed with ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (!controller.signal.aborted) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const frame = parseEvent(block);
          if (frame) await handleFrame(frame);
          boundary = buffer.indexOf("\n\n");
        }
        if (done) break;
      }
      await flushLifecycle();
      if (!controller.signal.aborted) throw new Error("Agent stream closed");
    }

    async function run() {
      while (!controller.signal.aborted) {
        if (!navigator.onLine) {
          setState("offline");
          try {
            await delay(1000, controller.signal);
          } catch {
            return;
          }
          continue;
        }
        setState(failures ? (failures >= 3 ? "fallback" : "reconnecting") : "connecting");
        try {
          await connect();
        } catch (error) {
          if (controller.signal.aborted) return;
          if (error instanceof CleanReconnect) {
            failures = 0;
            setState("reconnecting");
            continue;
          }
          failures += 1;
          setState(failures >= 3 ? "fallback" : "reconnecting");
          const backoff = Math.min(30_000, 500 * 2 ** Math.min(failures, 6));
          try {
            await delay(backoff + Math.floor(Math.random() * 250), controller.signal);
          } catch {
            return;
          }
        }
      }
    }

    const online = () => { if (!controller.signal.aborted) setState("reconnecting"); };
    const offline = () => setState("offline");
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    void run();
    return () => {
      controller.abort();
      window.removeEventListener("online", online);
      window.removeEventListener("offline", offline);
    };
  }, [enabled, matterId, queryClient, reviewBatchId, workflowType]);

  return {
    state,
    enabled,
    pollingEnabled: !enabled || state === "fallback" || state === "offline",
  };
}
