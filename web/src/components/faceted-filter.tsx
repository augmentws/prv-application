"use client";

import { ChevronDown, X } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

export interface FacetOption {
  value: string;
  label: string;
  count: number;
}

export interface FacetGroup {
  key: string;
  label: string;
  options: FacetOption[];
  loaded?: boolean;
  loading?: boolean;
  error?: boolean;
}

export interface ActiveFilter {
  key: string;
  label: string;
}

export function FacetSidebar({
  groups,
  selected,
  hasFilters,
  onToggle,
  onOpen,
  onClear,
  children,
}: {
  groups: FacetGroup[];
  selected: Record<string, string[]>;
  hasFilters: boolean;
  onToggle: (groupKey: string, value: string) => void;
  onOpen?: (groupKey: string) => void;
  onClear: () => void;
  children?: ReactNode;
}) {
  const [openGroups, setOpenGroups] = useState(() => new Set(
    Object.entries(selected).filter(([, values]) => values.length > 0).map(([key]) => key),
  ));

  return (
    <Card className="overflow-hidden lg:sticky lg:top-5">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-sm font-semibold">Filter documents</h2>
        {hasFilters ? <Button type="button" variant="ghost" size="sm" onClick={onClear}>Clear all</Button> : null}
      </div>
      <div className="divide-y">
        {children}
        {groups.map((group) => {
          const selectedCount = selected[group.key]?.length ?? 0;
          return (
            <details
              key={group.key}
              className="group p-4"
              open={openGroups.has(group.key)}
              onToggle={(event) => {
                const open = event.currentTarget.open;
                setOpenGroups((current) => {
                  const next = new Set(current);
                  if (open) next.add(group.key);
                  else next.delete(group.key);
                  return next;
                });
                if (open) onOpen?.(group.key);
              }}
            >
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-sm font-semibold marker:content-none">
                <span className="min-w-0 flex-1 truncate" title={group.label}>{group.label}</span>
                {selectedCount ? <Badge variant="accent">{selectedCount}</Badge> : null}
                <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
              </summary>
              <div className="mt-3">
                {group.loading ? <p className="text-sm text-muted-foreground">Loading values…</p>
                  : group.error ? <p className="text-sm text-destructive">Values could not be loaded.</p>
                  : !group.loaded ? <p className="text-sm text-muted-foreground">Open to load values.</p>
                  : group.options.length ? (
                    <div className="max-h-56 space-y-1 overflow-y-auto pr-1">
                      {group.options.map((option) => {
                        const checked = selected[group.key]?.includes(option.value) ?? false;
                        return (
                          <label key={option.value} className="flex min-h-8 cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-sm hover:bg-muted">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => onToggle(group.key, option.value)}
                              className="size-4 shrink-0 accent-primary"
                            />
                            <span className="min-w-0 flex-1 truncate" title={option.label}>{option.label}</span>
                            <span className="font-mono text-xs tabular-nums text-muted-foreground">{option.count.toLocaleString()}</span>
                          </label>
                        );
                      })}
                    </div>
                  ) : <p className="text-sm text-muted-foreground">No values</p>}
              </div>
            </details>
          );
        })}
      </div>
    </Card>
  );
}

export function DateRangeFacet({
  label = "File date",
  from,
  to,
  onChange,
}: {
  label?: string;
  from: string;
  to: string;
  onChange: (range: { from: string; to: string }) => void;
}) {
  const [open, setOpen] = useState(Boolean(from || to));

  return (
    <details className="group p-4" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 text-sm font-semibold marker:content-none">
        <span className="min-w-0 flex-1 truncate" title={label}>{label}</span>
        {from || to ? <Badge variant="accent">1</Badge> : null}
        <ChevronDown className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="mt-3 space-y-2">
        <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <span className="w-8 shrink-0">From</span>
          <Input
            type="date"
            aria-label={`${label} from`}
            className="h-9 min-w-0 px-2 text-xs"
            value={from}
            max={to || undefined}
            onChange={(event) => onChange({ from: event.target.value, to })}
          />
        </label>
        <label className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
          <span className="w-8 shrink-0">To</span>
          <Input
            type="date"
            aria-label={`${label} to`}
            className="h-9 min-w-0 px-2 text-xs"
            value={to}
            min={from || undefined}
            onChange={(event) => onChange({ from, to: event.target.value })}
          />
        </label>
        {from || to ? (
          <Button type="button" variant="ghost" size="sm" className="w-full" onClick={() => onChange({ from: "", to: "" })}>
            Clear dates
          </Button>
        ) : null}
      </div>
    </details>
  );
}

export function ActiveFilterBar({ filters, onRemove }: { filters: ActiveFilter[]; onRemove: (key: string) => void }) {
  if (!filters.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Active filters">
      <span className="text-sm font-medium text-muted-foreground">Active:</span>
      {filters.map((filter) => (
        <Button key={filter.key} type="button" variant="outline" size="sm" aria-label={`Remove ${filter.label}`} onClick={() => onRemove(filter.key)}>
          {filter.label}<X aria-hidden="true" />
        </Button>
      ))}
    </div>
  );
}
