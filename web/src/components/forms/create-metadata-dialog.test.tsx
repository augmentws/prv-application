import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateMetadataDialog } from "@/components/forms/create-metadata-dialog";

afterEach(cleanup);

describe("CreateMetadataDialog", () => {
  it("includes lowercase normalization for text field definitions", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<CreateMetadataDialog onCreate={onCreate} />);

    await user.click(screen.getByRole("button", { name: "Add field" }));
    await user.type(screen.getByLabelText("Display name"), "Sender email");
    await user.type(screen.getByLabelText("Field key"), "sender_email");
    await user.click(screen.getByRole("checkbox", { name: "Normalize values to lowercase" }));
    await user.click(screen.getByRole("button", { name: "Create field" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      key: "sender_email",
      type: "TEXT",
      normalize_to_lowercase: true,
    })));
  });

  it("shows and configures hierarchy paths for text metadata fields", async () => {
    const user = userEvent.setup();
    render(<CreateMetadataDialog onCreate={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Add field" }));

    const hierarchy = screen.getByRole("checkbox", { name: /Interpret delimited values as a hierarchy/ });
    expect(hierarchy).not.toBeChecked();
    expect(screen.queryByRole("textbox", { name: "Hierarchy delimiter" })).not.toBeInTheDocument();

    await user.click(hierarchy);

    expect(screen.getByRole("textbox", { name: "Hierarchy delimiter" })).toHaveValue("/");
    expect(screen.getByRole("checkbox", { name: "Searchable" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Available as a filter" })).toBeChecked();
  });
});
