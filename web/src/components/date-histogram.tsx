"use client";

import { Button } from "@/components/ui/button";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";

export type DateHistogramInterval = "week" | "month" | "year";

export interface DateHistogramBucket {
  start: string;
  count: number;
}

interface ChartBucket extends DateHistogramBucket {
  range: { from: string; to: string };
  label: string;
  selected: boolean;
  timestamp: number;
}

const MAX_FILLED_BUCKETS = 2_000;

export function bucketDateRange(start: string, interval: DateHistogramInterval) {
  const from = start.slice(0, 10);
  const end = new Date(`${from}T00:00:00.000Z`);
  if (interval === "week") end.setUTCDate(end.getUTCDate() + 7);
  else if (interval === "month") end.setUTCMonth(end.getUTCMonth() + 1);
  else end.setUTCFullYear(end.getUTCFullYear() + 1);
  end.setUTCDate(end.getUTCDate() - 1);
  return { from, to: end.toISOString().slice(0, 10) };
}

export function DateHistogram({
  buckets,
  interval,
  onIntervalChange,
  onSelect,
  selectedFrom = "",
  selectedTo = "",
  loading = false,
  error = false,
  compact = false,
}: {
  buckets: DateHistogramBucket[];
  interval: DateHistogramInterval;
  onIntervalChange: (interval: DateHistogramInterval) => void;
  onSelect?: (range: { from: string; to: string }) => void;
  selectedFrom?: string;
  selectedTo?: string;
  loading?: boolean;
  error?: boolean;
  compact?: boolean;
}) {
  const displayBuckets = buckets.length > MAX_FILLED_BUCKETS
    ? buckets.filter((bucket) => bucket.count > 0)
    : buckets;
  const chartData = displayBuckets
    .map((bucket): ChartBucket => {
      const range = bucketDateRange(bucket.start, interval);
      return {
        ...bucket,
        range,
        label: formatBucketLabel(bucket.start, interval),
        selected: Boolean(
          (selectedFrom || selectedTo)
          && (!selectedTo || range.from <= selectedTo)
          && (!selectedFrom || range.to >= selectedFrom),
        ),
        timestamp: Date.parse(bucket.start),
      };
    })
    .filter((bucket) => Number.isFinite(bucket.timestamp))
    .sort((left, right) => left.timestamp - right.timestamp);

  return (
    <div className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs font-medium text-muted-foreground">Documents by</p>
        <div className="flex gap-1" aria-label="Date histogram interval">
          {(["week", "month", "year"] as const).map((option) => (
            <Button
              key={option}
              type="button"
              size="sm"
              variant={interval === option ? "default" : "outline"}
              className="h-7 px-2 capitalize"
              aria-pressed={interval === option}
              onClick={() => onIntervalChange(option)}
            >
              {option}
            </Button>
          ))}
        </div>
      </div>
      {loading ? <p className="py-8 text-center text-sm text-muted-foreground">Loading date histogram…</p>
        : error ? <p className="py-8 text-center text-sm text-destructive">The date histogram could not be loaded.</p>
        : chartData.length ? (
          <div
            className="relative min-w-0 w-full"
            style={{ height: compact ? 176 : 320 }}
            aria-label={`${interval} document counts`}
          >
            <div className="absolute left-2 top-2 z-10">
              {chartData.filter((bucket) => bucket.count > 0).map((bucket) => (
                <button
                  key={bucket.start}
                  type="button"
                  className="sr-only focus:not-sr-only focus:block focus:rounded-md focus:border focus:bg-popover focus:px-3 focus:py-2 focus:text-sm focus:shadow-md focus:outline-none focus:ring-2 focus:ring-ring"
                  aria-label={`${bucket.label}: ${bucket.count.toLocaleString()} documents`}
                  disabled={!onSelect}
                  onClick={() => onSelect?.(bucket.range)}
                >
                  Filter to {bucket.label}
                </button>
              ))}
            </div>
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 640, height: compact ? 176 : 320 }}
            >
              <BarChart
                data={chartData}
                margin={{ top: 8, right: 8, bottom: compact ? 0 : 12, left: compact ? 0 : 12 }}
                accessibilityLayer
              >
                <CartesianGrid vertical={false} stroke="var(--border)" strokeOpacity={0.7} />
                <XAxis
                  dataKey="start"
                  axisLine={false}
                  tickLine={false}
                  tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                  tickFormatter={(value) => formatAxisLabel(String(value), interval)}
                  interval="preserveStartEnd"
                  minTickGap={compact ? 20 : 32}
                  height={compact ? 30 : 48}
                  label={compact ? undefined : {
                    value: `Calendar ${interval}`,
                    position: "insideBottom",
                    offset: -8,
                    fill: "var(--muted-foreground)",
                    fontSize: 12,
                  }}
                />
                <YAxis
                  allowDecimals={false}
                  axisLine={false}
                  tickLine={false}
                  tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                  tickFormatter={formatDocumentCount}
                  width={compact ? 42 : 64}
                  label={compact ? undefined : {
                    value: "Documents",
                    angle: -90,
                    position: "insideLeft",
                    fill: "var(--muted-foreground)",
                    fontSize: 12,
                  }}
                />
                <Tooltip
                  content={<HistogramTooltip />}
                  cursor={{ fill: "var(--muted)", fillOpacity: 0.45 }}
                />
                <Bar
                  dataKey="count"
                  maxBarSize={compact ? 18 : 30}
                  radius={[3, 3, 0, 0]}
                  isAnimationActive={false}
                  onClick={(_, index) => {
                    const bucket = chartData[index];
                    if (onSelect && bucket?.count) onSelect(bucket.range);
                  }}
                >
                  {chartData.map((bucket) => (
                    <Cell
                      key={bucket.start}
                      fill={bucket.selected ? "var(--accent)" : "var(--primary)"}
                      fillOpacity={bucket.count === 0 ? 0.25 : bucket.selected ? 1 : 0.72}
                      cursor={onSelect && bucket.count > 0 ? "pointer" : "default"}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : <p className="py-8 text-center text-sm text-muted-foreground">No documents have a date value.</p>}
    </div>
  );
}

function HistogramTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  const bucket = payload?.[0]?.payload as ChartBucket | undefined;
  if (!active || !bucket) return null;

  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="text-xs text-muted-foreground">{bucket.label}</p>
      <p className="mt-0.5 text-sm font-medium">
        {bucket.count.toLocaleString()} {bucket.count === 1 ? "document" : "documents"}
      </p>
    </div>
  );
}

function formatDocumentCount(value: number) {
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatBucketLabel(value: string, interval: DateHistogramInterval) {
  const date = new Date(value);
  const year = formatUtcYear(date);
  if (interval === "year") return year;
  const month = new Intl.DateTimeFormat(undefined, {
    month: interval === "month" ? "long" : "short",
    timeZone: "UTC",
  }).format(date);
  if (interval === "month") return `${month} ${year}`;
  return `Week of ${month} ${date.getUTCDate()}, ${year}`;
}

function formatAxisLabel(value: string, interval: DateHistogramInterval) {
  const date = new Date(value);
  const year = formatUtcYear(date);
  if (interval === "year") return year;
  const month = new Intl.DateTimeFormat(undefined, { month: "short", timeZone: "UTC" }).format(date);
  return interval === "month" ? `${month} ${year}` : `${month} ${date.getUTCDate()}, ${year}`;
}

function formatUtcYear(date: Date) {
  return String(date.getUTCFullYear()).padStart(4, "0");
}
