import { describe, expect, it } from "vitest";

import { factFilters, formatFactDate, hasFactFilters, readFactListState, writeFactListState } from "@/lib/facts";

describe("fact list URL state", () => {
  it("round-trips search, facets, dates, pending, sort and page while keeping other params", () => {
    const state = readFactListState(
      new URLSearchParams("tab=facts&fq=price&ff_status=WORKING,CONFIRMED&ff_key_fact=true&ffrom=2021-01-01&fto=bad&fpending=1&fsort=UPDATED_DESC&fpage=3"),
    );
    expect(state).toEqual({
      query: "price",
      selected: { status: ["WORKING", "CONFIRMED"], key_fact: ["true"] },
      dateFrom: "2021-01-01",
      dateTo: "",
      pendingOnly: true,
      entityId: "",
      sort: "UPDATED_DESC",
      page: 3,
    });
    const written = writeFactListState(new URLSearchParams("tab=facts&fq=old"), { ...state, page: 1, sort: "DATE_ASC" });
    expect(written.get("tab")).toBe("facts");
    expect(written.get("fq")).toBe("price");
    expect(written.get("ff_status")).toBe("WORKING,CONFIRMED");
    expect(written.has("fsort")).toBe(false);
    expect(written.has("fpage")).toBe(false);
  });

  it("builds API filters with boolean facets and self-excluding facet requests", () => {
    const state = readFactListState(new URLSearchParams("fq=  approval &ff_issue_key=pricing&ff_key_fact=true,false&ff_has_contradiction=false&fpending=1"));
    expect(factFilters(state)).toEqual({ query: "approval", issue_keys: ["pricing"], has_contradiction: false, has_pending_suggestion: true });
    expect(factFilters(state, { without: "issue_key" })).not.toHaveProperty("issue_keys");
    expect(hasFactFilters(readFactListState(new URLSearchParams("tab=facts")))).toBe(false);
    const entityState = readFactListState(new URLSearchParams("tab=facts&fentity=entity-1"));
    expect(factFilters(entityState)).toEqual({ entity_id: "entity-1" });
    expect(writeFactListState(new URLSearchParams("tab=facts"), entityState).get("fentity")).toBe("entity-1");
  });
});

describe("formatFactDate", () => {
  it("shows each precision without shifting date-only values across time zones", () => {
    expect(formatFactDate({ date_precision: "YEAR", event_date_start: "2021-01-01", event_date_end: null })).toBe("2021");
    expect(formatFactDate({ date_precision: "QUARTER", event_date_start: "2021-04-01", event_date_end: null })).toBe("Q2 2021");
    expect(formatFactDate({ date_precision: "MONTH", event_date_start: "2021-03-01", event_date_end: null })).toMatch(/2021/);
    expect(formatFactDate({ date_precision: "DAY", event_date_start: "2021-01-01", event_date_end: null })).toMatch(/1/);
    expect(formatFactDate({ date_precision: "DAY", event_date_start: "2021-01-01", event_date_end: null })).not.toMatch(/2020/);
    expect(formatFactDate({ date_precision: "RANGE", event_date_start: "2021-01-01", event_date_end: "2021-02-01" })).toContain("–");
    expect(formatFactDate({ date_precision: "UNKNOWN", event_date_start: null, event_date_end: null })).toBe("Undated");
  });
});
