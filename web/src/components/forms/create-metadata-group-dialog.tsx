"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Layers3, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { MetadataDefinitionRead } from "@/generated/models";

const schema = z.object({
  display_name: z.string().trim().min(2, "Enter a group name").max(200),
  description: z.string().trim().max(4000).optional(),
  scope: z.enum(["PERSONAL", "MATTER"]),
  definition_ids: z.array(z.string()).min(1, "Select at least one field"),
  default_table_visible: z.boolean(),
  default_document_visible: z.boolean(),
});

export type CreateMetadataGroupValues = z.infer<typeof schema>;

export function CreateMetadataGroupDialog({
  definitions,
  onCreate,
}: {
  definitions: MetadataDefinitionRead[];
  onCreate: (values: CreateMetadataGroupValues) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, watch, formState: { errors, isSubmitting } } = useForm<CreateMetadataGroupValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      scope: "PERSONAL",
      definition_ids: [],
      default_table_visible: true,
      default_document_visible: true,
    },
  });
  // React Hook Form owns field subscriptions; React Compiler safely skips this form component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const selectedIds = watch("definition_ids");

  async function submit(values: CreateMetadataGroupValues) {
    try {
      await onCreate(values);
      reset();
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Metadata group could not be created." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Layers3 />Create group</Button></DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader><DialogTitle>Create metadata group</DialogTitle><DialogDescription>Collect existing fields into a personal or matter-wide view.</DialogDescription></DialogHeader>
        <form className="space-y-5" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="group-name">Group name</Label><Input id="group-name" autoFocus placeholder="Key review fields" {...register("display_name")} />{errors.display_name ? <p className="text-sm text-destructive">{errors.display_name.message}</p> : null}</div>
          <div className="space-y-2"><Label htmlFor="group-description">Description</Label><Textarea id="group-description" placeholder="Explain when this group is useful." {...register("description")} /></div>
          <div className="space-y-2">
            <Label>Availability</Label>
            <Controller control={control} name="scope" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="PERSONAL">Only me</SelectItem><SelectItem value="MATTER">Everyone on this matter</SelectItem></SelectContent></Select>} />
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-semibold">Fields</legend>
            <div className="grid max-h-64 gap-2 overflow-y-auto rounded-lg border p-3 sm:grid-cols-2">
              {definitions.map((definition) => (
                <label key={definition.id} className="flex items-start gap-2 rounded-md p-2 text-sm hover:bg-muted">
                  <input type="checkbox" value={definition.id} className="mt-0.5 size-4 rounded border-input accent-primary" {...register("definition_ids")} />
                  <span><span className="block font-medium">{definition.display_name}</span><span className="text-xs text-muted-foreground">{definition.key}</span></span>
                </label>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">{selectedIds.length} field{selectedIds.length === 1 ? "" : "s"} selected</p>
            {errors.definition_ids ? <p className="text-sm text-destructive">{errors.definition_ids.message}</p> : null}
          </fieldset>
          <fieldset className="grid gap-3 rounded-lg border bg-muted/25 p-4 sm:grid-cols-2"><legend className="px-1 text-sm font-semibold">Visible by default</legend><label className="flex items-center gap-2 text-sm"><input type="checkbox" className="size-4 rounded border-input accent-primary" {...register("default_table_visible")} />Results table</label><label className="flex items-center gap-2 text-sm"><input type="checkbox" className="size-4 rounded border-input accent-primary" {...register("default_document_visible")} />Document view</label></fieldset>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create group</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
