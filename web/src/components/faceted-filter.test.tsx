import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ActiveFilterBar, FacetSidebar } from "@/components/faceted-filter";

describe("faceted filters", () => {
  it("shows counts and reports facet and active-filter actions", () => {
    const onToggle = vi.fn();
    const onClear = vi.fn();
    const onRemove = vi.fn();
    render(
      <>
        <FacetSidebar
          groups={[{ key: "extensions", label: "File extension", options: [{ value: "pdf", label: ".pdf", count: 12 }] }]}
          selected={{ extensions: ["pdf"] }}
          hasFilters
          onToggle={onToggle}
          onClear={onClear}
        />
        <ActiveFilterBar filters={[{ key: "extensions:pdf", label: "File extension: .pdf" }]} onRemove={onRemove} />
      </>,
    );

    expect(screen.getByRole("checkbox", { name: /\.pdf/ })).toBeChecked();
    expect(screen.getByText("12")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /\.pdf/ }));
    expect(onToggle).toHaveBeenCalledWith("extensions", "pdf");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(onClear).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Remove File extension: .pdf" }));
    expect(onRemove).toHaveBeenCalledWith("extensions:pdf");
  });
});
