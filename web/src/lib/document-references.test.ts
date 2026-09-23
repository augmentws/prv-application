import { describe, expect, it } from "vitest";

import { textParagraphRanges } from "@/components/document-viewer-dialog";
import { normalizeParagraphReference, parseParagraphNumbers } from "@/lib/document-references";

describe("document references", () => {
  it("expands paragraph ranges emitted by batch chat", () => {
    expect(parseParagraphNumbers("¶1-5")).toEqual([1, 2, 3, 4, 5]);
    expect(parseParagraphNumbers("¶2–¶4, ¶8")).toEqual([2, 3, 4, 8]);
    expect(normalizeParagraphReference("¶2–¶4, ¶8")).toBe("2-4,8");
  });

  it("uses the same blank-line paragraph boundaries as document analysis", () => {
    const source = "First paragraph.\n\n Second paragraph\ncontinues. \n\n\nThird paragraph.";
    const ranges = textParagraphRanges(source);

    expect(ranges.map((range) => source.slice(range.start, range.end))).toEqual([
      "First paragraph.",
      "Second paragraph\ncontinues.",
      "Third paragraph.",
    ]);
  });
});
