"use client";

import { Layers3, LoaderCircle, Settings2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import type { MetadataGroupRead, ReviewBatchRead } from "@/generated/models";

export function AssignBatchCodingGroupsDialog({ batch, groups, onSave }: {
  batch: ReviewBatchRead;
  groups: MetadataGroupRead[];
  onSave: (batchId: string, codingGroupIds: string[]) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const assigned = batch.coding_groups
    .map((group) => group.source_metadata_group_id)
    .filter((groupId): groupId is string => Boolean(groupId));
  const available = groups.filter((group) => group.status === "ACTIVE" && group.scope !== "PERSONAL");
  const availableIds = new Set(available.map((group) => group.id));
  const unavailableSnapshots = batch.coding_groups.filter(
    (group) => group.source_metadata_group_id && !availableIds.has(group.source_metadata_group_id),
  );

  const changeOpen = (next: boolean) => {
    setOpen(next);
    if (next) {
      setSelected(assigned);
      setError("");
    }
  };

  const toggle = (groupId: string) => {
    setSelected((current) => current.includes(groupId)
      ? current.filter((value) => value !== groupId)
      : [...current, groupId]);
  };

  const submit = async () => {
    setSaving(true);
    setError("");
    try {
      await onSave(batch.id, selected);
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The coding groups could not be updated.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogTrigger asChild>
        <Button type="button" size="sm" variant="outline" title="Manage coding groups">
          <Settings2 />Groups
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Manage coding groups</DialogTitle>
          <DialogDescription>
            Choose the coding fields shown for {batch.name}. Removing a group does not delete values already stored in review runs.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2 sm:grid-cols-2">
          {available.map((group) => (
            <label key={group.id} className="flex items-start gap-3 rounded-lg border p-3 text-sm">
              <input
                type="checkbox"
                checked={selected.includes(group.id)}
                onChange={() => toggle(group.id)}
                className="mt-0.5 size-4 accent-primary"
              />
              <span>
                <span className="block font-semibold">{group.display_name}</span>
                <span className="text-xs text-muted-foreground">{group.definition_ids.length} fields</span>
              </span>
            </label>
          ))}
          {unavailableSnapshots.map((group) => (
            <label key={group.id} className="flex items-start gap-3 rounded-lg border p-3 text-sm">
              <input
                type="checkbox"
                checked={Boolean(group.source_metadata_group_id && selected.includes(group.source_metadata_group_id))}
                onChange={() => group.source_metadata_group_id && toggle(group.source_metadata_group_id)}
                className="mt-0.5 size-4 accent-primary"
              />
              <span>
                <span className="block font-semibold">{group.display_name}</span>
                <span className="text-xs text-muted-foreground">{group.fields.length} snapshotted fields · source unavailable</span>
              </span>
            </label>
          ))}
          {!available.length && !unavailableSnapshots.length ? <p className="text-sm text-muted-foreground">No shared coding groups are available.</p> : null}
        </div>
        <p className="text-xs text-muted-foreground"><Layers3 className="mr-1 inline size-3" />Groups are snapshotted when assigned, so later group edits do not change this batch.</p>
        {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="button" onClick={submit} disabled={saving}>
            {saving ? <LoaderCircle className="animate-spin" /> : null}Save groups
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
