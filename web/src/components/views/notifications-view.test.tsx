import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NotificationsView } from "@/components/views/notifications-view";

vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/components/workspace-context", () => ({
  useWorkspace: () => ({ tenant: { id: "tenant-1", name: "Tenant One" } }),
}));

const api = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("@/lib/api-client", () => ({ coreApi: api.request }));

const notification = {
  id: "notification-1",
  tenant_id: "tenant-1",
  scope_type: "MATTER",
  client_id: "client-1",
  client_name: "Acme",
  matter_id: "matter-1",
  matter_name: "Investigation",
  type_key: "agent_action.requested",
  category: "AGENT",
  severity: "WARNING",
  kind: "ACTION_REQUIRED",
  title: "Agent approval required",
  summary: "An agent is waiting for approval before it can continue.",
  source_type: "agent_action_request",
  source_id: "action-1",
  primary_route_name: "AGENT_CONVERSATION",
  primary_route_params: {
    client_id: "client-1",
    matter_id: "matter-1",
    conversation_id: "conversation-1",
    workflow_type: "MATTER_DEFINITION_SETUP",
    review_batch_id: null,
  },
  occurred_at: "2026-10-21T12:00:00Z",
  created_at: "2026-10-21T12:00:00Z",
  resolved_at: null,
  resolution_code: null,
  seen_at: null,
  read_at: null,
  dismissed_at: null,
};

afterEach(() => {
  cleanup();
  api.request.mockReset();
});

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><NotificationsView /></QueryClientProvider>);
}

describe("NotificationsView", () => {
  it("shows actionable notifications and marks them read when opened", async () => {
    api.request.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path.startsWith("/v1/notifications?")) return { items: [notification], next_cursor: null };
      if (path === "/v1/notifications/notification-1/read" && init?.method === "POST") return { updated: 1 };
      return { items: [], next_cursor: null };
    });
    const user = userEvent.setup();
    renderView();

    expect(await screen.findByRole("heading", { name: "Agent approval required" })).toBeInTheDocument();
    expect(screen.getAllByText("Needs action")).toHaveLength(2);
    const link = screen.getByRole("link", { name: "Review request" });
    expect(link).toHaveAttribute("href", "/app/clients/client-1/matters/matter-1/definition");
    await user.click(link);
    await waitFor(() => expect(api.request).toHaveBeenCalledWith(
      "/v1/notifications/notification-1/read",
      { method: "POST" },
    ));
  });

  it("switches to the unread view", async () => {
    api.request.mockResolvedValue({ items: [], next_cursor: null });
    const user = userEvent.setup();
    renderView();
    await user.click(screen.getByRole("tab", { name: "Unread" }));
    await waitFor(() => expect(api.request).toHaveBeenCalledWith(expect.stringContaining("state=UNREAD")));
  });
});
