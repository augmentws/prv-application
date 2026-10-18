import { describe, expect, it } from "vitest";

import { entityFactsHref, factFilters, factsFilterParams, formatFactDate, groupTimeline, hasFactFilters, readFactListState, timelineFilters, writeFactListState } from "@/lib/facts";

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
      includeWorking: false,
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
    const entityId = "0b8c3c1e-6d3f-4a8e-9d55-2f1b7a6c9e10";
    const entityState = readFactListState(new URLSearchParams(`tab=facts&fentity=${entityId}`));
    expect(factFilters(entityState)).toEqual({ entity_id: entityId });
    expect(writeFactListState(new URLSearchParams("tab=facts"), entityState).get("fentity")).toBe(entityId);
    // The entity id is used in API paths, so anything but a UUID is dropped.
    expect(readFactListState(new URLSearchParams("tab=facts&fentity=../../clients")).entityId).toBe("");
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

  it("keeps the calendar day in a time zone behind UTC", () => {
    const original = process.env.TZ;
    process.env.TZ = "America/Los_Angeles";
    try {
      const utc = (options: Intl.DateTimeFormatOptions, value: number) =>
        new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" }).format(value);
      // Local midnight on Jan 1 in Los Angeles is still Dec 31 in local time if the UTC handling is lost.
      expect(formatFactDate({ date_precision: "DAY", event_date_start: "2021-01-01", event_date_end: null })).toBe(
        utc({ month: "short", day: "numeric", year: "numeric" }, Date.UTC(2021, 0, 1)),
      );
      expect(formatFactDate({ date_precision: "MONTH", event_date_start: "2021-03-01", event_date_end: null })).toBe(
        utc({ month: "short", year: "numeric" }, Date.UTC(2021, 2, 1)),
      );
      // The pin is in effect: UTC midnight on Jan 1 is Dec 31 in local time.
      expect(new Date(Date.UTC(2021, 0, 1)).getDate()).toBe(31);
    } finally {
      process.env.TZ = original;
    }
  });
});

describe("timeline", () => {
  it("shows confirmed facts unless working facts are included, ignoring the list's status facet", () => {
    const state = readFactListState(new URLSearchParams("tab=facts&facts=timeline&ff_status=REJECTED&ff_issue_key=pricing"));
    expect(timelineFilters(state)).toEqual({ issue_keys: ["pricing"], statuses: ["CONFIRMED"] });
    const withWorking = readFactListState(new URLSearchParams("fworking=1"));
    expect(withWorking.includeWorking).toBe(true);
    expect(timelineFilters(withWorking).statuses).toEqual(["CONFIRMED", "WORKING"]);
    expect(writeFactListState(new URLSearchParams(), withWorking).get("fworking")).toBe("1");
  });

  it("switches sub-tabs keeping filters but not the page", () => {
    const params = new URLSearchParams("ff_issue_key=pricing&fpage=3");
    expect(factsFilterParams(params).toString()).toBe("ff_issue_key=pricing");
  });

  it("links an entity to the Facts list filtered to that entity", () => {
    expect(entityFactsHref("client-1", "matter-1", "entity-1")).toBe("/app/clients/client-1/matters/matter-1/facts?fentity=entity-1");
  });

  it("groups date-ordered facts by year and by each fact's precision, with undated facts apart", () => {
    const fact = (id: string, date_precision: string, event_date_start: string | null) => ({ id, date_precision, event_date_start, event_date_end: null });
    const { years, undated } = groupTimeline([
      fact("a", "YEAR", "2020-01-01"),
      fact("b", "DAY", "2020-03-04"),
      fact("c", "MONTH", "2020-03-01"),
      fact("d", "QUARTER", "2021-04-01"),
      fact("e", "DAY", "2021-05-02"),
      fact("f", "UNKNOWN", null),
    ] as never[]);
    expect(years.map((year) => [year.year, year.sections.map((section) => [section.label, section.facts.map((item: { id: string }) => item.id)])])).toEqual([
      ["2020", [["2020, month unknown", ["a"]], [expect.stringContaining("2020"), ["b", "c"]]]],
      ["2021", [["Q2 2021", ["d"]], [expect.stringContaining("2021"), ["e"]]]],
    ]);
    expect(undated.map((item: { id: string }) => item.id)).toEqual(["f"]);
  });
});
