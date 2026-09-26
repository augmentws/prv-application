import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CreateTopicJobDialog } from "@/components/forms/create-topic-job-dialog";

afterEach(cleanup);

describe("CreateTopicJobDialog", () => {
  it("selects a new destination field before discovery starts", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();

    render(<CreateTopicJobDialog savedSearches={[]} definitions={[]} onCreate={onCreate} />);

    await user.click(screen.getByRole("button", { name: "Cluster topics" }));
    await user.click(screen.getByRole("combobox", { name: "Destination field option" }));
    await user.click(screen.getByRole("option", { name: "Create a new field" }));
    await user.type(screen.getByLabelText("Field name"), "Communication topics");
    expect(screen.getByLabelText("Field key")).toHaveValue("communication_topics");
    await user.click(screen.getByRole("button", { name: "Start discovery" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      destination_mode: "NEW_FIELD",
      new_field_name: "Communication topics",
      new_field_key: "communication_topics",
    })));
  });
});
