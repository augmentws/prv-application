import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAgentChatStream } from "@/hooks/use-agent-chat-stream";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("useAgentChatStream", () => {
  it("hydrates on ready and applies lifecycle invalidations before advancing the checkpoint", async () => {
    vi.stubEnv("NEXT_PUBLIC_AGENT_STREAMING_ENABLED", "true");
    const encoder = new TextEncoder();
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(stream, {
      headers: { "content-type": "text/event-stream" },
    })));
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const { result, unmount } = renderHook(
      () => useAgentChatStream({ matterId: "matter-1", workflowType: "MATTER_DEFINITION_SETUP" }),
      { wrapper },
    );

    act(() => {
      streamController!.enqueue(encoder.encode([
        "event: stream.ready",
        "data: {\"schema_version\":1,\"matter_sequence\":4}",
        "",
        "id: 5",
        "event: message.created",
        "data: {\"schema_version\":1,\"matter_sequence\":5,\"conversation_id\":\"conversation-1\"}",
        "",
        "event: stream.checkpoint",
        "data: {\"schema_version\":1,\"matter_sequence\":5}",
        "",
        "",
      ].join("\n")));
    });

    await waitFor(() => expect(result.current.state).toBe("live"));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-messages", "conversation-1"] });
    expect(result.current.pollingEnabled).toBe(false);
    unmount();
    streamController?.close();
  });
});
