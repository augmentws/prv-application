"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipContentProps,
} from "recharts";

import type { ClassificationClassCount } from "@/generated/models";

export interface ClassificationDistribution {
  population_count: number;
  labeled_document_count: number;
  unlabeled_document_count: number;
  conflicting_document_count: number;
  class_counts: ClassificationClassCount[];
}

function compactCount(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function shortLabel(value: string) {
  return value.length > 30 ? `${value.slice(0, 28)}…` : value;
}

function DistributionTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  const item = payload?.[0]?.payload as ClassificationClassCount | undefined;
  if (!active || !item) return null;
  return (
    <div className="max-w-xs rounded-md border bg-popover px-3 py-2 text-popover-foreground shadow-md">
      <p className="break-words text-xs text-muted-foreground">{item.label}</p>
      <p className="mt-0.5 text-sm font-medium">
        {item.document_count.toLocaleString()} {item.document_count === 1 ? "document" : "documents"}
      </p>
    </div>
  );
}

export function ClassificationDistributionChart({
  distribution,
  loading,
  error,
  waitingForScope,
}: {
  distribution?: ClassificationDistribution;
  loading: boolean;
  error: boolean;
  waitingForScope: boolean;
}) {
  if (waitingForScope) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Choose a ready review batch to see its class distribution.</p>;
  }
  if (loading) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Loading class distribution…</p>;
  }
  if (error) {
    return <p className="py-8 text-center text-sm text-destructive">The class distribution could not be loaded.</p>;
  }
  if (!distribution?.population_count) {
    return <p className="py-8 text-center text-sm text-muted-foreground">The selected scope contains no documents.</p>;
  }

  const classCounts = [...distribution.class_counts].sort(
    (left, right) => right.document_count - left.document_count || left.label.localeCompare(right.label),
  );
  if (!distribution.labeled_document_count || !classCounts.length) {
    return (
      <div className="py-8 text-center">
        <p className="text-sm text-muted-foreground">No documents in this scope have a current code for this field.</p>
        <p className="mt-1 text-xs text-muted-foreground">{distribution.population_count.toLocaleString()} documents are in scope.</p>
      </div>
    );
  }

  const chartHeight = Math.max(288, classCounts.length * 36);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <span><strong className="font-semibold text-foreground">{distribution.labeled_document_count.toLocaleString()}</strong> labeled</span>
        <span><strong className="font-semibold text-foreground">{distribution.unlabeled_document_count.toLocaleString()}</strong> unlabeled</span>
        {distribution.conflicting_document_count ? <span><strong className="font-semibold text-destructive">{distribution.conflicting_document_count.toLocaleString()}</strong> conflicting</span> : null}
        <span>{distribution.population_count.toLocaleString()} total documents</span>
      </div>
      <div className="min-w-0 pb-2">
        <div className="relative w-full min-w-0" style={{ height: chartHeight }} aria-label="Documents per class">
          <ul className="sr-only">
            {classCounts.map((item) => <li key={item.class_key}>{item.label}: {item.document_count.toLocaleString()} documents</li>)}
          </ul>
          <ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 960, height: chartHeight }}>
            <BarChart data={classCounts} layout="vertical" margin={{ top: 8, right: 72, bottom: 8, left: 8 }} accessibilityLayer>
              <CartesianGrid horizontal={false} stroke="var(--border)" strokeOpacity={0.7} />
              <XAxis
                type="number"
                axisLine={false}
                tickLine={false}
                allowDecimals={false}
                height={28}
                tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                tickFormatter={compactCount}
              />
              <YAxis
                type="category"
                dataKey="label"
                axisLine={false}
                tickLine={false}
                interval={0}
                width={205}
                tick={{ fill: "var(--muted-foreground)", fontSize: 11 }}
                tickFormatter={(value) => shortLabel(String(value))}
              />
              <Tooltip content={<DistributionTooltip />} cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }} />
              <Bar dataKey="document_count" fill="var(--primary)" fillOpacity={0.78} radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive={false}>
                <LabelList
                  dataKey="document_count"
                  position="right"
                  fill="var(--muted-foreground)"
                  fontSize={11}
                  formatter={(value: unknown) => Number(value).toLocaleString()}
                />
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
