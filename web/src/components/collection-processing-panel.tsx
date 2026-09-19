"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Beaker, Play, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { CollectionItemSearchResponse } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

type RuleAction = "REMOVE_LINE" | "REMOVE_BLOCK" | "REPLACE";

interface ProcessingRule {
  id: string;
  name: string;
  description: string | null;
  action: RuleAction;
  pattern: string;
  end_pattern: string | null;
  replacement: string;
  case_sensitive: boolean;
  enabled: boolean;
}

interface ProcessingProfile {
  collection_id: string;
  processor_version: string;
  revision: number;
  default_rules: SystemProcessingRule[];
  rules: ProcessingRule[];
  active_run_id: string | null;
  updated_at: string | null;
}

interface SystemProcessingRule {
  id: string;
  name: string;
  description: string;
  action: string;
  match_description: string;
}

interface ProcessingTestItem {
  item_id: string;
  filename: string;
  source_role: string | null;
  original_text: string | null;
  normalized_text: string | null;
  original_char_count: number;
  normalized_char_count: number;
  changes: { rule_id: string; rule_name: string; match_count: number }[];
  warnings: string[];
}

interface ProcessingTestResponse {
  processor_version: string;
  items: ProcessingTestItem[];
}

interface ProcessingRun {
  id: string;
  status: string;
  processor_version: string;
  profile_revision: number;
  total_count: number;
  processed_count: number;
  created_count: number;
  reused_count: number;
  skipped_count: number;
  failed_count: number;
  error_message: string | null;
  created_at: string;
}

