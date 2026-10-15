import { describe, expect, it } from "vitest";

import { HAS_VALUE_FILTER_TOKEN, NO_VALUE_FILTER_TOKEN, toggleFacetFilterToken } from "@/lib/search-filters";

describe("toggleFacetFilterToken", () => {
  it("keeps Has value mutually exclusive with concrete and missing values", () => {
    expect(toggleFacetFilterToken(["responsive", NO_VALUE_FILTER_TOKEN], HAS_VALUE_FILTER_TOKEN)).toEqual([
      HAS_VALUE_FILTER_TOKEN,
    ]);
    expect(toggleFacetFilterToken([HAS_VALUE_FILTER_TOKEN], NO_VALUE_FILTER_TOKEN)).toEqual([
      NO_VALUE_FILTER_TOKEN,
    ]);
    expect(toggleFacetFilterToken([HAS_VALUE_FILTER_TOKEN], "responsive")).toEqual(["responsive"]);
    expect(toggleFacetFilterToken([NO_VALUE_FILTER_TOKEN], "responsive")).toEqual(["responsive"]);
    expect(toggleFacetFilterToken(["responsive", "privileged"], NO_VALUE_FILTER_TOKEN)).toEqual([
      NO_VALUE_FILTER_TOKEN,
    ]);
    expect(toggleFacetFilterToken([HAS_VALUE_FILTER_TOKEN], HAS_VALUE_FILTER_TOKEN)).toEqual([]);
  });
});
