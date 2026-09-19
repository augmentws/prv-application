import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ResultPagination } from "@/components/result-pagination";

afterEach(cleanup);

function PaginationHarness({ total = 126 }: { total?: number }) {
  const [offset, setOffset] = useState(0);
  return (
    <>
      <ResultPagination total={total} offset={offset} pageSize={50} onPageChange={setOffset} />
      <output aria-label="Current offset">{offset}</output>
    </>
  );
}

describe("ResultPagination", () => {
  it("jumps directly to a page and navigates to the last page", async () => {
    const user = userEvent.setup();
    render(<PaginationHarness />);

    const pageNumber = screen.getByRole("spinbutton", { name: "Page number" });
    await user.clear(pageNumber);
    await user.type(pageNumber, "2{Enter}");
    expect(screen.getByRole("status", { name: "Current offset" })).toHaveTextContent("50");

    await user.click(screen.getByRole("button", { name: "Last page" }));
    expect(screen.getByRole("status", { name: "Current offset" })).toHaveTextContent("100");
    expect(pageNumber).toHaveValue(3);
    expect(screen.getByRole("button", { name: "Last page" })).toBeDisabled();
  });

  it("clamps entered pages to the available result range", async () => {
    const onPageChange = vi.fn();
    const user = userEvent.setup();
    render(<ResultPagination total={126} offset={0} pageSize={50} onPageChange={onPageChange} />);

    const pageNumber = screen.getByRole("spinbutton", { name: "Page number" });
    await user.clear(pageNumber);
    await user.type(pageNumber, "99");
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(onPageChange).toHaveBeenCalledWith(100);
  });
});
