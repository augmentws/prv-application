import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ClientView } from "@/components/views/client-view";
import { coreApi } from "@/lib/api-client";

vi.mock("@/lib/api-client", () => ({ coreApi: vi.fn() }));
vi.mock("sonner", () => ({ toast: { success: vi.fn() } }));
vi.mock("next/link", () => ({
  default: ({ children, ...props }: { children: ReactNode; href: string }) => <a {...props}>{children}</a>,
}));

const collections = [
  { id: "collection-1", tenant_id: "tenant-1", client_id: "client-1", name: "Enron mailboxes", description: null, status: "ACTIVE", created_at: "2026-10-01T00:00:00Z" },
  { id: "collection-2", tenant_id: "tenant-1", client_id: "client-1", name: "Shared drives", description: null, status: "DELETING", created_at: "2026-10-02T00:00:00Z" },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mockApi(clientPermissions: string[]) {
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path === "/v1/authorization/effective?resource_type=CLIENT&resource_id=client-1") return { permission_keys: clientPermissions } as never;
    if (path === "/v1/clients/client-1/matters") return [] as never;
    if (path === "/v1/clients/client-1/matter-templates") return [] as never;
    if (path === "/v1/tenants/tenant-1/clients/client-1/collections") return collections as never;
    if (path === "/v1/collections/collection-1" && init?.method === "DELETE") return { collection_name: "Enron mailboxes" } as never;
    throw new Error(`Unexpected API request: ${path}`);
  });
}

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(<QueryClientProvider client={client}><ClientView clientId="client-1" /></QueryClientProvider>);
}

describe("ClientView collections", () => {
  it("lets a client administrator delete a collection from the trash can at the right of its row", async () => {
    mockApi(["collection.view", "client.configure"]);
    const user = userEvent.setup();
    renderView();

    const row = (await screen.findByRole("link", { name: "Enron mailboxes" })).closest("tr")!;
    const trash = within(row).getByRole("button", { name: "Delete Collection Enron mailboxes" });
    expect(trash).toHaveAttribute("title", "Delete Collection");
    expect(within(row).getAllByRole("cell").at(-1)).toContainElement(trash);

    await user.click(trash);
    const dialog = await screen.findByRole("dialog", { name: "Delete Enron mailboxes?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete permanently" });
    // Nothing can be deleted until the exact collection name is typed.
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm/), "Enron mailbox");
    expect(confirm).toBeDisabled();
    expect(coreApi).not.toHaveBeenCalledWith("/v1/collections/collection-1", { method: "DELETE" });
    await user.type(within(dialog).getByLabelText(/to confirm/), "es");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith("/v1/collections/collection-1", { method: "DELETE" }));
  });

  it("shows no trash can for a collection that is already being deleted", async () => {
    mockApi(["collection.view", "client.configure"]);
    renderView();

    const row = (await screen.findByRole("link", { name: "Shared drives" })).closest("tr")!;
    expect(within(row).queryByRole("button", { name: /Delete Collection/ })).not.toBeInTheDocument();
  });

  it("hides deletion from users who cannot administer the client", async () => {
    mockApi(["collection.view"]);
    renderView();

    await screen.findByRole("link", { name: "Enron mailboxes" });
    expect(screen.queryByRole("button", { name: /Delete Collection/ })).not.toBeInTheDocument();
  });
});
