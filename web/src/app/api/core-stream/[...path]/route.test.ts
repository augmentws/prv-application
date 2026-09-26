import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET } from "@/app/api/core-stream/[...path]/route";

vi.mock("server-only", () => ({}));

const context = {
  params: Promise.resolve({ path: ["v1", "matters", "matter-1", "agent-events"] }),
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("agent event streaming proxy", () => {
  it("passes through the upstream stream without buffering it", async () => {
    const encoder = new TextEncoder();
    let upstreamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        upstreamController = controller;
        controller.enqueue(encoder.encode("event: stream.ready\ndata: {\"matter_sequence\":0}\n\n"));
      },
    });
    const upstream = new Response(body, {
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache, no-store, no-transform",
        "x-accel-buffering": "no",
        "content-length": "999",
      },
    });
    const arrayBufferSpy = vi.spyOn(upstream, "arrayBuffer");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstream));
    const request = new NextRequest(
      "http://localhost:3000/api/core-stream/v1/matters/matter-1/agent-events?workflow_type=MATTER_DEFINITION_SETUP",
      { headers: { cookie: "pv_access=access-token", "last-event-id": "12" } },
    );

    const response = await GET(request, context);
    const reader = response.body!.getReader();
    const first = await reader.read();

    expect(new TextDecoder().decode(first.value)).toContain("stream.ready");
    expect(arrayBufferSpy).not.toHaveBeenCalled();
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    expect(response.headers.get("content-length")).toBeNull();
    const forwarded = vi.mocked(fetch).mock.calls[0][1];
    expect(new Headers(forwarded?.headers).get("last-event-id")).toBe("12");
    upstreamController?.close();
    await reader.cancel();
  });
});
