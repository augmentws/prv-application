import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DocumentChatMessageContent } from "@/components/batch-chat-panel";

describe("DocumentChatMessageContent", () => {
  it("links every document in a grouped citation", async () => {
    const user = userEvent.setup();
    const onOpenDocument = vi.fn();
    const firstId = "af8f5de2-6b75-4f8c-bfc6-9d8bb5c6e709";
    const secondId = "80f02055-16c4-4cfd-b9b8-790e5535bdb6";

    render(<DocumentChatMessageContent
      content={`Evidence&#x20;[Document ${firstId}, Document ${secondId}].`}
      linkDocuments
      onOpenDocument={onOpenDocument}
    />);

    const firstLink = screen.getByRole("button", { name: `Document ${firstId}` });
    const secondLink = screen.getByRole("button", { name: `Document ${secondId}` });
    await user.click(firstLink);
    await user.click(secondLink);

    expect(onOpenDocument).toHaveBeenNthCalledWith(1, { documentId: firstId, paragraphReference: undefined });
    expect(onOpenDocument).toHaveBeenNthCalledWith(2, { documentId: secondId, paragraphReference: undefined });
  });

  it("keeps a paragraph reference attached to its document", async () => {
    const user = userEvent.setup();
    const onOpenDocument = vi.fn();
    const documentId = "af8f5de2-6b75-4f8c-bfc6-9d8bb5c6e709";

    render(<DocumentChatMessageContent
      content={`See [Document ${documentId} ¶12-14].`}
      linkDocuments
      onOpenDocument={onOpenDocument}
    />);

    await user.click(screen.getByRole("button", { name: `Document ${documentId} ¶12-14` }));
    expect(onOpenDocument).toHaveBeenCalledWith({ documentId, paragraphReference: "12-14" });
  });
});
