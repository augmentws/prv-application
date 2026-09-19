import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ActiveFilterBar, DateRangeFacet, FacetSidebar } from "@/components/faceted-filter";

afterEach(cleanup);

describe("faceted filters", () => {
  it("shows counts and reports facet and active-filter actions", () => {
    const onToggle = vi.fn();
    const onOpen = vi.fn();
    const onClear = vi.fn();
    const onRemove = vi.fn();
    render(
      <>
        <FacetSidebar
          groups={[{ key: "extensions", label: "File extension", options: [{ value: "pdf", label: ".pdf", count: 12 }], loaded: true }]}
          selected={{ extensions: [] }}
          hasFilters
          onToggle={onToggle}
          onOpen={onOpen}
          onClear={onClear}
        />
        <ActiveFilterBar filters={[{ key: "extensions:pdf", label: "File extension: .pdf" }]} onRemove={onRemove} />
      </>,
    );

    const details = screen.getByText("File extension").closest("details");
    expect(details).not.toBeNull();
    details!.open = true;
    fireEvent(details!, new Event("toggle"));
    expect(onOpen).toHaveBeenCalledWith("extensions");
    expect(screen.getByRole("checkbox", { name: /\.pdf/ })).not.toBeChecked();
    expect(screen.getByText("12")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /\.pdf/ }));
    expect(onToggle).toHaveBeenCalledWith("extensions", "pdf");
    fireEvent.click(screen.getByRole("button", { name: "Clear all" }));
    expect(onClear).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Remove File extension: .pdf" }));
    expect(onRemove).toHaveBeenCalledWith("extensions:pdf");
  });

  it("opens an inline file-date range and applies changes immediately", () => {
    function DateRangeHarness() {
      const [range, setRange] = useState({ from: "", to: "" });
      return <DateRangeFacet from={range.from} to={range.to} onChange={setRange} />;
    }

    render(<DateRangeHarness />);
    fireEvent.click(screen.getByText("File date"));
    fireEvent.change(screen.getByLabelText("File date from"), { target: { value: "2024-01-01" } });
    fireEvent.change(screen.getByLabelText("File date to"), { target: { value: "2024-01-31" } });

    expect(screen.getByLabelText("File date from")).toHaveValue("2024-01-01");
    expect(screen.getByLabelText("File date to")).toHaveValue("2024-01-31");
    fireEvent.click(screen.getByRole("button", { name: "Clear dates" }));
    expect(screen.getByLabelText("File date from")).toHaveValue("");
    expect(screen.getByLabelText("File date to")).toHaveValue("");
  });
});
