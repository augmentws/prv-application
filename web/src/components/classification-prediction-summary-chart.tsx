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

import type { ClassificationPredictionClassCount, ClassificationPredictionSummaryRead } from "@/generated/models";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export const PREDICTION_THRESHOLD_OPTIONS = ["policy", "0.50", "0.60", "0.70", "0.80", "0.90", "0.95"] as const;
export type PredictionThreshold = typeof PREDICTION_THRESHOLD_OPTIONS[number];

function compactCount(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function shortLabel(value: string) {
  return value.length > 30 ? `${value.slice(0, 28)}…` : value;
}

function PredictionTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  const item = payload?.[0]?.payload as ClassificationPredictionClassCount | undefined;
  if (!active || !item) return null;
  return <div className="max-w-xs rounded-md border bg-popover px-3 py-2 text-popover-foreground shadow-md">
    <p className="break-words text-xs text-muted-foreground">{item.label}</p>
    <p className="mt-0.5 text-sm font-medium">{item.document_count.toLocaleString()} {item.document_count === 1 ? "document" : "documents"}</p>
  </div>;
}

export function ClassificationPredictionSummaryChart({
  summary,
  threshold,
  onThresholdChange,
}: {
  summary: ClassificationPredictionSummaryRead;
  threshold: PredictionThreshold;
  onThresholdChange: (value: PredictionThreshold) => void;
}) {
  const classCounts = [...summary.class_counts].sort(
    (left, right) => right.document_count - left.document_count || left.label.localeCompare(right.label),
  );
  const chartHeight = Math.max(288, classCounts.length * 36);

  return <div className="mt-4 space-y-3 border-t pt-3">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p className="text-sm font-semibold">Predictions by class</p>
        <p className="mt-0.5 text-xs text-muted-foreground">
          {summary.minimum_probability == null
            ? "Document count whose resolved prediction includes each class."
            : `Document count where each class probability is at least ${Math.round(summary.minimum_probability * 100)}%.`}{" "}
          A multi-label document can appear in more than one class.
        </p>
      </div>
      <Select value={threshold} onValueChange={(value) => onThresholdChange(value as PredictionThreshold)}>
        <SelectTrigger className="w-[168px]" aria-label="Prediction confidence threshold">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="policy">Model policy</SelectItem>
          {PREDICTION_THRESHOLD_OPTIONS.slice(1).map((value) => (
            <SelectItem key={value} value={value}>Probability ≥ {Math.round(Number(value) * 100)}%</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
    <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
      <span><strong className="font-semibold text-foreground">{summary.predicted_document_count.toLocaleString()}</strong> documents with a prediction</span>
      <span><strong className="font-semibold text-foreground">{summary.no_prediction_document_count.toLocaleString()}</strong> evaluated with no resolved class</span>
      <span><strong className="font-semibold text-foreground">{summary.predicted_value_count.toLocaleString()}</strong> predicted values</span>
    </div>
    {classCounts.length ? <div className="relative w-full min-w-0" style={{ height: chartHeight }} aria-label="Prediction documents per class">
      <ul className="sr-only">
        {classCounts.map((item) => <li key={item.class_key}>{item.label}: {item.document_count.toLocaleString()} documents</li>)}
      </ul>
      <ResponsiveContainer width="100%" height="100%" minWidth={0} initialDimension={{ width: 960, height: chartHeight }}>
        <BarChart data={classCounts} layout="vertical" margin={{ top: 8, right: 72, bottom: 8, left: 8 }} accessibilityLayer>
          <CartesianGrid horizontal={false} stroke="var(--border)" strokeOpacity={0.7} />
          <XAxis type="number" axisLine={false} tickLine={false} allowDecimals={false} height={28} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} tickFormatter={compactCount} />
          <YAxis type="category" dataKey="label" axisLine={false} tickLine={false} interval={0} width={205} tick={{ fill: "var(--muted-foreground)", fontSize: 11 }} tickFormatter={(value) => shortLabel(String(value))} />
          <Tooltip content={<PredictionTooltip />} cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }} />
          <Bar dataKey="document_count" fill="var(--primary)" fillOpacity={0.78} radius={[0, 4, 4, 0]} maxBarSize={22} isAnimationActive={false}>
            <LabelList dataKey="document_count" position="right" fill="var(--muted-foreground)" fontSize={11} formatter={(value: unknown) => Number(value).toLocaleString()} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div> : <p className="py-6 text-center text-sm text-muted-foreground">No resolved class predictions were produced.</p>}
  </div>;
}
