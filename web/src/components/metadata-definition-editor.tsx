"use client";

import { Ban, LoaderCircle, PencilLine, Plus, Save } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { EnumValue, MetadataDefinitionRead, MetadataDefinitionUpdate, MetadataEnumValueCreate, MetadataEnumValueUpdate } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

type FieldDraft = {
  display_name: string;
  description: string;
  searchable: boolean;
  facetable: boolean;
  normalize_to_lowercase: boolean;
  reviewable: boolean;
  ai_assignable: boolean;
};

type ValueDraft = { label: string; description: string };

function valueDraftsFor(values: EnumValue[] | null): Record<string, ValueDraft> {
  return Object.fromEntries((values ?? []).map((value) => [
    value.key,
    { label: value.label, description: value.description ?? "" },
  ]));
}

function draftFor(definition: MetadataDefinitionRead): FieldDraft {
  return {
    display_name: definition.display_name,
    description: definition.description ?? "",
    searchable: definition.searchable,
    facetable: definition.facetable,
    normalize_to_lowercase: definition.normalize_to_lowercase ?? false,
    reviewable: definition.reviewable,
    ai_assignable: definition.ai_assignable,
  };
}

function updateFor(definition: MetadataDefinitionRead, draft: FieldDraft): MetadataDefinitionUpdate {
  const update: MetadataDefinitionUpdate = {};
  const displayName = draft.display_name.trim();
  const description = draft.description.trim() || null;
  if (displayName !== definition.display_name) update.display_name = displayName;
  if (description !== definition.description) update.description = description;
  if (draft.searchable !== definition.searchable) update.searchable = draft.searchable;
  if (draft.facetable !== definition.facetable) update.facetable = draft.facetable;
  if (draft.normalize_to_lowercase !== (definition.normalize_to_lowercase ?? false)) update.normalize_to_lowercase = draft.normalize_to_lowercase;
  if (draft.reviewable !== definition.reviewable) update.reviewable = draft.reviewable;
  if (draft.ai_assignable !== definition.ai_assignable) update.ai_assignable = draft.ai_assignable;
  return update;
}

