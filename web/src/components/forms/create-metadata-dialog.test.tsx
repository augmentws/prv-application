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
});
