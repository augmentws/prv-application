"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Beaker, Bot, Play, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { HelpLink } from "@/components/help-link";
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
  match_pattern: string;
  stop_pattern: string;
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
  rules_snapshot: ProcessingRule[];
  disabled_rule_ids: string[];
  total_count: number;
  processed_count: number;
  created_count: number;
  reused_count: number;
  skipped_count: number;
  failed_count: number;
  error_message: string | null;
  created_at: string;
}

interface AgentPackage {
  id: string;
  key: string;
  name: string;
  description: string | null;
  version: {
    usage_instructions: string | null;
    scope_types: string[];
  };
}

interface CleanerProposal {
  status: "CLARIFICATION" | "PROPOSAL";
  clarifying_question: string | null;
  explanation: string;
  rule: ProcessingRule | null;
  replace_rule_id: string | null;
}

interface AgentInvokeResponse {
  output: CleanerProposal;
}

export function CollectionProcessingPanel({ collectionId }: { collectionId: string }) {
  const queryClient = useQueryClient();
  const [draftRules, setDraftRules] = useState<ProcessingRule[] | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [disabledTestRuleIds, setDisabledTestRuleIds] = useState<string[]>([]);
  const [disabledRunRuleIds, setDisabledRunRuleIds] = useState<string[]>([]);
  const [testResults, setTestResults] = useState<ProcessingTestItem[]>([]);
  const [builderSelectedIds, setBuilderSelectedIds] = useState<string[]>([]);
  const [builderOpen, setBuilderOpen] = useState(false);
  const [builderInstruction, setBuilderInstruction] = useState("");
  const [clarificationHistory, setClarificationHistory] = useState<{ question: string; answer: string }[]>([]);
  const [clarificationAnswer, setClarificationAnswer] = useState("");
  const [cleanerProposal, setCleanerProposal] = useState<CleanerProposal | null>(null);
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
  const agentPackages = useQuery({
    queryKey: ["agent-packages", "COLLECTION", collectionId],
    queryFn: () => coreApi<AgentPackage[]>(`/v1/agent-packages?scope_type=COLLECTION&scope_id=${collectionId}`),
  });

  const savedRulesJson = useMemo(() => JSON.stringify(profile.data?.rules ?? []), [profile.data?.rules]);
  const rules = draftRules ?? profile.data?.rules ?? [];
  const dirty = JSON.stringify(rules) !== savedRulesJson;
  const selectedTestRules = rules.filter((rule) => rule.enabled && !disabledTestRuleIds.includes(rule.id));
  const incompleteTestRules = selectedTestRules.filter((rule) => (
    !rule.id || !rule.name.trim() || !rule.pattern.trim() || (rule.action === "REMOVE_BLOCK" && !rule.end_pattern?.trim())
  ));

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
      body: JSON.stringify({
        item_ids: selectedIds,
        rules: selectedTestRules,
        disabled_rule_ids: disabledTestRuleIds.filter((id) => profile.data?.default_rules.some((rule) => rule.id === id)),
      }),
    }),
    onSuccess: (value) => {
      setTestResults(value.items);
      setBuilderSelectedIds([]);
      setBuilderOpen(false);
      setCleanerProposal(null);
      setClarificationHistory([]);
      setClarificationAnswer("");
    },
    onError: (error) => toast.error(error.message),
  });
  const startRun = useMutation({
    mutationFn: () => coreApi<ProcessingRun>(`/v1/collections/${collectionId}/text-processing/runs`, {
      method: "POST",
      body: JSON.stringify({
        enabled_rule_ids: [
          ...(profile.data?.default_rules ?? []).filter((rule) => !disabledRunRuleIds.includes(rule.id)).map((rule) => rule.id),
          ...rules.filter((rule) => rule.enabled && !disabledRunRuleIds.includes(rule.id)).map((rule) => rule.id),
        ],
      }),
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["collection-processing-runs", collectionId] });
      toast.success("Full collection processing started.");
    },
    onError: (error) => toast.error(error.message),
  });
  const cleanerAgent = agentPackages.data?.find((agent) => agent.key === "document_cleaner");
  const selectedBuilderDocuments = testResults.filter((result) => builderSelectedIds.includes(result.item_id));
  const invokeCleaner = useMutation({
    mutationFn: (history: { question: string; answer: string }[]) => coreApi<AgentInvokeResponse>(`/v1/agents/${cleanerAgent!.id}:invoke`, {
      method: "POST",
      body: JSON.stringify({
        scope: { type: "COLLECTION", id: collectionId },
        input: {
          instruction: builderInstruction.trim(),
          documents: selectedBuilderDocuments.map((result) => ({
            item_id: result.item_id,
            filename: result.filename,
            original_text: excerptForAgent(result.original_text),
            normalized_text: excerptForAgent(result.normalized_text),
            changes: result.changes,
          })),
          current_rules: rules.filter(ruleIsComplete),
          clarification_history: history,
        },
      }),
    }),
    onSuccess: (value) => {
      setCleanerProposal(value.output);
      setClarificationAnswer("");
    },
    onError: (error) => toast.error(error.message),
  });
  const saveCleanerProposal = useMutation({
    mutationFn: () => {
      const proposalRule = cleanerProposal!.rule!;
      const nextRules = cleanerProposal!.replace_rule_id
        ? rules.map((rule) => rule.id === cleanerProposal!.replace_rule_id ? proposalRule : rule)
        : [...rules, proposalRule];
      return coreApi<ProcessingProfile>(`/v1/collections/${collectionId}/text-processing/profile`, {
        method: "PUT",
        body: JSON.stringify({ rules: nextRules }),
      });
    },
    onSuccess: async (value) => {
      setDraftRules(value.rules);
      setCleanerProposal(null);
      setBuilderOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["collection-processing-profile", collectionId] });
      toast.success("Proposed rule saved to the collection profile.");
    },
    onError: (error) => toast.error(error.message),
  });

  if (profile.isPending || documents.isPending || runs.isPending) return <TableLoading />;
  if (profile.error || documents.error || runs.error) return <QueryError message={profile.error?.message ?? documents.error?.message ?? runs.error?.message} />;

  const activeRun = runs.data.find((run) => ["QUEUED", "RUNNING"].includes(run.status));
  const currentDocumentIds = documents.data.items.map((document) => document.id);
  const allCurrentDocumentsSelected = currentDocumentIds.length > 0 && currentDocumentIds.every((id) => selectedIds.includes(id));

  function addRule() {
    const suffix = crypto.randomUUID().slice(0, 8);
    setTestResults([]);
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
    setTestResults([]);
    setDraftRules((draft) => (draft ?? profile.data!.rules).map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...update } : rule));
  }

  function toggleRuleForTest(ruleId: string) {
    setTestResults([]);
    setDisabledTestRuleIds((current) => current.includes(ruleId)
      ? current.filter((id) => id !== ruleId)
      : [...current, ruleId]);
  }

  function toggleRuleForRun(ruleId: string) {
    setDisabledRunRuleIds((current) => current.includes(ruleId)
      ? current.filter((id) => id !== ruleId)
      : [...current, ruleId]);
  }

  function toggleDocument(id: string) {
    setTestResults([]);
    setSelectedIds((current) => {
      if (current.includes(id)) return current.filter((value) => value !== id);
      if (current.length >= 25) {
        toast.error("Select no more than 25 documents for a synchronous test.");
        return current;
      }
      return [...current, id];
    });
  }

  function toggleAllDocuments() {
    setTestResults([]);
    setSelectedIds(allCurrentDocumentsSelected ? [] : currentDocumentIds.slice(0, 25));
  }

  function loadDifferentDocuments() {
    const documentTotal = documents.data?.total ?? 0;
    setSelectedIds([]);
    setTestResults([]);
    setDocumentOffset((current) => current + 25 >= documentTotal ? 0 : current + 25);
  }

  function toggleBuilderDocument(id: string) {
    setCleanerProposal(null);
    setClarificationHistory([]);
    setClarificationAnswer("");
    setBuilderSelectedIds((current) => current.includes(id)
      ? current.filter((value) => value !== id)
      : [...current, id]);
  }

  function continueAfterClarification() {
    if (!cleanerProposal?.clarifying_question || !clarificationAnswer.trim()) return;
    const history = [...clarificationHistory, {
      question: cleanerProposal.clarifying_question,
      answer: clarificationAnswer.trim(),
    }];
    setClarificationHistory(history);
    setCleanerProposal(null);
    invokeCleaner.mutate(history);
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
            <HelpLink topic="documentCleaner" />
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
                <dl className="mt-3 space-y-2 rounded-md bg-background/70 p-3 text-xs">
                  <div><dt className="font-semibold text-foreground">Pattern</dt><dd className="mt-1 break-all font-mono text-muted-foreground">{rule.match_pattern}</dd></div>
                  <div><dt className="font-semibold text-foreground">Stops at</dt><dd className="mt-1 text-muted-foreground">{rule.stop_pattern}</dd></div>
                </dl>
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
              <Button variant="ghost" size="icon" aria-label={`Delete ${rule.name}`} onClick={() => {
                setTestResults([]);
                setDisabledTestRuleIds((current) => current.filter((id) => id !== rule.id));
                setDraftRules((draft) => (draft ?? profile.data.rules).filter((_, ruleIndex) => ruleIndex !== index));
              }}><Trash2 /></Button>
            </div>
          ))}
        </div>
      </Card>

      <Card className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div><h2 className="text-lg font-semibold">Test documents</h2><p className="mt-1 text-sm text-muted-foreground">Select up to 25 documents from the current set. Tests use the unsaved rules currently shown above.</p></div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" disabled={!currentDocumentIds.length || documents.isFetching} onClick={toggleAllDocuments}>{allCurrentDocumentsSelected ? "Clear all" : `Select all ${currentDocumentIds.length}`}</Button>
            <Button variant="outline" disabled={documents.isFetching} onClick={loadDifferentDocuments}><RefreshCw />Load different documents</Button>
            <Button variant="outline" disabled={!selectedIds.length || Boolean(incompleteTestRules.length) || test.isPending} onClick={() => test.mutate()}><Beaker />Test {selectedIds.length || "selected"}</Button>
          </div>
        </div>
        <fieldset className="mt-4 rounded-lg border p-4" aria-label="Rules included in test">
          <legend className="px-1 text-sm font-semibold">Rules included in this test</legend>
          <p className="mb-3 text-xs text-muted-foreground">Turn off a rule for this preview only. This does not change the saved processing profile or full collection runs.</p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {profile.data.default_rules.map((rule) => (
              <label key={rule.id} className="flex items-center gap-2 rounded-md border bg-muted/20 px-3 py-2 text-sm">
                <input type="checkbox" checked={!disabledTestRuleIds.includes(rule.id)} onChange={() => toggleRuleForTest(rule.id)} className="size-4 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1 truncate" title={rule.name}>{rule.name}</span>
                <Badge variant="outline">Default</Badge>
              </label>
            ))}
            {rules.map((rule) => (
              <label key={rule.id} className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm">
                <input type="checkbox" checked={rule.enabled && !disabledTestRuleIds.includes(rule.id)} disabled={!rule.enabled} onChange={() => toggleRuleForTest(rule.id)} className="size-4 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1 truncate" title={rule.name}>{rule.name}</span>
                {!rule.enabled ? <Badge variant="outline">Profile disabled</Badge> : <Badge variant="outline">Custom</Badge>}
              </label>
            ))}
          </div>
          {incompleteTestRules.length ? <p role="alert" className="mt-3 text-xs text-destructive">Complete or turn off {incompleteTestRules.length} incomplete {incompleteTestRules.length === 1 ? "rule" : "rules"} before testing.</p> : null}
        </fieldset>
        <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {documents.data.items.map((document) => (
            <label key={document.id} className="flex min-w-0 items-center gap-3 rounded-lg border p-3 text-sm">
              <input type="checkbox" checked={selectedIds.includes(document.id)} onChange={() => toggleDocument(document.id)} className="size-4 shrink-0 accent-primary" />
              <span className="truncate" title={document.original_filename}>{document.original_filename}</span>
            </label>
          ))}
        </div>
        {testResults.length ? <div className="mt-5 space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-muted/20 p-4">
            <div>
              <p className="font-semibold">Build or improve a rule</p>
              <p className="text-sm text-muted-foreground">Select the test examples the agent should use, then describe the cleanup you want.</p>
            </div>
            <Button
              variant="outline"
              disabled={!builderSelectedIds.length || !cleanerAgent}
              onClick={() => setBuilderOpen(true)}
            ><Bot />Build/improve rule ({builderSelectedIds.length})</Button>
          </div>
          {agentPackages.error ? <p role="alert" className="text-sm text-destructive">The available agent packages could not be loaded: {agentPackages.error.message}</p> : null}
          {builderOpen ? (
            <div className="rounded-lg border border-primary/30 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="font-semibold">{cleanerAgent?.name ?? "Document Cleaner Agent"}</h3>
                  <p className="mt-1 max-w-3xl text-sm text-muted-foreground">{cleanerAgent?.version.usage_instructions}</p>
                </div>
                <Badge variant="outline">{builderSelectedIds.length} selected examples</Badge>
              </div>
              <label className="mt-4 block space-y-1 text-sm font-medium">
                What should be cleaned up?
                <Textarea
                  value={builderInstruction}
                  onChange={(event) => {
                    setBuilderInstruction(event.target.value);
                    setCleanerProposal(null);
                    setClarificationHistory([]);
                  }}
                  placeholder="For example: remove the recurring confidentiality footer, but keep ordinary signature text."
                  className="min-h-28"
                />
              </label>
              {!cleanerProposal ? (
                <Button className="mt-3" disabled={!builderInstruction.trim() || invokeCleaner.isPending} onClick={() => invokeCleaner.mutate(clarificationHistory)}>
                  <Bot />{invokeCleaner.isPending ? "Building rule…" : "Ask agent"}
                </Button>
              ) : null}
              {cleanerProposal?.status === "CLARIFICATION" ? (
                <div className="mt-4 rounded-lg border bg-muted/20 p-4">
                  <p className="font-medium">{cleanerProposal.clarifying_question}</p>
                  <p className="mt-1 text-sm text-muted-foreground">{cleanerProposal.explanation}</p>
                  <Textarea className="mt-3" value={clarificationAnswer} onChange={(event) => setClarificationAnswer(event.target.value)} placeholder="Answer the agent’s question" />
                  <Button className="mt-3" disabled={!clarificationAnswer.trim() || invokeCleaner.isPending} onClick={continueAfterClarification}>Continue</Button>
                </div>
              ) : null}
              {cleanerProposal?.status === "PROPOSAL" && cleanerProposal.rule ? (
                <div className="mt-4 rounded-lg border bg-muted/20 p-4">
                  <div className="flex flex-wrap items-center gap-2"><p className="font-semibold">{cleanerProposal.rule.name}</p><Badge variant="outline">{cleanerProposal.rule.action.toLowerCase().replaceAll("_", " ")}</Badge></div>
                  <p className="mt-2 text-sm text-muted-foreground">{cleanerProposal.explanation}</p>
                  <p className="mt-3 break-all rounded-md bg-background p-3 font-mono text-xs">{cleanerProposal.rule.pattern}</p>
                  <Button className="mt-3" disabled={rules.some((rule) => !ruleIsComplete(rule)) || saveCleanerProposal.isPending} onClick={() => saveCleanerProposal.mutate()}><Save />Save proposed rule</Button>
                  {rules.some((rule) => !ruleIsComplete(rule)) ? <p className="mt-2 text-xs text-destructive">Complete or remove unfinished rules before saving this proposal.</p> : null}
                </div>
              ) : null}
            </div>
          ) : null}
          {testResults.map((result) => (
          <div key={result.item_id} className="rounded-lg border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="flex min-w-0 items-center gap-2"><input aria-label={`Use ${result.filename} in rule builder`} type="checkbox" checked={builderSelectedIds.includes(result.item_id)} onChange={() => toggleBuilderDocument(result.item_id)} className="size-4 shrink-0 accent-primary" /><span className="truncate font-semibold">{result.filename}</span></label>
              <p className="text-xs text-muted-foreground">{result.original_char_count.toLocaleString()} → {result.normalized_char_count.toLocaleString()} characters · {result.source_role?.toLowerCase().replaceAll("_", " ") ?? "no text"}</p>
            </div>
            {result.warnings.map((warning) => <p key={warning} className="mt-2 text-sm text-destructive">{warning}</p>)}
            <div className="mt-3 grid gap-3 lg:grid-cols-2">
              <section aria-label={`Source preview for ${result.filename}`} className="min-w-0">
                <p className="flex h-5 items-center text-xs font-semibold uppercase tracking-wide text-muted-foreground">Source</p>
                <Textarea aria-label={`Source text for ${result.filename}`} readOnly value={result.original_text ?? "No supported source text"} className="mt-1 h-64 resize-y font-mono text-xs" />
              </section>
              <section aria-label={`Processed preview for ${result.filename}`} className="min-w-0">
                <div className="flex h-5 min-w-0 items-center gap-2">
                  <p className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Processed</p>
                  <div aria-label="Rule hits" className="ml-auto flex min-w-0 items-center gap-1 overflow-x-auto whitespace-nowrap">
                  {result.changes.length ? (
                    <>
                      {result.changes.map((change) => (
                        <Badge key={change.rule_id} variant="outline" title={change.rule_id} className="shrink-0 px-1.5 py-0 text-[10px] leading-4 normal-case tracking-normal">
                          {change.rule_name} · {change.match_count} {change.match_count === 1 ? "match" : "matches"}
                        </Badge>
                      ))}
                    </>
                  ) : <span className="text-[10px] font-normal normal-case tracking-normal text-muted-foreground">No rule hits</span>}
                  </div>
                </div>
                <Textarea aria-label={`Processed text for ${result.filename}`} readOnly value={result.normalized_text ?? "No output"} className="mt-1 h-64 resize-y font-mono text-xs" />
              </section>
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
        <fieldset className="mt-4 rounded-lg border p-4" aria-label="Rules included in full collection run">
          <legend className="px-1 text-sm font-semibold">Rules included in this run</legend>
          <p className="mb-3 text-xs text-muted-foreground">This selection applies only to the next full collection run. It does not change the saved processing profile.</p>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {profile.data.default_rules.map((rule) => (
              <label key={rule.id} className="flex items-center gap-2 rounded-md border bg-muted/20 px-3 py-2 text-sm">
                <input type="checkbox" checked={!disabledRunRuleIds.includes(rule.id)} disabled={Boolean(activeRun)} onChange={() => toggleRuleForRun(rule.id)} className="size-4 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1 truncate" title={rule.name}>{rule.name}</span>
                <Badge variant="outline">Default</Badge>
              </label>
            ))}
            {rules.map((rule) => (
              <label key={rule.id} className="flex items-center gap-2 rounded-md border px-3 py-2 text-sm">
                <input type="checkbox" checked={rule.enabled && !disabledRunRuleIds.includes(rule.id)} disabled={!rule.enabled || Boolean(activeRun)} onChange={() => toggleRuleForRun(rule.id)} className="size-4 shrink-0 accent-primary" />
                <span className="min-w-0 flex-1 truncate" title={rule.name}>{rule.name}</span>
                {!rule.enabled ? <Badge variant="outline">Profile disabled</Badge> : <Badge variant="outline">Custom</Badge>}
              </label>
            ))}
          </div>
        </fieldset>
        <div className="mt-4 space-y-2">
          {runs.data.length === 0 ? <p className="text-sm text-muted-foreground">No processing runs yet.</p> : runs.data.map((run) => (
            <div key={run.id} className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3">
              <div className="min-w-0"><div className="flex items-center gap-2"><StatusBadge status={run.status} /><span className="text-sm font-medium">Profile revision {run.profile_revision}</span></div><p className="mt-1 text-xs text-muted-foreground">Started {formatDate(run.created_at)} · {run.processed_count.toLocaleString()} / {run.total_count.toLocaleString()} processed · {run.created_count.toLocaleString()} created · {run.reused_count.toLocaleString()} reused · {run.skipped_count.toLocaleString()} skipped · {run.failed_count.toLocaleString()} failed</p><p className="mt-1 truncate text-xs text-muted-foreground" title={runRuleNames(run, profile.data.default_rules).join(", ")}>Rules: {runRuleNames(run, profile.data.default_rules).join(", ") || "none"}</p>{run.error_message ? <p className="mt-1 text-xs text-destructive">{run.error_message}</p> : null}</div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function runRuleNames(run: ProcessingRun, defaultRules: SystemProcessingRule[]) {
  return [
    ...defaultRules.filter((rule) => !run.disabled_rule_ids.includes(rule.id)).map((rule) => rule.name),
    ...run.rules_snapshot.map((rule) => rule.name),
  ];
}

function ruleIsComplete(rule: ProcessingRule) {
  return Boolean(rule.id && rule.name.trim() && rule.pattern.trim() && (rule.action !== "REMOVE_BLOCK" || rule.end_pattern?.trim()));
}

function excerptForAgent(value: string | null) {
  const text = value ?? "";
  if (text.length <= 3000) return text;
  return `${text.slice(0, 1500)}\n\n[... middle omitted from agent context ...]\n\n${text.slice(-1500)}`;
}