export function MetadataDefinitionEditor({ matterId, definition, onChange }: {
  matterId: string;
  definition: MetadataDefinitionRead;
  onChange: (definition: MetadataDefinitionRead) => void;
}) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState(definition);
  const [draft, setDraft] = useState<FieldDraft>(() => draftFor(definition));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [valueDrafts, setValueDrafts] = useState<Record<string, ValueDraft>>(() => valueDraftsFor(definition.allowed_values));
  const [newValue, setNewValue] = useState<MetadataEnumValueCreate>({ key: "", label: "", description: null });
  const [valueSaving, setValueSaving] = useState<string>();
  const [confirmDeactivate, setConfirmDeactivate] = useState<string>();
  const [valueError, setValueError] = useState<string>();
  const update = updateFor(current, draft);
  const changed = Object.keys(update).length > 0;
  const canFacet = !["LONG_TEXT", "JSON"].includes(definition.type);
  const canNormalize = ["TEXT", "LONG_TEXT"].includes(definition.type);
  const hierarchyLocksSearch = Boolean(definition.hierarchy_separator);

  function changeOpen(nextOpen: boolean) {
    if (saving) return;
    setOpen(nextOpen);
    setError(undefined);
    setValueError(undefined);
    setConfirmDeactivate(undefined);
    if (nextOpen) {
      setCurrent(definition);
      setDraft(draftFor(definition));
      setValueDrafts(valueDraftsFor(definition.allowed_values));
      setNewValue({ key: "", label: "", description: null });
    }
  }

  async function save() {
    if (!draft.display_name.trim() || !changed) return;
    setSaving(true);
    setError(undefined);
    try {
      const updated = await coreApi<MetadataDefinitionRead>(`/v1/matters/${matterId}/metadata-definitions/${current.id}`, {
        method: "PATCH",
        body: JSON.stringify(update),
      });
      setCurrent(updated);
      onChange(updated);
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The metadata field could not be updated.");
    } finally {
      setSaving(false);
    }
  }

  function setBoolean(key: keyof Pick<FieldDraft, "searchable" | "facetable" | "normalize_to_lowercase" | "reviewable" | "ai_assignable">, value: boolean) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function applyValueUpdate(updated: MetadataDefinitionRead) {
    setCurrent(updated);
    setValueDrafts(valueDraftsFor(updated.allowed_values));
    onChange(updated);
  }

  async function valueRequest(path: string, method: "POST" | "PATCH", body?: MetadataEnumValueCreate | MetadataEnumValueUpdate) {
    return coreApi<MetadataDefinitionRead>(path, { method, body: body ? JSON.stringify(body) : undefined });
  }

  async function addValue() {
    const key = newValue.key.trim();
    const label = newValue.label.trim();
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,99}$/.test(key)) {
      setValueError("Value keys must begin with a letter and contain only letters, numbers, underscores, or hyphens.");
      return;
    }
    if (!label) {
      setValueError("Enter a display label for the new value.");
      return;
    }
    setValueSaving("new");
    setValueError(undefined);
    try {
      const updated = await valueRequest(
        `/v1/matters/${matterId}/metadata-definitions/${current.id}/enum-values`,
        "POST",
        { key, label, description: newValue.description?.trim() || null },
      );
      applyValueUpdate(updated);
      setNewValue({ key: "", label: "", description: null });
    } catch (caught) {
      setValueError(caught instanceof Error ? caught.message : "The enum value could not be added.");
    } finally {
      setValueSaving(undefined);
    }
  }

  async function saveValue(value: EnumValue) {
    const valueDraft = valueDrafts[value.key];
    const label = valueDraft?.label.trim();
    if (!label) {
      setValueError("Every active enum value needs a display label.");
      return;
    }
    setValueSaving(value.key);
    setValueError(undefined);
    try {
      const updated = await valueRequest(
        `/v1/matters/${matterId}/metadata-definitions/${current.id}/enum-values/${encodeURIComponent(value.key)}`,
        "PATCH",
        { label, description: valueDraft.description.trim() || null },
      );
      applyValueUpdate(updated);
    } catch (caught) {
      setValueError(caught instanceof Error ? caught.message : "The enum value could not be updated.");
    } finally {
      setValueSaving(undefined);
    }
  }

  async function deactivateValue(value: EnumValue) {
    setValueSaving(value.key);
    setValueError(undefined);
    try {
      const updated = await valueRequest(
        `/v1/matters/${matterId}/metadata-definitions/${current.id}/enum-values/${encodeURIComponent(value.key)}/deactivate`,
        "POST",
      );
      applyValueUpdate(updated);
      setConfirmDeactivate(undefined);
    } catch (caught) {
      setValueError(caught instanceof Error ? caught.message : "The enum value could not be deactivated.");
    } finally {
      setValueSaving(undefined);
    }
  }

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild><Button type="button" size="sm" variant="outline"><PencilLine />Edit</Button></DialogTrigger>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>Edit {definition.display_name}</DialogTitle>
          <DialogDescription>Update this matter-owned field and manage its controlled values in one place.</DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2"><Label htmlFor={`metadata-name-${definition.id}`}>Display name</Label><Input id={`metadata-name-${definition.id}`} value={draft.display_name} maxLength={200} onChange={(event) => setDraft((current) => ({ ...current, display_name: event.target.value }))} /></div>
          <div className="space-y-2"><Label>Stable field key</Label><Input value={definition.key} readOnly className="font-mono text-muted-foreground" /></div>
          <div className="space-y-2 sm:col-span-2"><Label htmlFor={`metadata-description-${definition.id}`}>Description</Label><Textarea id={`metadata-description-${definition.id}`} value={draft.description} maxLength={4000} rows={3} onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))} /></div>
        </div>

        <div className="grid gap-3 rounded-lg border bg-muted/20 p-3 text-sm sm:grid-cols-3">
          <div><span className="block text-xs text-muted-foreground">Type</span><span className="font-medium">{definition.type.toLowerCase().replace("_", " ")}</span></div>
          <div><span className="block text-xs text-muted-foreground">Cardinality</span><span className="font-medium">{definition.cardinality.toLowerCase()}</span></div>
          <div><span className="block text-xs text-muted-foreground">Source</span><span className="font-medium">{definition.value_source.toLowerCase()}</span></div>
        </div>

        <fieldset className="grid gap-3 rounded-lg border p-4 sm:grid-cols-2">
          <legend className="px-1 text-sm font-semibold">Behavior</legend>
          <BehaviorOption label="Searchable" checked={draft.searchable} disabled={hierarchyLocksSearch} onChange={(value) => setBoolean("searchable", value)} />
          <BehaviorOption label="Available as a filter" checked={draft.facetable} disabled={!canFacet || hierarchyLocksSearch} onChange={(value) => setBoolean("facetable", value)} />
          <BehaviorOption label="Normalize values to lowercase" checked={draft.normalize_to_lowercase} disabled={!canNormalize} onChange={(value) => setBoolean("normalize_to_lowercase", value)} />
          <BehaviorOption label="Visible to reviewers" checked={draft.reviewable} onChange={(value) => setBoolean("reviewable", value)} />
          <BehaviorOption label="Agents may assign values" description="Makes this field available as an Analysis Task destination." checked={draft.ai_assignable} onChange={(value) => setBoolean("ai_assignable", value)} />
        </fieldset>

        {current.type === "ENUM" ? <section className="space-y-3 border-t pt-5">
          <div><h3 className="font-semibold">Controlled values</h3><p className="mt-1 text-xs text-muted-foreground">Value actions apply immediately. Stable keys cannot be changed.</p></div>
          {(current.allowed_values ?? []).map((value) => {
            const active = value.active !== false;
            const valueDraft = valueDrafts[value.key] ?? { label: value.label, description: value.description ?? "" };
            const valueChanged = valueDraft.label.trim() !== value.label || (valueDraft.description.trim() || null) !== (value.description || null);
            return <div key={value.key} className={`rounded-lg border p-3 ${active ? "bg-card" : "bg-muted/35"}`}>
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><code className="text-xs font-semibold">{value.key}</code>{active ? confirmDeactivate === value.key ? <div className="flex items-center gap-2"><span className="text-xs text-destructive">Stop offering this value?</span><Button type="button" size="sm" variant="ghost" onClick={() => setConfirmDeactivate(undefined)}>Cancel</Button><Button type="button" size="sm" variant="destructive" disabled={Boolean(valueSaving)} onClick={() => void deactivateValue(value)}>{valueSaving === value.key ? <LoaderCircle className="animate-spin" /> : <Ban />}Confirm</Button></div> : <Button type="button" size="sm" variant="ghost" disabled={Boolean(valueSaving)} onClick={() => setConfirmDeactivate(value.key)}><Ban />Deactivate</Button> : <span className="text-xs text-muted-foreground">Inactive</span>}</div>
              <div className="grid gap-3 md:grid-cols-[minmax(12rem,0.8fr)_minmax(16rem,1.2fr)_auto]">
                <div className="space-y-1.5"><Label htmlFor={`enum-label-${value.key}`}>Display label</Label><Input id={`enum-label-${value.key}`} value={valueDraft.label} disabled={!active || Boolean(valueSaving)} maxLength={200} onChange={(event) => setValueDrafts((drafts) => ({ ...drafts, [value.key]: { ...valueDraft, label: event.target.value } }))} /></div>
                <div className="space-y-1.5"><Label htmlFor={`enum-description-${value.key}`}>Description</Label><Input id={`enum-description-${value.key}`} value={valueDraft.description} disabled={!active || Boolean(valueSaving)} maxLength={2000} placeholder="Optional reviewer guidance" onChange={(event) => setValueDrafts((drafts) => ({ ...drafts, [value.key]: { ...valueDraft, description: event.target.value } }))} /></div>
                <div className="flex items-end"><Button type="button" size="sm" disabled={!active || !valueChanged || Boolean(valueSaving) || !valueDraft.label.trim()} onClick={() => void saveValue(value)}>{valueSaving === value.key ? <LoaderCircle className="animate-spin" /> : <Save />}Save value</Button></div>
              </div>
            </div>;
          })}
          <div className="rounded-lg border bg-muted/20 p-4">
            <h4 className="font-semibold">Add value</h4>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <div className="space-y-1.5"><Label htmlFor="new-enum-key">Stable key</Label><Input id="new-enum-key" value={newValue.key} disabled={Boolean(valueSaving)} maxLength={100} onChange={(event) => setNewValue((value) => ({ ...value, key: event.target.value }))} /></div>
              <div className="space-y-1.5"><Label htmlFor="new-enum-label">Display label</Label><Input id="new-enum-label" value={newValue.label} disabled={Boolean(valueSaving)} maxLength={200} onChange={(event) => setNewValue((value) => ({ ...value, label: event.target.value }))} /></div>
              <div className="space-y-1.5 md:col-span-2"><Label htmlFor="new-enum-description">Description</Label><Textarea id="new-enum-description" value={newValue.description ?? ""} disabled={Boolean(valueSaving)} maxLength={2000} rows={2} onChange={(event) => setNewValue((value) => ({ ...value, description: event.target.value }))} /></div>
            </div>
            <Button type="button" className="mt-3" size="sm" disabled={Boolean(valueSaving) || !newValue.key.trim() || !newValue.label.trim()} onClick={() => void addValue()}>{valueSaving === "new" ? <LoaderCircle className="animate-spin" /> : <Plus />}Add value</Button>
          </div>
          {valueError ? <p role="alert" className="text-sm text-destructive">{valueError}</p> : null}
        </section> : null}

        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter><Button type="button" variant="outline" disabled={saving || Boolean(valueSaving)} onClick={() => changeOpen(false)}>Close</Button><Button type="button" disabled={saving || Boolean(valueSaving) || !changed || !draft.display_name.trim()} onClick={() => void save()}>{saving ? <LoaderCircle className="animate-spin" /> : <Save />}{saving ? "Saving…" : "Save field settings"}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function BehaviorOption({ label, description, checked, disabled = false, onChange }: {
  label: string;
  description?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (checked: boolean) => void;
}) {
  return <label className="flex items-start gap-2 text-sm"><input type="checkbox" aria-label={label} className="mt-0.5 size-4 rounded border-input accent-primary disabled:opacity-50" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} /><span><span className="font-medium">{label}</span>{description ? <span className="mt-0.5 block text-xs leading-5 text-muted-foreground">{description}</span> : null}</span></label>;
}
