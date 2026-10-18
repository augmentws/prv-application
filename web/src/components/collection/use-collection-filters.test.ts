import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useCollectionFilters } from "@/components/collection/use-collection-filters";

const base = "/app/clients/client-1/collections/collection-1";
const navigation = vi.hoisted(() => ({ params: new URLSearchParams(), push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/app/clients/client-1/collections/collection-1",
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
  useSearchParams: () => navigation.params,
}));

afterEach(() => {
  vi.clearAllMocks();
  navigation.params = new URLSearchParams();
});

function setup(query: string) {
  navigation.params = new URLSearchParams(query);
  return renderHook(() => useCollectionFilters()).result.current;
}

describe("useCollectionFilters", () => {
  it("reads the filters from the address", () => {
    const { filters } = setup("q=memo&ext=pdf&offset=50");
    expect(filters.search).toBe("memo");
    expect(filters.facets.file_extensions).toEqual(["pdf"]);
    expect(filters.offset).toBe(50);
  });

  it("pushes a filter change, back to the first page and with the viewer closed", () => {
    setup("q=memo&offset=50&item=item-1").toggleFacet("file_extensions", "pdf");
    expect(navigation.push).toHaveBeenCalledWith(`${base}?q=memo&ext=pdf`, { scroll: false });
  });

  it("removes a facet value that is already selected", () => {
    setup("ext=pdf&ext=docx").toggleFacet("file_extensions", "pdf");
    expect(navigation.push).toHaveBeenCalledWith(`${base}?ext=docx`, { scroll: false });
  });

  it("pushes a new search, trimmed", () => {
    setup("ext=pdf").setSearch("  invoice ");
    expect(navigation.push).toHaveBeenCalledWith(`${base}?q=invoice&ext=pdf`, { scroll: false });
  });

  it("replaces the entry while a date is typed, and pushes otherwise", () => {
    const filters = setup("");
    filters.setFileDateRange({ from: "2001-01-01", to: "" }, "replace");
    expect(navigation.replace).toHaveBeenCalledWith(`${base}?from=2001-01-01`, { scroll: false });
    filters.setFileDateRange({ from: "", to: "" });
    expect(navigation.push).toHaveBeenCalledWith(base, { scroll: false });
  });

  it("pages and opens documents without adding history", () => {
    const filters = setup("q=memo&item=item-1");
    filters.setOffset(100);
    expect(navigation.replace).toHaveBeenCalledWith(`${base}?q=memo&offset=100`, { scroll: false });
    filters.setItemId("item-2");
    expect(navigation.replace).toHaveBeenCalledWith(`${base}?q=memo&item=item-2`, { scroll: false });
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it("clears every filter back to the bare address", () => {
    setup("q=memo&ext=pdf&from=2001-01-01&offset=50").clearFilters();
    expect(navigation.push).toHaveBeenCalledWith(base, { scroll: false });
  });
});
