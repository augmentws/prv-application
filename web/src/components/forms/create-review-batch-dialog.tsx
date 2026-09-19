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
import type { MetadataGroupRead, ReviewBatchCreate, ReviewBatchRead } from "@/generated/models";

const schema = z.object({
  name: z.string().trim().min(1, "Enter a batch name").max(200),
  description: z.string().trim().max(4000),
  selection_type: z.enum(["ALL_MATTER", "SEARCH_QUERY", "RANDOM_MATTER", "RANDOM_BATCH"]),
  query: z.string().trim().max(2000),
  source_batch_id: z.string(),
  sample_size: z.number().int().positive().max(10_000_000).optional(),
  reviewer_value_visibility: z.enum(["OWN_VALUES", "ALL_REVIEWER_VALUES"]),
  coding_group_ids: z.array(z.string()),
}).superRefine((values, context) => {
  if (values.selection_type === "SEARCH_QUERY" && !values.query) {
    context.addIssue({ code: "custom", path: ["query"], message: "Enter a keyword query" });
  }
  if (values.selection_type === "RANDOM_BATCH" && !values.source_batch_id) {
    context.addIssue({ code: "custom", path: ["source_batch_id"], message: "Choose a source batch" });
  }
});

type FormValues = z.infer<typeof schema>;

export function CreateReviewBatchDialog({ groups, batches, onCreate }: {
  groups: MetadataGroupRead[];
  batches: ReviewBatchRead[];
  onCreate: (values: ReviewBatchCreate) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, watch, formState: { errors, isSubmitting } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      name: "",
      description: "",
      selection_type: "ALL_MATTER",
      query: "",
      source_batch_id: "",
      reviewer_value_visibility: "OWN_VALUES",
      coding_group_ids: [],
    },
  });
  // React Hook Form owns field subscriptions; React Compiler safely skips this form component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const selectionType = watch("selection_type");

  async function submit(values: FormValues) {
    const payload: ReviewBatchCreate = {
      name: values.name,
      description: values.description || null,
      selection_type: values.selection_type,
      reviewer_value_visibility: values.reviewer_value_visibility,
      coding_group_ids: values.coding_group_ids,
    };
    if (values.selection_type === "SEARCH_QUERY") {
      payload.search = { query: values.query, search_mode: "KEYWORD", query_fields: [], filters: [], facets: [], sort: [], offset: 0, size: 100 };
    }
    if (values.selection_type === "RANDOM_BATCH") payload.source_batch_id = values.source_batch_id;
    if (values.selection_type.startsWith("RANDOM")) payload.sample_size = values.sample_size ?? null;
    try {
      await onCreate(payload);
      reset();
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "The review batch could not be created." });
    }
  }

  const availableGroups = groups.filter((group) => group.status === "ACTIVE" && group.scope !== "PERSONAL");
  const readyBatches = batches.filter((batch) => batch.status === "READY");

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Layers3 />Create batch</Button></DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Create review batch</DialogTitle>
          <DialogDescription>Freeze a reproducible set of matter documents for human review or agent evaluation.</DialogDescription>
        </DialogHeader>
        <form className="space-y-5" onSubmit={handleSubmit(submit)}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="batch-name">Name</Label><Input id="batch-name" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
            <div className="space-y-2"><Label>Documents</Label><Controller control={control} name="selection_type" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="ALL_MATTER">All matter documents</SelectItem><SelectItem value="SEARCH_QUERY">Keyword search results</SelectItem><SelectItem value="RANDOM_MATTER">Random matter sample</SelectItem><SelectItem value="RANDOM_BATCH">Random sample from batch</SelectItem></SelectContent></Select>} /></div>
          </div>
          <div className="space-y-2"><Label htmlFor="batch-description">Description / notes</Label><Textarea id="batch-description" rows={3} placeholder="Purpose, instructions, or evaluation hypothesis" {...register("description")} /></div>
          {selectionType === "SEARCH_QUERY" ? <div className="space-y-2"><Label htmlFor="batch-query">Keyword query</Label><Input id="batch-query" placeholder="Search filename, path, metadata, and document body" {...register("query")} />{errors.query ? <p className="text-sm text-destructive">{errors.query.message}</p> : null}</div> : null}
          {selectionType === "RANDOM_BATCH" ? <div className="space-y-2"><Label>Source batch</Label><Controller control={control} name="source_batch_id" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue placeholder="Choose a ready batch" /></SelectTrigger><SelectContent>{readyBatches.map((batch) => <SelectItem key={batch.id} value={batch.id}>{batch.name} ({batch.document_count.toLocaleString()})</SelectItem>)}</SelectContent></Select>} />{errors.source_batch_id ? <p className="text-sm text-destructive">{errors.source_batch_id.message}</p> : null}</div> : null}
          {selectionType.startsWith("RANDOM") ? <div className="space-y-2"><Label htmlFor="batch-sample-size">Sample size</Label><Input id="batch-sample-size" type="number" min={1} max={10000000} placeholder="Leave empty to randomize all documents" {...register("sample_size", { setValueAs: (value) => value === "" ? undefined : Number(value) })} />{errors.sample_size ? <p className="text-sm text-destructive">{errors.sample_size.message}</p> : null}</div> : null}
          <div className="space-y-2"><Label>Reviewer visibility</Label><Controller control={control} name="reviewer_value_visibility" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="OWN_VALUES">Only the reviewer&apos;s values</SelectItem><SelectItem value="ALL_REVIEWER_VALUES">Values from all reviewers</SelectItem></SelectContent></Select>} /><p className="text-xs text-muted-foreground">Agent results remain isolated by run. This setting controls human review visibility.</p></div>
          <fieldset className="space-y-2"><legend className="text-sm font-medium">Coding groups</legend><div className="grid gap-2 sm:grid-cols-2">{availableGroups.map((group) => <label key={group.id} className="flex items-start gap-3 rounded-lg border p-3 text-sm"><input type="checkbox" value={group.id} className="mt-0.5 size-4 accent-primary" {...register("coding_group_ids")} /><span><span className="block font-semibold">{group.display_name}</span><span className="text-xs text-muted-foreground">{group.definition_ids.length} fields</span></span></label>)}</div><p className="text-xs text-muted-foreground">The selected fields are snapshotted so later group edits do not change an evaluation already in progress.</p></fieldset>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create batch</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
