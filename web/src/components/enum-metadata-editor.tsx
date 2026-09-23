"use client";

import { Ban, Braces, LoaderCircle, Plus, Save } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { EnumValue, MetadataDefinitionRead, MetadataEnumValueCreate, MetadataEnumValueUpdate } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

type ValueDraft = { label: string; description: string };

function draftsFor(values: EnumValue[] | null): Record<string, ValueDraft> {
  return Object.fromEntries((values ?? []).map((value) => [
    value.key,
    { label: value.label, description: value.description ?? "" },
  ]));
}

export function EnumMetadataEditor({ matterId, definition, onChange }: {
  matterId: string;
  definition: MetadataDefinitionRead;
  onChange: (definition: MetadataDefinitionRead) => void;
}) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState(definition);
  const [drafts, setDrafts] = useState<Record<string, ValueDraft>>(() => draftsFor(definition.allowed_values));
  const [newValue, setNewValue] = useState<MetadataEnumValueCreate>({ key: "", label: "", description: null });
  const [saving, setSaving] = useState<string>();
  const [confirmDeactivate, setConfirmDeactivate] = useState<string>();
  const [error, setError] = useState<string>();

  function changeOpen(nextOpen: boolean) {
    setOpen(nextOpen);
    setError(undefined);
    setConfirmDeactivate(undefined);
    if (nextOpen) {
      setCurrent(definition);
      setDrafts(draftsFor(definition.allowed_values));
      setNewValue({ key: "", label: "", description: null });
    }
  }

  function applyUpdate(updated: MetadataDefinitionRead) {
    setCurrent(updated);
    setDrafts(draftsFor(updated.allowed_values));
    onChange(updated);
  }

  async function request(path: string, method: "POST" | "PATCH", body?: MetadataEnumValueCreate | MetadataEnumValueUpdate) {
    return coreApi<MetadataDefinitionRead>(path, {
      method,
      body: body ? JSON.stringify(body) : undefined,
    });
  }

  async function addValue() {
    const key = newValue.key.trim();
    const label = newValue.label.trim();
    if (!/^[A-Za-z][A-Za-z0-9_-]{0,99}$/.test(key)) {
      setError("Value keys must begin with a letter and contain only letters, numbers, underscores, or hyphens.");
      return;
    }
    if (!label) {
      setError("Enter a display label for the new value.");
      return;
    }
    setSaving("new");
    setError(undefined);
    try {
      const updated = await request(
        `/v1/matters/${matterId}/metadata-definitions/${definition.id}/enum-values`,
        "POST",
        { key, label, description: newValue.description?.trim() || null },
      );
      applyUpdate(updated);
      setNewValue({ key: "", label: "", description: null });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The enum value could not be added.");
    } finally {
      setSaving(undefined);
    }
  }

  async function saveValue(value: EnumValue) {
    const draft = drafts[value.key];
    const label = draft?.label.trim();
    if (!label) {
      setError("Every active enum value needs a display label.");
      return;
    }
    setSaving(value.key);
    setError(undefined);
    try {
      const updated = await request(
        `/v1/matters/${matterId}/metadata-definitions/${definition.id}/enum-values/${encodeURIComponent(value.key)}`,
        "PATCH",
        { label, description: draft.description.trim() || null },
      );
      applyUpdate(updated);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The enum value could not be updated.");
    } finally {
      setSaving(undefined);
    }
  }

  async function deactivateValue(value: EnumValue) {
    setSaving(value.key);
    setError(undefined);
    try {
      const updated = await request(
        `/v1/matters/${matterId}/metadata-definitions/${definition.id}/enum-values/${encodeURIComponent(value.key)}/deactivate`,
        "POST",
      );
      applyUpdate(updated);
      setConfirmDeactivate(undefined);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The enum value could not be deactivated.");
    } finally {
      setSaving(undefined);
    }
  }

  const values = current.allowed_values ?? [];

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild><Button type="button" size="sm" variant="outline"><Braces />Manage values</Button></DialogTrigger>
      <DialogContent className="max-w-4xl">
        <DialogHeader>
          <DialogTitle>{current.display_name} values</DialogTitle>
          <DialogDescription>Edit labels and descriptions, add values, or deactivate values that should no longer be assigned. Stable keys cannot be changed.</DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          {values.length ? values.map((value) => {
            const active = value.active !== false;
            const draft = drafts[value.key] ?? { label: value.label, description: value.description ?? "" };
            const changed = draft.label.trim() !== value.label || (draft.description.trim() || null) !== (value.description || null);
            return (
              <section key={value.key} className={`rounded-lg border p-4 ${active ? "bg-card" : "bg-muted/35"}`}>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2"><code className="text-xs font-semibold">{value.key}</code><Badge variant={active ? "active" : "outline"}>{active ? "Active" : "Inactive"}</Badge></div>
                  {active ? <div className="flex items-center gap-2">
                    {confirmDeactivate === value.key ? <><span className="text-xs text-destructive">Stop offering this value?</span><Button type="button" size="sm" variant="ghost" onClick={() => setConfirmDeactivate(undefined)}>Cancel</Button><Button type="button" size="sm" variant="destructive" disabled={Boolean(saving)} onClick={() => deactivateValue(value)}>{saving === value.key ? <LoaderCircle className="animate-spin" /> : <Ban />}Confirm</Button></> : <Button type="button" size="sm" variant="ghost" disabled={Boolean(saving)} onClick={() => setConfirmDeactivate(value.key)}><Ban />Deactivate</Button>}
                  </div> : null}
                </div>
                <div className="grid gap-3 md:grid-cols-[minmax(12rem,0.8fr)_minmax(16rem,1.2fr)_auto]">
                  <div className="space-y-1.5"><Label htmlFor={`enum-label-${value.key}`}>Display label</Label><Input id={`enum-label-${value.key}`} value={draft.label} disabled={!active || Boolean(saving)} maxLength={200} onChange={(event) => setDrafts((currentDrafts) => ({ ...currentDrafts, [value.key]: { ...draft, label: event.target.value } }))} /></div>
                  <div className="space-y-1.5"><Label htmlFor={`enum-description-${value.key}`}>Description</Label><Input id={`enum-description-${value.key}`} value={draft.description} disabled={!active || Boolean(saving)} maxLength={2000} placeholder="Optional reviewer guidance" onChange={(event) => setDrafts((currentDrafts) => ({ ...currentDrafts, [value.key]: { ...draft, description: event.target.value } }))} /></div>
                  <div className="flex items-end"><Button type="button" size="sm" disabled={!active || !changed || Boolean(saving) || !draft.label.trim()} onClick={() => saveValue(value)}>{saving === value.key ? <LoaderCircle className="animate-spin" /> : <Save />}Save</Button></div>
                </div>
              </section>
            );
          }) : <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">This enum field has no values.</p>}
        </div>

        <section className="rounded-lg border bg-muted/20 p-4">
          <h3 className="font-semibold">Add value</h3>
          <p className="mt-1 text-xs text-muted-foreground">The stable key is stored on documents and cannot be renamed later.</p>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="space-y-1.5"><Label htmlFor="new-enum-key">Stable key</Label><Input id="new-enum-key" value={newValue.key} disabled={Boolean(saving)} maxLength={100} placeholder="needs_follow_up" onChange={(event) => setNewValue((currentValue) => ({ ...currentValue, key: event.target.value }))} /></div>
            <div className="space-y-1.5"><Label htmlFor="new-enum-label">Display label</Label><Input id="new-enum-label" value={newValue.label} disabled={Boolean(saving)} maxLength={200} placeholder="Needs follow-up" onChange={(event) => setNewValue((currentValue) => ({ ...currentValue, label: event.target.value }))} /></div>
            <div className="space-y-1.5 md:col-span-2"><Label htmlFor="new-enum-description">Description</Label><Textarea id="new-enum-description" value={newValue.description ?? ""} disabled={Boolean(saving)} maxLength={2000} rows={2} placeholder="Optional reviewer guidance" onChange={(event) => setNewValue((currentValue) => ({ ...currentValue, description: event.target.value }))} /></div>
          </div>
          <Button type="button" className="mt-3" size="sm" disabled={Boolean(saving) || !newValue.key.trim() || !newValue.label.trim()} onClick={addValue}>{saving === "new" ? <LoaderCircle className="animate-spin" /> : <Plus />}Add value</Button>
        </section>

        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Done</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
