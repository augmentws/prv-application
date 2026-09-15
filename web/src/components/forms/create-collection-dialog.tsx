"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { FolderPlus, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

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
import { Textarea } from "@/components/ui/textarea";

const schema = z.object({
  name: z.string().trim().min(2, "Enter a collection name").max(200),
  description: z.string().trim().max(4000, "Keep the description under 4,000 characters").optional(),
});

export type CreateCollectionValues = z.infer<typeof schema>;

export function CreateCollectionDialog({
  onCreate,
}: {
  onCreate: (values: CreateCollectionValues) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<CreateCollectionValues>({ resolver: zodResolver(schema) });

  async function submit(values: CreateCollectionValues) {
    try {
      await onCreate(values);
      reset();
      setOpen(false);
    } catch (error) {
      setError("root", {
        message: error instanceof Error ? error.message : "Collection could not be created.",
      });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button><FolderPlus />Create collection</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create collection</DialogTitle>
          <DialogDescription>Create a client-level evidence collection for imports and processing.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit(submit)}>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="collection-name">Name</Label>
              <Input id="collection-name" autoFocus placeholder="Initial collection" {...register("name")} />
              {errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}
            </div>
            <div className="space-y-2">
              <Label htmlFor="collection-description">Description <span className="font-normal text-muted-foreground">(optional)</span></Label>
              <Textarea
                id="collection-description"
                placeholder="What this collection contains and where it came from"
                {...register("description")}
              />
              {errors.description ? <p className="text-sm text-destructive">{errors.description.message}</p> : null}
            </div>
          </div>
          {errors.root ? <p role="alert" className="mt-4 text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? <LoaderCircle className="animate-spin" /> : null}
              Create collection
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
