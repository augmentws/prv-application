"use client";

import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

type JsonObject = Record<string, unknown>;

const CONTEXT_FIELDS = [
  "summary",
  "controlling_guidance",
  "responsiveness_scope",
  "inclusion_criteria",
  "exclusion_criteria",
  "issue_definitions",
  "key_entities",
  "date_scope",
  "terminology",
  "examples",
] as const;

export function DecisionSpecificationView({ specification, json, editable = false, onJsonChange }: {
  specification: object | null | undefined;
  json?: string;
  editable?: boolean;
  onJsonChange?: (value: string) => void;
}) {
  const [view, setView] = useState<"html" | "json">("html");
  const baseline = isObject(specification) ? specification : null;
  const serialized = json ?? (specification ? JSON.stringify(specification, null, 2) : "");
  const parsed = useMemo(() => {
    if (!serialized.trim()) return baseline;
    try {
      const value = JSON.parse(serialized) as unknown;
      return isObject(value) ? value : baseline;
    } catch {
      return baseline;
    }
  }, [serialized, baseline]);

  return <div className="space-y-3">
    <div role="tablist" aria-label="Decision Specification format" className="inline-flex rounded-md border bg-muted/40 p-1">
      <Button type="button" role="tab" aria-selected={view === "html"} variant={view === "html" ? "default" : "ghost"} size="sm" className="h-7" onClick={() => setView("html")}>HTML view</Button>
      <Button type="button" role="tab" aria-selected={view === "json"} variant={view === "json" ? "default" : "ghost"} size="sm" className="h-7" onClick={() => setView("json")}>JSON view</Button>
    </div>
    {view === "html" ? <DecisionPromptPreview specification={parsed} /> : editable ? <Textarea aria-label="Decision Specification JSON" className="min-h-[28rem] resize-y font-mono text-xs leading-5" value={serialized} onChange={(event) => onJsonChange?.(event.target.value)} placeholder="Generate a specification from the task definition." /> : <pre aria-label="Decision Specification JSON" className="max-h-[36rem] overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 text-xs">{serialized || "No Decision Specification was recorded."}</pre>}
  </div>;
}

function DecisionPromptPreview({ specification }: { specification: JsonObject | null }) {
  if (!specification) return <p className="rounded-md border p-3 text-sm text-muted-foreground">No Decision Specification was recorded.</p>;
  const rawContext = isObject(specification.decision_context) ? specification.decision_context : {};
  const context = CONTEXT_FIELDS.flatMap((key) => rawContext[key] == null || isEmpty(rawContext[key]) ? [] : [[key, rawContext[key]] as const]);
  const questions = isObject(specification.questions) ? Object.entries(specification.questions) : [];
  return <div className="space-y-4" aria-label="Decision prompt preview">
    <p className="text-xs text-muted-foreground">This preview shows only the matter context and questions sent to the decision provider. Execution policy, mappings, source references, and state-contract details remain available in JSON view.</p>
    <section className="rounded-lg border p-4" aria-label="Matter decision context">
      <h4 className="font-semibold">Matter context</h4>
      {context.length ? <dl className="mt-3 divide-y">{context.map(([key, value]) => <div key={key} className="grid gap-1 py-2 first:pt-0 last:pb-0 sm:grid-cols-[minmax(10rem,0.3fr)_minmax(0,1fr)]"><dt className="text-sm font-medium text-muted-foreground">{label(key)}</dt><dd className="min-w-0 text-sm"><PromptValue value={value} /></dd></div>)}</dl> : <p className="mt-2 text-sm text-muted-foreground">No structured matter context was recorded. The reviewed task definition is supplied as the runtime fallback.</p>}
    </section>
    <section className="space-y-3" aria-label="Decision questions">
      <div className="flex items-center justify-between gap-2"><h4 className="font-semibold">Decision questions</h4><Badge variant="outline">{questions.length}</Badge></div>
      {questions.length ? questions.map(([key, rawQuestion], index) => {
        const question = isObject(rawQuestion) ? rawQuestion : {};
        const type = typeof question.type === "string" ? question.type : "question";
        return <article key={key} className="rounded-lg border p-4">
          <div className="flex flex-wrap items-start justify-between gap-2"><div><p className="text-xs text-muted-foreground">Question {index + 1}</p><h5 className="font-medium">{key}</h5></div><Badge variant="outline">{label(type)}</Badge></div>
          <div className="mt-3"><p className="text-xs font-medium text-muted-foreground">Instructions</p><div className="mt-1 text-sm"><PromptValue value={question.instructions} /></div></div>
          {question.criteria != null && !isEmpty(question.criteria) ? <div className="mt-3"><p className="text-xs font-medium text-muted-foreground">Answer criteria</p><div className="mt-1 text-sm"><PromptValue value={question.criteria} /></div></div> : null}
        </article>;
      }) : <p className="rounded-md border p-3 text-sm text-muted-foreground">No decision questions were recorded.</p>}
    </section>
  </div>;
}

function PromptValue({ value }: { value: unknown }) {
  if (value == null) return <span className="text-muted-foreground">—</span>;
  if (typeof value === "string" || typeof value === "number") return <span className="whitespace-pre-wrap break-words">{String(value)}</span>;
  if (typeof value === "boolean") return <span>{value ? "Yes" : "No"}</span>;
  if (Array.isArray(value)) return <ul className="space-y-1">{value.map((item, index) => <li key={index} className="flex gap-2"><span className="text-muted-foreground">•</span><span className="min-w-0"><PromptValue value={item} /></span></li>)}</ul>;
  if (isObject(value)) return <dl className="space-y-1">{Object.entries(value).map(([key, item]) => <div key={key} className="grid gap-1 sm:grid-cols-[minmax(8rem,0.3fr)_minmax(0,1fr)]"><dt className="font-medium text-muted-foreground">{label(key)}</dt><dd className="min-w-0"><PromptValue value={item} /></dd></div>)}</dl>;
  return <span>{String(value)}</span>;
}

function isObject(value: unknown): value is JsonObject {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function isEmpty(value: unknown) {
  return value === "" || (Array.isArray(value) && value.length === 0) || (isObject(value) && Object.keys(value).length === 0);
}

function label(value: string) {
  return value.toLowerCase().replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
