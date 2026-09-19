"use client";

import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type ColumnSizingState,
} from "@tanstack/react-table";
import { useState, type KeyboardEvent } from "react";

import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function DataTable<TData>({ columns, data, emptyMessage, fitToWidth = false }: { columns: ColumnDef<TData>[]; data: TData[]; emptyMessage: string; fitToWidth?: boolean }) {
  const [columnSizing, setColumnSizing] = useState<ColumnSizingState>({});
  // TanStack Table deliberately returns a mutable table interface; React Compiler safely skips this component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns,
    defaultColumn: { minSize: 72, size: 160, maxSize: 800 },
    columnResizeMode: "onChange",
    state: { columnSizing },
    onColumnSizingChange: setColumnSizing,
    getCoreRowModel: getCoreRowModel(),
  });
  const totalSize = table.getTotalSize();

  function columnWidth(size: number) {
    return fitToWidth ? `${(size / totalSize) * 100}%` : size;
  }

  function resizeWithKeyboard(event: KeyboardEvent<HTMLButtonElement>, columnId: string, currentSize: number, minSize: number, maxSize: number) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -10 : 10;
    setColumnSizing((current) => ({
      ...current,
      [columnId]: Math.min(maxSize, Math.max(minSize, currentSize + delta)),
    }));
  }

  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <Table className="table-fixed" style={{ width: fitToWidth ? "100%" : `max(100%, ${totalSize}px)` }}>
        <TableHeader>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-muted/50">
              {headerGroup.headers.map((header) => {
                const minSize = header.column.columnDef.minSize ?? 72;
                const maxSize = header.column.columnDef.maxSize ?? 800;
                const label = typeof header.column.columnDef.header === "string" ? header.column.columnDef.header : header.column.id;
                return (
                  <TableHead
                    key={header.id}
                    aria-label={typeof header.column.columnDef.header === "string" ? header.column.columnDef.header : undefined}
                    className="relative overflow-visible"
                    style={{ width: columnWidth(header.getSize()) }}
                  >
                    <div className="overflow-hidden text-ellipsis whitespace-nowrap">
                      {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                    </div>
                    {header.column.getCanResize() ? (
                      <button
                        type="button"
                        role="separator"
                        aria-label={`Resize ${label} column`}
                        aria-orientation="vertical"
                        aria-valuemin={minSize}
                        aria-valuemax={maxSize}
                        aria-valuenow={header.getSize()}
                        className={`absolute -right-1 top-0 z-10 h-full w-2 cursor-col-resize touch-none select-none bg-transparent outline-none after:absolute after:inset-y-2 after:left-1/2 after:w-px after:-translate-x-1/2 after:bg-border hover:after:bg-primary focus-visible:after:w-0.5 focus-visible:after:bg-ring ${header.column.getIsResizing() ? "after:w-0.5 after:bg-primary" : ""}`}
                        onDoubleClick={() => header.column.resetSize()}
                        onKeyDown={(event) => resizeWithKeyboard(event, header.column.id, header.getSize(), minSize, maxSize)}
                        onMouseDown={header.getResizeHandler()}
                        onTouchStart={header.getResizeHandler()}
                      />
                    ) : null}
                  </TableHead>
                );
              })}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.length ? (
            table.getRowModel().rows.map((row) => (
              <TableRow key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id} className="max-w-0 overflow-hidden" style={{ width: columnWidth(cell.column.getSize()) }}>
                    <div className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </div>
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : (
            <TableRow className="hover:bg-card"><TableCell colSpan={columns.length} className="h-36 text-center text-muted-foreground">{emptyMessage}</TableCell></TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
