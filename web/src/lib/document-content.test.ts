import { describe, expect, it } from "vitest";

import { emailBody, textDocument } from "@/lib/document-content";

const bytes = (value: string) => new TextEncoder().encode(value);

describe("document content", () => {
  it("extracts a plain text email body", () => {
    const message = [
      "From: sender@example.com",
      "Content-Type: text/plain; charset=utf-8",
      "Content-Transfer-Encoding: quoted-printable",
      "",
      "Hello=2C reviewer!=0ASecond line.",
    ].join("\r\n");

    expect(emailBody(bytes(message))).toEqual({ text: "Hello, reviewer!\nSecond line.", source: "plain" });
  });

  it("prefers the plain part of a multipart email", () => {
    const message = [
      'Content-Type: multipart/alternative; boundary="part-1"',
      "",
      "--part-1",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Readable body",
      "--part-1",
      "Content-Type: text/html; charset=utf-8",
      "",
      "<p>HTML body</p>",
      "--part-1--",
    ].join("\r\n");

    expect(emailBody(bytes(message))).toEqual({ text: "Readable body", source: "plain" });
  });

  it("decodes plain text documents", () => {
    expect(textDocument(bytes("Evidence text"), "text/plain; charset=utf-8")).toBe("Evidence text");
  });
});
