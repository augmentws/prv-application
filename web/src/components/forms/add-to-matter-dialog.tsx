"use client";

import { FolderInput, LoaderCircle } from "lucide-react";
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
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { MatterRead } from "@/generated/models";

export function AddToMatterDialog({
  matters,
  selectionDescription,
  triggerLabel,
  disabled,
  onAdd,
}: {
  matters: MatterRead[];
  selectionDescription: string;
  triggerLabel: string;
  disabled?: boolean;
  onAdd: (matterId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [matterId, setMatterId] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!matterId) return;
    setIsSubmitting(true);
    setError(null);
    try {
      await onAdd(matterId);
      setOpen(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "The import job could not be created.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button disabled={disabled}><FolderInput />{triggerLabel}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add documents to a matter</DialogTitle>
          <DialogDescription>
            {selectionDescription} A background job will freeze this selection and add it without copying the source files.
          </DialogDescription>
        </DialogHeader>
        {matters.length ? (
          <div className="space-y-2">
            <Label htmlFor="destination-matter">Destination matter</Label>
            <Select value={matterId} onValueChange={setMatterId}>
              <SelectTrigger id="destination-matter"><SelectValue placeholder="Choose a matter" /></SelectTrigger>
              <SelectContent>
                {matters.map((matter) => <SelectItem key={matter.id} value={matter.id}>{matter.name}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <p className="rounded-lg border bg-muted/40 p-4 text-sm text-muted-foreground">
            Create a matter for this client before adding documents.
          </p>
        )}
        {error ? <p role="alert" className="mt-4 text-sm text-destructive">{error}</p> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="button" disabled={!matterId || isSubmitting} onClick={submit}>
            {isSubmitting ? <LoaderCircle className="animate-spin" /> : <FolderInput />}
            Start job
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
