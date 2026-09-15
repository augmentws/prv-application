import { render, screen } from "@testing-library/react";
import type { ColumnDef } from "@tanstack/react-table";
import { describe, expect, it } from "vitest";

import { DataTable } from "@/components/data-table";

interface Row { name: string; status: string }

const columns: ColumnDef<Row>[] = [
  { accessorKey: "name", header: "Name" },
  { accessorKey: "status", header: "Status" },
];

describe("DataTable", () => {
  it("renders headings and data", () => {
    render(<DataTable columns={columns} data={[{ name: "Acme", status: "ACTIVE" }]} emptyMessage="No clients" />);
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Acme" })).toBeInTheDocument();
  });

  it("renders an empty state", () => {
    render(<DataTable columns={columns} data={[]} emptyMessage="No clients" />);
    expect(screen.getByText("No clients")).toBeInTheDocument();
  });
});
