import { describe, expect, it } from "vitest";

import { formatDateOnly, parseDateOnly, utcDayEnd, utcDayStart } from "@/lib/date-only";

describe("date-only helpers", () => {
  it("does not format or submit an incomplete date", () => {
    expect(parseDateOnly("2026-0")).toBeNull();
    expect(formatDateOnly("2026-0")).toBe("2026-0");
    expect(utcDayStart("2026-0")).toBeNull();
    expect(utcDayEnd("2026-0")).toBeNull();
  });

  it("rejects impossible dates and converts valid dates to UTC boundaries", () => {
    expect(parseDateOnly("2026-02-30")).toBeNull();
    expect(utcDayStart("2026-09-18")).toBe("2026-09-18T00:00:00.000Z");
    expect(utcDayEnd("2026-09-18")).toBe("2026-09-18T23:59:59.999Z");
    expect(formatDateOnly("2026-09-18")).not.toBe("2026-09-18");
  });
});
