"use client";

import { LoaderCircle, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function DeleteCollectionDialog({
  collectionName,
  onDelete,
}: {
  collectionName: string;
  onDelete: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function remove() {
    setSubmitting(true);
    setError(null);
    try {
      await onDelete();
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Collection deletion could not be queued.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => {
      setOpen(next);
      if (!next) {
        setConfirmation("");
        setError(null);
      }
    }}>
      <DialogTrigger asChild>
        <Button variant="destructive"><Trash2 />Delete collection</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {collectionName}?</DialogTitle>
          <DialogDescription>
            This queues a permanent deletion of every collection database record and every blob that is not shared by another artifact. A collection referenced by a matter cannot be deleted.
          </DialogDescription>
        </DialogHeader>
        <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
          <div className="flex gap-2">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
            <p>This operation cannot be undone after the deletion workflow starts.</p>
          </div>
        </div>
        <div className="mt-4 space-y-2">
          <Label htmlFor="delete-collection-confirmation">
            Type <span className="font-mono font-semibold">{collectionName}</span> to confirm
          </Label>
          <Input
            id="delete-collection-confirmation"
            autoComplete="off"
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
        </div>
        {error ? <p role="alert" className="mt-4 text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)} disabled={submitting}>Cancel</Button>
          <Button type="button" variant="destructive" onClick={() => void remove()} disabled={confirmation !== collectionName || submitting}>
            {submitting ? <LoaderCircle className="animate-spin" /> : <Trash2 />}
            {submitting ? "Queuing deletion…" : "Delete permanently"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
