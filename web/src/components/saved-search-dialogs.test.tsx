import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SavedSearchesDialog, SaveSearchDialog } from "@/components/saved-search-dialogs";
import type { MatterSavedSearchCreate, MatterSavedSearchRead, MatterSearchRequest, UserRead } from "@/generated/models";

const search: MatterSearchRequest = {
  query: "price coordination",
  search_mode: "HYBRID",
  filters: [{ field: "privilege", operator: "IN", values: ["privileged"] }],
  facets: [],
  sort: [{ field: "_score", direction: "DESC" }],
  offset: 50,
  size: 50,
};

const users: UserRead[] = [
  { id: "owner", tenant_id: "tenant", email: "owner@example.com", display_name: "Owner", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" },
  { id: "reviewer", tenant_id: "tenant", email: "reviewer@example.com", display_name: "Reviewer", status: "ACTIVE", tenant_role: "ADMIN", is_superuser: false, created_at: "2026-09-14T12:00:00Z" },
];

afterEach(cleanup);

describe("saved search dialogs", () => {
  it("creates a user-shared search and resets its page", async () => {
    const onCreate = vi.fn(async (payload: MatterSavedSearchCreate) => {
      void payload;
    });
    const user = userEvent.setup();
    render(<SaveSearchDialog search={search} users={users} currentUserId="owner" onCreate={onCreate} />);

    await user.click(screen.getByRole("button", { name: "Save search" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "Privileged pricing");
    await user.click(within(dialog).getByRole("combobox", { name: "Access" }));
    await user.click(screen.getByRole("option", { name: "Shared — selected users" }));
    await user.click(within(dialog).getByRole("checkbox", { name: /Reviewer/ }));
    await user.click(within(dialog).getByRole("button", { name: "Save search" }));

    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      name: "Privileged pricing",
      visibility: "SHARED",
      shared_user_ids: ["reviewer"],
      search: expect.objectContaining({ query: "price coordination", offset: 0 }),
    }));
  });

  it("runs an accessible saved search", async () => {
    const saved: MatterSavedSearchRead = {
      id: "saved-1",
      matter_id: "matter-1",
      name: "Privileged pricing",
      description: null,
      visibility: "PUBLIC",
      search,
      owner: { id: "owner", display_name: "Owner", email: "owner@example.com" },
      shared_users: [],
      is_owner: false,
      created_at: "2026-09-14T12:00:00Z",
      updated_at: "2026-09-14T12:00:00Z",
    };
    const onRun = vi.fn();
    const user = userEvent.setup();
    render(<SavedSearchesDialog searches={[saved]} loading={false} onRun={onRun} onDelete={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Saved searches" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Run" }));

    expect(onRun).toHaveBeenCalledWith(saved);
  });
});
