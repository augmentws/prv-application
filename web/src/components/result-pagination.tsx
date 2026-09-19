"use client";

import { ChevronLeft, ChevronRight, ChevronsRight } from "lucide-react";
import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function ResultPagination({
  total,
  offset,
  pageSize,
  disabled = false,
  onPageChange,
}: {
  total: number;
  offset: number;
  pageSize: number;
  disabled?: boolean;
  onPageChange: (offset: number) => void;
}) {
  const inputId = useId();
  const totalPages = Math.ceil(total / pageSize);
  const currentPage = totalPages ? Math.min(totalPages, Math.floor(offset / pageSize) + 1) : 0;
  const [draftPage, setDraftPage] = useState<string | null>(null);
  const displayedPage = draftPage ?? (currentPage ? String(currentPage) : "");
  const controlsDisabled = disabled || totalPages === 0;

  const goToPage = (page: number) => {
    if (!totalPages || !Number.isFinite(page)) return;
    const nextPage = Math.min(totalPages, Math.max(1, Math.trunc(page)));
    setDraftPage(null);
    onPageChange((nextPage - 1) * pageSize);
  };

  const submitPage = (event: FormEvent) => {
    event.preventDefault();
    goToPage(Number(displayedPage));
  };

  const pageStart = total ? offset + 1 : 0;
  const pageEnd = Math.min(offset + pageSize, total);

  return (
    <nav aria-label="Search result pages" className="flex w-full flex-wrap items-center justify-center gap-1">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="size-8 p-0"
        aria-label="Previous page"
        title="Previous page"
        disabled={controlsDisabled || currentPage <= 1}
        onClick={() => goToPage(currentPage - 1)}
      >
        <ChevronLeft />
      </Button>
      <form onSubmit={submitPage} className="flex items-center gap-1">
        <label htmlFor={inputId} className="text-xs text-muted-foreground">Page</label>
        <Input
          id={inputId}
          type="number"
          step="1"
          inputMode="numeric"
          value={displayedPage}
          onChange={(event) => setDraftPage(event.target.value)}
          aria-label="Page number"
          aria-valuemin={1}
          aria-valuemax={Math.max(1, totalPages)}
          disabled={controlsDisabled}
          className="h-8 w-14 rounded-md px-1 text-center text-xs tabular-nums"
        />
        <span className="whitespace-nowrap text-xs tabular-nums text-muted-foreground">of {totalPages.toLocaleString()}</span>
        <Button type="submit" variant="outline" size="sm" className="px-2" disabled={controlsDisabled || !displayedPage}>Go</Button>
      </form>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="size-8 p-0"
        aria-label="Next page"
        title="Next page"
        disabled={controlsDisabled || currentPage >= totalPages}
        onClick={() => goToPage(currentPage + 1)}
      >
        <ChevronRight />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="px-2"
        aria-label="Last page"
        disabled={controlsDisabled || currentPage >= totalPages}
        onClick={() => goToPage(totalPages)}
      >
        <ChevronsRight />Last
      </Button>
      <span className="sr-only" aria-live="polite">Showing results {pageStart} through {pageEnd} of {total}</span>
    </nav>
  );
}
