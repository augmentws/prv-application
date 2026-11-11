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

const matters = [
  { id: "matter-1", client_id: "client-1", name: "Regulatory review", status: "ACTIVE", created_at: "2026-10-03T00:00:00Z" },
  { id: "matter-2", client_id: "client-1", name: "Legacy review", status: "DELETING", created_at: "2026-10-04T00:00:00Z" },
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function mockApi(clientPermissions: string[]) {
  vi.mocked(coreApi).mockImplementation(async (path, init) => {
    if (path === "/v1/clients/client-1") return { id: "client-1", tenant_id: "tenant-1", name: "TREC" } as never;
    if (path === "/v1/authorization/effective?resource_type=CLIENT&resource_id=client-1") return { permission_keys: clientPermissions } as never;
    if (path === "/v1/clients/client-1/matters") return matters as never;
    if (path.startsWith("/v1/authorization/effective?resource_type=MATTER&resource_id=")) return { permission_keys: ["matter.configure"] } as never;
    if (path === "/v1/clients/client-1/matter-templates") return [] as never;
    if (path === "/v1/tenants/tenant-1/clients/client-1/collections") return collections as never;
    if (path === "/v1/collections/collection-2/deletion") return {
      id: "collection-deletion-1",
      collection_id: "collection-2",
      tenant_id: "tenant-1",
      client_id: "client-1",
      collection_name: "Shared drives",
      status: "DELETING_DATABASE_ROWS",
      workflow_id: "collection-delete:collection-deletion-1:attempt:1",
      attempt_count: 1,
      item_count: 4_000,
      artifact_count: 8_000,
      blob_count: 0,
      deleted_item_count: 1_000,
      deleted_artifact_count: 2_000,
      deleted_blob_count: 0,
      error_message: null,
      requested_by_user_id: "user-1",
      started_at: "2026-10-02T00:01:00Z",
      completed_at: null,
      created_at: "2026-10-02T00:00:00Z",
      updated_at: "2026-10-02T00:02:00Z",
    } as never;
    if (path === "/v1/matters/matter-2/deletion") return {
      id: "matter-deletion-1",
      matter_id: "matter-2",
      tenant_id: "tenant-1",
      client_id: "client-1",
      matter_name: "Legacy review",
      status: "DELETING_ARTIFACTS",
      workflow_id: "matter-delete:matter-deletion-1:attempt:1",
      attempt_count: 1,
      document_count: 55_366,
      artifact_count: 5,
      deleted_index_count: 1,
      deleted_artifact_count: 2,
      deleted_blob_count: 2,
      error_message: null,
      requested_by_user_id: "user-1",
      started_at: "2026-10-04T00:01:00Z",
      completed_at: null,
      created_at: "2026-10-04T00:00:00Z",
      updated_at: "2026-10-04T00:02:00Z",
    } as never;
    if (path === "/v1/collections/collection-1" && init?.method === "DELETE") return { collection_name: "Enron mailboxes" } as never;
    if (path === "/v1/matters/matter-1" && init?.method === "DELETE") return { matter_name: "Regulatory review" } as never;
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
    expect(await within(row).findByText("deleting database rows")).toBeInTheDocument();
    expect(within(row).getByText("1,000 of 4,000 items removed")).toBeInTheDocument();
  });

  it("hides deletion from users who cannot administer the client", async () => {
    mockApi(["collection.view"]);
    renderView();

    await screen.findByRole("link", { name: "Enron mailboxes" });
    expect(screen.queryByRole("button", { name: /Delete Collection/ })).not.toBeInTheDocument();
  });
});

describe("ClientView matters", () => {
  it("requires the exact matter name before queuing permanent deletion", async () => {
    mockApi(["client.configure"]);
    const user = userEvent.setup();
    renderView();

    const row = (await screen.findByRole("link", { name: "Regulatory review" })).closest("tr")!;
    const trash = within(row).getByRole("button", { name: "Delete Matter Regulatory review" });
    expect(trash).toHaveAttribute("title", "Delete Matter");
    await user.click(trash);

    const dialog = await screen.findByRole("dialog", { name: "Delete Regulatory review?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete permanently" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/to confirm/), "Regulatory review");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(coreApi).toHaveBeenCalledWith("/v1/matters/matter-1", { method: "DELETE" }));
  });

  it("hides matter deletion without client administration and while deletion is running", async () => {
    mockApi([]);
    renderView();
    await screen.findByRole("link", { name: "Regulatory review" });
    expect(screen.queryByRole("button", { name: /Delete Matter/ })).not.toBeInTheDocument();

    cleanup();
    mockApi(["client.configure"]);
    renderView();
    const deletingRow = (await screen.findByRole("link", { name: "Legacy review" })).closest("tr")!;
    expect(within(deletingRow).queryByRole("button", { name: /Delete Matter/ })).not.toBeInTheDocument();
    expect(await within(deletingRow).findByText("deleting artifacts")).toBeInTheDocument();
    expect(within(deletingRow).getByText("2 of 5 artifacts removed")).toBeInTheDocument();
  });
});

describe("ClientView table columns", () => {
  function columnWidths(table: HTMLElement) {
    return within(table).getAllByRole("columnheader").map((header) => header.style.width);
  }

  it.each([
    ["with the collection trash can", ["collection.view", "client.configure"]],
    ["without it", ["collection.view"]],
  ])("lines up the Collections and Matters columns %s", async (_, permissions) => {
    mockApi(permissions);
    renderView();

    await screen.findByRole("link", { name: "Enron mailboxes" });
    const [collectionTable, matterTable] = screen.getAllByRole("table");
    expect(columnWidths(collectionTable)).toEqual(columnWidths(matterTable));
    // Widths are shares of the table, so both tables fill the same width.
    expect(columnWidths(collectionTable).every((width) => width.endsWith("%"))).toBe(true);
  });
});
