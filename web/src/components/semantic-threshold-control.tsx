import { CircleHelp } from "lucide-react";

import { Input } from "@/components/ui/input";

export function parseMinimumSimilarity(value: string): number | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 1 ? parsed : null;
}

export function SemanticThresholdControl({ value, onChange, disabled = false }: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
      <span className="hidden xl:inline">Min similarity</span>
      <span
        tabIndex={0}
        aria-label="About minimum semantic similarity"
        className="group relative inline-flex rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CircleHelp aria-hidden="true" className="size-4" />
        <span
          id="minimum-semantic-similarity-help"
          role="tooltip"
          className="pointer-events-none absolute left-0 top-full z-50 mt-2 hidden w-72 max-w-[calc(100vw-2rem)] rounded-md border bg-popover px-3 py-2 text-left text-xs leading-5 text-popover-foreground shadow-lg group-hover:block group-focus:block"
        >
          Optional cosine similarity cutoff from 0 to 1. Lower values include more loosely related documents; higher values return fewer, more closely related documents. Leave blank to use the configured candidate limit.
        </span>
      </span>
      <Input
        type="number"
        min="0"
        max="1"
        step="0.001"
        inputMode="decimal"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="Any"
        aria-label="Minimum semantic similarity"
        aria-describedby="minimum-semantic-similarity-help"
        disabled={disabled}
        className="w-20 tabular-nums"
      />
    </label>
  );
}