export function CollectionProcessingPanel({ collectionId }: { collectionId: string }) {
  const queryClient = useQueryClient();
  const [draftRules, setDraftRules] = useState<ProcessingRule[] | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [testResults, setTestResults] = useState<ProcessingTestItem[]>([]);
  const [documentOffset, setDocumentOffset] = useState(0);

  const profile = useQuery({
    queryKey: ["collection-processing-profile", collectionId],
    queryFn: () => coreApi<ProcessingProfile>(`/v1/collections/${collectionId}/text-processing/profile`),
  });
  const documents = useQuery({
    queryKey: ["collection-processing-documents", collectionId, documentOffset],
    queryFn: () => coreApi<CollectionItemSearchResponse>(`/v1/collections/${collectionId}/search?limit=25&offset=${documentOffset}`),
    placeholderData: (previousData) => previousData,
  });
  const runs = useQuery({
    queryKey: ["collection-processing-runs", collectionId],
    queryFn: () => coreApi<ProcessingRun[]>(`/v1/collections/${collectionId}/text-processing/runs`),
    refetchInterval: (query) => query.state.data?.some((run) => ["QUEUED", "RUNNING"].includes(run.status)) ? 3000 : false,
  });

  const savedRulesJson = useMemo(() => JSON.stringify(profile.data?.rules ?? []), [profile.data?.rules]);
  const rules = draftRules ?? profile.data?.rules ?? [];
  const dirty = JSON.stringify(rules) !== savedRulesJson;

  const save = useMutation({
    mutationFn: () => coreApi<ProcessingProfile>(`/v1/collections/${collectionId}/text-processing/profile`, {
      method: "PUT",
      body: JSON.stringify({ rules }),
    }),
    onSuccess: async (value) => {
      setDraftRules(value.rules);
      await queryClient.invalidateQueries({ queryKey: ["collection-processing-profile", collectionId] });
      toast.success("Processing rules saved.");
    },
    onError: (error) => toast.error(error.message),
  });
  const test = useMutation({
    mutationFn: () => coreApi<ProcessingTestResponse>(`/v1/collections/${collectionId}/text-processing:test`, {
      method: "POST",
      body: JSON.stringify({ item_ids: selectedIds, rules }),
    }),
    onSuccess: (value) => setTestResults(value.items),
    onError: (error) => toast.error(error.message),
  });
  const startRun = useMutation({
    mutationFn: () => coreApi<ProcessingRun>(`/v1/collections/${collectionId}/text-processing/runs`, { method: "POST" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["collection-processing-runs", collectionId] });
      toast.success("Full collection processing started.");
    },
    onError: (error) => toast.error(error.message),
  });

  if (profile.isPending || documents.isPending || runs.isPending) return <TableLoading />;
  if (profile.error || documents.error || runs.error) return <QueryError message={profile.error?.message ?? documents.error?.message ?? runs.error?.message} />;

  const activeRun = runs.data.find((run) => ["QUEUED", "RUNNING"].includes(run.status));

  function addRule() {
    const suffix = crypto.randomUUID().slice(0, 8);
    setDraftRules((draft) => [...(draft ?? profile.data!.rules), {
      id: `rule-${suffix}`,
      name: "New rule",
      description: null,
      action: "REMOVE_LINE",
      pattern: "",
      end_pattern: null,
      replacement: "",
      case_sensitive: false,
      enabled: true,
    }]);
  }

  function updateRule(index: number, update: Partial<ProcessingRule>) {
    setDraftRules((draft) => (draft ?? profile.data!.rules).map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...update } : rule));
  }

  function toggleDocument(id: string) {
    setSelectedIds((current) => {
      if (current.includes(id)) return current.filter((value) => value !== id);
      if (current.length >= 10) {
        toast.error("Select no more than 10 documents for a synchronous test.");
        return current;
      }
      return [...current, id];
    });
  }

  function loadDifferentDocuments() {
    const documentTotal = documents.data?.total ?? 0;
    setSelectedIds([]);
    setTestResults([]);
    setDocumentOffset((current) => current + 25 >= documentTotal ? 0 : current + 25);
  }

  return (
    <div className="space-y-6">
      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">Text processing rules</h2>
            <p className="mt-1 max-w-3xl text-sm text-muted-foreground">
              The {profile.data.processor_version} default processor normalizes whitespace, unwraps recognized mail-forwarding envelopes, and removes quoted reply history and unmistakable attachment placeholders. Add collection-specific regular-expression rules below. Original evidence and extracted text are never changed.
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={addRule}><Plus />Add rule</Button>
            <Button disabled={!dirty || save.isPending} onClick={() => save.mutate()}><Save />Save rules</Button>
          </div>
        </div>

        <div className="mt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Default rules</h3>
            <Badge variant="outline">Read only · {profile.data.processor_version}</Badge>
          </div>
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            {profile.data.default_rules.map((rule) => (
              <div key={rule.id} className="rounded-lg border bg-muted/20 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="font-semibold">{rule.name}</p>
                  <Badge variant="outline">{rule.action.toLowerCase().replaceAll("_", " ")}</Badge>
                </div>
                <p className="mt-2 text-sm text-muted-foreground">{rule.description}</p>
                <p className="mt-2 text-xs text-muted-foreground"><span className="font-semibold text-foreground">Matches:</span> {rule.match_description}</p>
                <p className="mt-2 font-mono text-[11px] text-muted-foreground">{rule.id}</p>
              </div>
            ))}
          </div>
        </div>

        <div className="mt-6 border-t pt-5">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">Collection-specific rules</h3>
          <p className="mt-1 text-sm text-muted-foreground">These editable rules run after the default rules, in the order shown.</p>
        </div>
        <div className="mt-5 space-y-4">
          {rules.length === 0 ? (
            <div className="rounded-lg border border-dashed p-6 text-sm text-muted-foreground">No collection-specific rules. The default processor will still run.</div>
          ) : rules.map((rule, index) => (
            <div key={rule.id} className="grid gap-3 rounded-lg border p-4 lg:grid-cols-[1fr_12rem_auto]">
              <div className="space-y-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="space-y-1 text-sm font-medium">Name<Input value={rule.name} onChange={(event) => updateRule(index, { name: event.target.value })} /></label>
                  <label className="space-y-1 text-sm font-medium">Rule ID<Input value={rule.id} onChange={(event) => updateRule(index, { id: event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-") })} /></label>
                </div>
                <label className="block space-y-1 text-sm font-medium">Start / match expression<Input className="font-mono" value={rule.pattern} onChange={(event) => updateRule(index, { pattern: event.target.value })} placeholder="Regular expression" /></label>
                {rule.action === "REMOVE_BLOCK" ? <label className="block space-y-1 text-sm font-medium">End expression<Input className="font-mono" value={rule.end_pattern ?? ""} onChange={(event) => updateRule(index, { end_pattern: event.target.value })} /></label> : null}
                {rule.action === "REPLACE" ? <label className="block space-y-1 text-sm font-medium">Replacement<Input value={rule.replacement} onChange={(event) => updateRule(index, { replacement: event.target.value })} /></label> : null}
              </div>
              <div className="space-y-3">
                <label className="block space-y-1 text-sm font-medium">Action<Select value={rule.action} onValueChange={(value: RuleAction) => updateRule(index, { action: value, end_pattern: value === "REMOVE_BLOCK" ? (rule.end_pattern ?? "") : null, replacement: value === "REPLACE" ? rule.replacement : "" })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="REMOVE_LINE">Remove matching line</SelectItem><SelectItem value="REMOVE_BLOCK">Remove block</SelectItem><SelectItem value="REPLACE">Replace matches</SelectItem></SelectContent></Select></label>
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={rule.enabled} onChange={(event) => updateRule(index, { enabled: event.target.checked })} className="size-4 accent-primary" />Enabled</label>
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={rule.case_sensitive} onChange={(event) => updateRule(index, { case_sensitive: event.target.checked })} className="size-4 accent-primary" />Case sensitive</label>
              </div>
              <Button variant="ghost" size="icon" aria-label={`Delete ${rule.name}`} onClick={() => setDraftRules((draft) => (draft ?? profile.data.rules).filter((_, ruleIndex) => ruleIndex !== index))}><Trash2 /></Button>
            </div>
          ))}
        </div>
      </Card>

      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div><h2 className="text-lg font-semibold">Test documents</h2><p className="mt-1 text-sm text-muted-foreground">Select up to 10 documents from the current set of 25. Tests use the unsaved rules currently shown above.</p></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={documents.isFetching} onClick={loadDifferentDocuments}><RefreshCw />Load different documents</Button>
            <Button variant="outline" disabled={!selectedIds.length || test.isPending} onClick={() => test.mutate()}><Beaker />Test {selectedIds.length || "selected"}</Button>
          </div>
        </div>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {documents.data.items.map((document) => (
            <label key={document.id} className="flex min-w-0 items-center gap-3 rounded-lg border p-3 text-sm">
              <input type="checkbox" checked={selectedIds.includes(document.id)} onChange={() => toggleDocument(document.id)} className="size-4 shrink-0 accent-primary" />
              <span className="truncate" title={document.original_filename}>{document.original_filename}</span>
            </label>
          ))}
        </div>
        {testResults.length ? <div className="mt-5 space-y-5">{testResults.map((result) => (
          <div key={result.item_id} className="rounded-lg border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold">{result.filename}</h3><p className="text-xs text-muted-foreground">{result.original_char_count.toLocaleString()} → {result.normalized_char_count.toLocaleString()} characters · {result.source_role?.toLowerCase().replaceAll("_", " ") ?? "no text"}</p></div>
            {result.warnings.map((warning) => <p key={warning} className="mt-2 text-sm text-destructive">{warning}</p>)}
            {result.changes.length ? <p className="mt-2 text-xs text-muted-foreground">{result.changes.map((change) => `${change.rule_name} (${change.match_count})`).join(" · ")}</p> : null}
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <label className="space-y-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Source<Textarea readOnly value={result.original_text ?? "No supported source text"} className="mt-1 h-64 resize-y font-mono text-xs" /></label>
              <label className="space-y-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Processed<Textarea readOnly value={result.normalized_text ?? "No output"} className="mt-1 h-64 resize-y font-mono text-xs" /></label>
            </div>
          </div>
        ))}</div> : null}
      </Card>

      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div><h2 className="text-lg font-semibold">Full collection run</h2><p className="mt-1 text-sm text-muted-foreground">Creates versioned normalized-text artifacts. The completed run becomes the preferred source for future indexing, chunking, and embeddings.</p></div>
          <Button disabled={dirty || Boolean(activeRun) || startRun.isPending} onClick={() => startRun.mutate()}><Play />Run full collection</Button>
        </div>
        {dirty ? <p className="mt-3 text-sm text-amber-600">Save the current rules before starting a full run.</p> : null}
        <div className="mt-4 space-y-2">
          {runs.data.length === 0 ? <p className="text-sm text-muted-foreground">No processing runs yet.</p> : runs.data.map((run) => (
            <div key={run.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
              <div><div className="flex items-center gap-2"><StatusBadge status={run.status} /><span className="text-sm font-medium">Profile revision {run.profile_revision}</span></div><p className="mt-1 text-xs text-muted-foreground">Started {formatDate(run.created_at)} · {run.processed_count.toLocaleString()} / {run.total_count.toLocaleString()} processed · {run.created_count.toLocaleString()} created · {run.reused_count.toLocaleString()} reused · {run.skipped_count.toLocaleString()} skipped · {run.failed_count.toLocaleString()} failed</p>{run.error_message ? <p className="mt-1 text-xs text-destructive">{run.error_message}</p> : null}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
