import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ColumnDef } from "@tanstack/react-table";
import { afterEach, describe, expect, it } from "vitest";

import { DataTable } from "@/components/data-table";

interface Row { name: string; status: string }

const columns: ColumnDef<Row>[] = [
  { accessorKey: "name", header: "Name" },
  { accessorKey: "status", header: "Status" },
];

afterEach(cleanup);

describe("DataTable", () => {
  it("renders headings and data", () => {
    render(<DataTable columns={columns} data={[{ name: "Acme", status: "ACTIVE" }]} emptyMessage="No clients" />);
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Acme" })).toBeInTheDocument();
    expect(screen.getByText("Acme")).toHaveClass("text-ellipsis");
  });

  it("resizes columns with an accessible keyboard handle", () => {
    const view = render(<DataTable columns={columns} data={[{ name: "Acme", status: "ACTIVE" }]} emptyMessage="No clients" />);
    const nameHeader = view.getByRole("columnheader", { name: "Name" });

    expect(nameHeader).toHaveStyle({ width: "160px" });
    fireEvent.keyDown(view.getByRole("separator", { name: "Resize Name column" }), { key: "ArrowRight" });
    expect(nameHeader).toHaveStyle({ width: "170px" });
  });

  it("fits all resizable columns inside the available width", () => {
    const view = render(<DataTable columns={columns} data={[{ name: "Acme", status: "ACTIVE" }]} emptyMessage="No clients" fitToWidth />);
    const table = view.container.querySelector("table");
    const nameHeader = view.getByRole("columnheader", { name: "Name" });

    expect(table).toHaveStyle({ width: "100%" });
    expect(nameHeader).toHaveStyle({ width: "50%" });
    fireEvent.keyDown(view.getByRole("separator", { name: "Resize Name column" }), { key: "ArrowRight" });
    expect(nameHeader).toHaveStyle({ width: "51.515151515151516%" });
  });

  it("renders an empty state", () => {
    render(<DataTable columns={columns} data={[]} emptyMessage="No clients" />);
    expect(screen.getByText("No clients")).toBeInTheDocument();
  });
});
