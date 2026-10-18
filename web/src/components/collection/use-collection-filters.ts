"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";

import {
  type CollectionFacetKey,
  type CollectionFilters,
  collectionFilterSearch,
  emptyCollectionFilters,
  type FileDateRange,
  parseCollectionFilters,
} from "@/lib/collection-filters";

/**
 * Documents filters, read from and written to the page address. Each filter change is one Back step;
 * paging, the open document and typing in the date fields replace the current entry so they don't
 * fill the history.
 * Any filter change returns to the first page and closes the viewer.
 */
export function useCollectionFilters() {
  const searchParams = useSearchParams();
  const pathname = usePathname();
  const router = useRouter();
  const filters = useMemo(() => parseCollectionFilters(searchParams), [searchParams]);

  // Stable between address changes, so tables and memoized columns that call these don't rebuild.
  return useMemo(() => {
    function navigate(next: CollectionFilters, mode: "push" | "replace") {
      const query = collectionFilterSearch(next).toString();
      const href = query ? `${pathname}?${query}` : pathname;
      if (mode === "push") router.push(href, { scroll: false });
      else router.replace(href, { scroll: false });
    }

    function filter(change: Partial<Pick<CollectionFilters, "search" | "facets" | "fileDateRange">>, mode: "push" | "replace" = "push") {
      navigate({ ...filters, ...change, offset: 0, itemId: null }, mode);
    }

    return {
      filters,
      setSearch: (search: string) => filter({ search: search.trim() }),
      /** Pass "replace" while the user is typing a date, so each keystroke isn't its own Back step. */
      setFileDateRange: (fileDateRange: FileDateRange, mode: "push" | "replace" = "push") => filter({ fileDateRange }, mode),
      toggleFacet: (key: CollectionFacetKey, value: string) => {
        const current = filters.facets[key];
        filter({ facets: { ...filters.facets, [key]: current.includes(value) ? current.filter((selected) => selected !== value) : [...current, value] } });
      },
      clearFilters: () => navigate(emptyCollectionFilters(), "push"),
      setOffset: (offset: number) => navigate({ ...filters, offset: Math.max(0, offset), itemId: null }, "replace"),
      setItemId: (itemId: string | null) => navigate({ ...filters, itemId }, "replace"),
    };
  }, [filters, pathname, router]);
}
