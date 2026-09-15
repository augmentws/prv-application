"use client";

import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export interface FacetOption {
  value: string;
  label: string;
  count: number;
}

export interface FacetGroup {
  key: string;
  label: string;
  options: FacetOption[];
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
  onClear,
}: {
  groups: FacetGroup[];
  selected: Record<string, string[]>;
  hasFilters: boolean;
  onToggle: (groupKey: string, value: string) => void;
  onClear: () => void;
}) {
  return (
    <Card className="overflow-hidden lg:sticky lg:top-5">
      <div className="flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-sm font-semibold">Filter documents</h2>
        {hasFilters ? <Button type="button" variant="ghost" size="sm" onClick={onClear}>Clear all</Button> : null}
      </div>
      <div className="divide-y">
        {groups.map((group) => (
          <fieldset key={group.key} className="p-4">
            <legend className="mb-2 text-sm font-semibold">{group.label}</legend>
            {group.options.length ? (
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
          </fieldset>
        ))}
      </div>
    </Card>
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
