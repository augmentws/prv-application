import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { bucketDateRange, DateHistogram } from "@/components/date-histogram";

afterEach(cleanup);

describe("DateHistogram", () => {
  it("changes intervals and applies a clicked calendar bucket", () => {
    const onIntervalChange = vi.fn();
    const onSelect = vi.fn();
    render(
      <DateHistogram
        buckets={[
          { start: "2025-01-01T00:00:00Z", count: 4 },
          { start: "2025-02-01T00:00:00Z", count: 2 },
        ]}
        interval="month"
        onIntervalChange={onIntervalChange}
        onSelect={onSelect}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /week/i }));
    expect(onIntervalChange).toHaveBeenCalledWith("week");
    fireEvent.click(screen.getByRole("button", { name: /January 2025: 4 documents/ }));
    expect(onSelect).toHaveBeenCalledWith({ from: "2025-01-01", to: "2025-01-31" });
  });

  it("calculates inclusive week, month, and leap-year ranges", () => {
    expect(bucketDateRange("2024-02-26T00:00:00Z", "week")).toEqual({ from: "2024-02-26", to: "2024-03-03" });
    expect(bucketDateRange("2024-02-01T00:00:00Z", "month")).toEqual({ from: "2024-02-01", to: "2024-02-29" });
    expect(bucketDateRange("2024-01-01T00:00:00Z", "year")).toEqual({ from: "2024-01-01", to: "2024-12-31" });
  });

  it("drops empty buckets from oversized historical ranges", () => {
    const buckets = Array.from({ length: 2_001 }, (_, index) => ({
      start: `${String(100 + Math.floor(index / 12)).padStart(4, "0")}-${String((index % 12) + 1).padStart(2, "0")}-01T00:00:00Z`,
      count: index === 0 || index === 2_000 ? 1 : 0,
    }));

    render(
      <DateHistogram
        buckets={buckets}
        interval="month"
        onIntervalChange={vi.fn()}
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: /January 0100: 1 documents/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(5);
  });
});
