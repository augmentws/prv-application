import { describe, expect, it } from "vitest";

import {
  collectionFilterSearch,
  collectionSearchApiParams,
  emptyCollectionFilters,
  hasCollectionFilters,
  parseCollectionFilters,
  toImportSelection,
} from "@/lib/collection-filters";

const query = "q=invoice&custodian=c-1&custodian=c-2&ext=pdf&type=EMAIL&status=PROCESSED&from=2001-01-01&to=2001-01-31&offset=50&item=item-1";

describe("parseCollectionFilters", () => {
  it("reads every filter from the page address", () => {
    expect(parseCollectionFilters(new URLSearchParams(query))).toEqual({
      search: "invoice",
      facets: { custodians: ["c-1", "c-2"], file_extensions: ["pdf"], record_types: ["EMAIL"], processing_statuses: ["PROCESSED"] },
      fileDateRange: { from: "2001-01-01", to: "2001-01-31" },
      offset: 50,
      itemId: "item-1",
    });
  });

  it("is empty for a bare address", () => {
    expect(parseCollectionFilters(new URLSearchParams())).toEqual(emptyCollectionFilters());
  });

  it.each([
    ["from=2001-02-31", { from: "", to: "" }],
    ["from=yesterday&to=2001-13-01", { from: "", to: "" }],
    ["to=2001-02-28", { from: "", to: "2001-02-28" }],
  ])("drops invalid dates in %s", (search, fileDateRange) => {
    expect(parseCollectionFilters(new URLSearchParams(search)).fileDateRange).toEqual(fileDateRange);
  });

  it.each(["offset=-50", "offset=abc", "offset=1.5", "offset=0"])("treats %s as the first page", (search) => {
    expect(parseCollectionFilters(new URLSearchParams(search)).offset).toBe(0);
  });

  it("drops blank and repeated facet values and trims the search", () => {
    const filters = parseCollectionFilters(new URLSearchParams("q=%20memo%20&ext=pdf&ext=pdf&ext=&custodian=%20"));
    expect(filters.search).toBe("memo");
    expect(filters.facets.file_extensions).toEqual(["pdf"]);
    expect(filters.facets.custodians).toEqual([]);
  });
});

describe("collectionFilterSearch", () => {
  it("round-trips through the page address", () => {
    const filters = parseCollectionFilters(new URLSearchParams(query));
    expect(parseCollectionFilters(collectionFilterSearch(filters))).toEqual(filters);
  });

  it("leaves defaults out, so an unfiltered view is the bare address", () => {
    expect(collectionFilterSearch(emptyCollectionFilters()).toString()).toBe("");
  });
});

describe("hasCollectionFilters", () => {
  it("ignores paging and the open document", () => {
    expect(hasCollectionFilters({ ...emptyCollectionFilters(), offset: 100, itemId: "item-1" })).toBe(false);
    expect(hasCollectionFilters({ ...emptyCollectionFilters(), fileDateRange: { from: "", to: "2001-01-01" } })).toBe(true);
    expect(hasCollectionFilters(parseCollectionFilters(new URLSearchParams("type=EMAIL")))).toBe(true);
  });
});

describe("collectionSearchApiParams", () => {
  it("uses the Core API's names and whole UTC days, without paging", () => {
    const params = collectionSearchApiParams(parseCollectionFilters(new URLSearchParams(query)));
    expect(params.get("q")).toBe("invoice");
    expect(params.getAll("custodian_id")).toEqual(["c-1", "c-2"]);
    expect(params.getAll("extension")).toEqual(["pdf"]);
    expect(params.getAll("record_type")).toEqual(["EMAIL"]);
    expect(params.getAll("processing_status")).toEqual(["PROCESSED"]);
    expect(params.get("file_date_from")).toBe("2001-01-01T00:00:00.000Z");
    expect(params.get("file_date_to")).toBe("2001-01-31T23:59:59.999Z");
    expect(params.has("offset")).toBe(false);
  });
});

describe("toImportSelection", () => {
  it("freezes exactly the documents the filters show", () => {
    expect(toImportSelection(parseCollectionFilters(new URLSearchParams(query)))).toEqual({
      mode: "QUERY",
      q: "invoice",
      custodian_ids: ["c-1", "c-2"],
      file_extensions: ["pdf"],
      record_types: ["EMAIL"],
      processing_statuses: ["PROCESSED"],
      file_date_from: "2001-01-01T00:00:00.000Z",
      file_date_to: "2001-01-31T23:59:59.999Z",
      item_ids: [],
    });
  });

  it("selects the whole collection without filters", () => {
    expect(toImportSelection(emptyCollectionFilters())).toMatchObject({ q: null, custodian_ids: [], file_date_from: null, file_date_to: null });
  });
});
