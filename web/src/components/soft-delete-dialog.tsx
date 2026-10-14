"use client";

import { LoaderCircle, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export function SoftDeleteDialog({
  open,
  onOpenChange,
  resourceType,
  resourceName,
  onDelete,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  resourceType: "batch" | "job";
  resourceName: string;
  onDelete: () => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    setSubmitting(true);
    setError(null);
    try {
      await onDelete();
      onOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : `The ${resourceType} could not be deleted.`);
    } finally {
      setSubmitting(false);
    }
  }

  return <Dialog open={open} onOpenChange={(next) => {
    onOpenChange(next);
    if (!next) setError(null);
  }}>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Delete {resourceType}?</DialogTitle>
        <DialogDescription>
          {resourceName} will disappear from normal views. Its documents, results, coding, artifacts, and audit history will be retained.
        </DialogDescription>
      </DialogHeader>
      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error}</p> : null}
      <DialogFooter>
        <Button type="button" variant="outline" disabled={submitting} onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button type="button" variant="destructive" disabled={submitting} onClick={() => void remove()}>
          {submitting ? <LoaderCircle className="animate-spin" /> : <Trash2 />}
          {submitting ? "Deleting…" : `Delete ${resourceType}`}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
