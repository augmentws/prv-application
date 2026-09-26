"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LoaderCircle, Network } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { MatterSavedSearchRead, MatterTopicJobCreate, MetadataDefinitionRead } from "@/generated/models";

const schema = z.object({
  operating_mode: z.enum(["AUTO", "FIXED"]),
  sample_size: z.number().int().min(10).max(200_000),
  requested_topic_count: z.number().int().min(2).max(200).optional(),
  replace_existing: z.boolean(),
  scope_mode: z.enum(["ENTIRE_MATTER", "INCLUDE_SAVED_SEARCH", "EXCLUDE_SAVED_SEARCH"]),
  saved_search_id: z.string().optional(),
  destination_mode: z.enum(["TOPICS", "EXISTING_FIELD", "NEW_FIELD"]),
  existing_metadata_definition_id: z.string().optional(),
  new_field_name: z.string().max(200).optional(),
  new_field_key: z.string().max(100).optional(),
}).superRefine((values, context) => {
  if (values.operating_mode === "FIXED" && values.requested_topic_count === undefined) {
    context.addIssue({ code: "custom", path: ["requested_topic_count"], message: "Enter the number of topics" });
  }
  if (values.requested_topic_count !== undefined && values.requested_topic_count > values.sample_size) {
    context.addIssue({ code: "custom", path: ["requested_topic_count"], message: "Topic count cannot exceed sample size" });
  }
  if (values.scope_mode !== "ENTIRE_MATTER" && !values.saved_search_id) {
    context.addIssue({ code: "custom", path: ["saved_search_id"], message: "Select a saved search" });
  }
  if (values.destination_mode === "EXISTING_FIELD" && !values.existing_metadata_definition_id) {
    context.addIssue({ code: "custom", path: ["existing_metadata_definition_id"], message: "Select an existing destination field" });
  }
  if (values.destination_mode === "NEW_FIELD") {
    if (!values.new_field_name?.trim()) {
      context.addIssue({ code: "custom", path: ["new_field_name"], message: "Enter a field name" });
    }
    if (!values.new_field_key?.match(/^[a-z][a-z0-9_]{0,99}$/)) {
      context.addIssue({ code: "custom", path: ["new_field_key"], message: "Use lowercase letters, numbers, and underscores, starting with a letter" });
    }
  }
});

type FormValues = z.infer<typeof schema>;

function fieldKeyFromName(name: string) {
  return name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").replace(/^[^a-z]+/, "").slice(0, 100);
}

export function CreateTopicJobDialog({ savedSearches, definitions, onCreate, disabled }: {
  savedSearches: MatterSavedSearchRead[];
  definitions: MetadataDefinitionRead[];
  onCreate: (values: MatterTopicJobCreate) => Promise<void>;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [fieldKeyEdited, setFieldKeyEdited] = useState(false);
  const { register, control, handleSubmit, reset, setError, setValue, watch, formState: { errors, isSubmitting } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { operating_mode: "AUTO", sample_size: 10_000, replace_existing: true, scope_mode: "ENTIRE_MATTER", destination_mode: "TOPICS" },
  });
  // React Hook Form owns field subscriptions; React Compiler safely skips this form component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const mode = watch("operating_mode");
  const scopeMode = watch("scope_mode");
  const destinationMode = watch("destination_mode");
  const compatibleDefinitions = definitions.filter((definition) => definition.key !== "topics" && definition.status === "ACTIVE" && definition.value_source === "ASSERTED" && definition.type === "ENUM" && definition.cardinality === "MULTIPLE");

  async function submit(values: FormValues) {
    try {
      await onCreate({
        operating_mode: values.operating_mode,
        sample_size: values.sample_size,
        requested_topic_count: values.operating_mode === "FIXED" ? values.requested_topic_count : null,
        assignment_mode: values.replace_existing ? "REPLACE" : "APPEND",
        scope_mode: values.scope_mode,
        saved_search_id: values.scope_mode === "ENTIRE_MATTER" ? null : values.saved_search_id,
        destination_mode: values.destination_mode,
        existing_metadata_definition_id: values.destination_mode === "EXISTING_FIELD" ? values.existing_metadata_definition_id : null,
        new_field_name: values.destination_mode === "NEW_FIELD" ? values.new_field_name?.trim() : null,
        new_field_key: values.destination_mode === "NEW_FIELD" ? values.new_field_key?.trim() : null,
      });
      reset({ operating_mode: "AUTO", sample_size: 10_000, replace_existing: true, scope_mode: "ENTIRE_MATTER", destination_mode: "TOPICS" });
      setFieldKeyEdited(false);
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Topic clustering could not be started." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button disabled={disabled}><Network />Cluster topics</Button></DialogTrigger>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Cluster document topics</DialogTitle>
          <DialogDescription>Discover proposed topics from the matter&apos;s existing chunk embeddings. You will review them before anything is applied to documents.</DialogDescription>
        </DialogHeader>
        <form className="space-y-5" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2">
            <Label>Operating mode</Label>
            <Controller control={control} name="operating_mode" render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="AUTO">Automatic discovery</SelectItem>
                  <SelectItem value="FIXED">Fixed topic count</SelectItem>
                </SelectContent>
              </Select>
            )} />
            <p className="text-xs leading-5 text-muted-foreground">Automatic discovery learns the topic count from the sample. Fixed mode is useful when you need a predictable number.</p>
          </div>
          <div className="space-y-4 rounded-lg border bg-muted/20 p-4">
            <div><Label>Destination field</Label><p className="mt-1 text-xs leading-5 text-muted-foreground">Choose where approved topic values will be stored. This choice is fixed for the run.</p></div>
            <Controller control={control} name="destination_mode" render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger aria-label="Destination field option"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="TOPICS">Use Topics field</SelectItem>
                  <SelectItem value="EXISTING_FIELD" disabled={compatibleDefinitions.length === 0}>Choose an existing field</SelectItem>
                  <SelectItem value="NEW_FIELD">Create a new field</SelectItem>
                </SelectContent>
              </Select>
            )} />
            {destinationMode === "EXISTING_FIELD" ? <div className="space-y-2">
              <Label>Existing field</Label>
              <Controller control={control} name="existing_metadata_definition_id" render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger aria-label="Existing destination field"><SelectValue placeholder="Select a multi-value enum field" /></SelectTrigger>
                  <SelectContent>{compatibleDefinitions.map((definition) => <SelectItem key={definition.id} value={definition.id}>{definition.display_name}</SelectItem>)}</SelectContent>
                </Select>
              )} />
              {errors.existing_metadata_definition_id ? <p className="text-sm text-destructive">{errors.existing_metadata_definition_id.message}</p> : null}
            </div> : null}
            {destinationMode === "NEW_FIELD" ? <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2"><Label htmlFor="topic-field-name">Field name</Label><Input id="topic-field-name" maxLength={200} placeholder="Communication topics" {...register("new_field_name", { onChange: (event) => { if (!fieldKeyEdited) setValue("new_field_key", fieldKeyFromName(event.target.value), { shouldValidate: true }); } })} />{errors.new_field_name ? <p className="text-sm text-destructive">{errors.new_field_name.message}</p> : null}</div>
              <div className="space-y-2"><Label htmlFor="topic-field-key">Field key</Label><Input id="topic-field-key" maxLength={100} placeholder="communication_topics" {...register("new_field_key", { onChange: (event) => { setFieldKeyEdited(true); setValue("new_field_key", event.target.value.toLowerCase()); } })} /><p className="text-xs text-muted-foreground">Lowercase letters, numbers, and underscores.</p>{errors.new_field_key ? <p className="text-sm text-destructive">{errors.new_field_key.message}</p> : null}</div>
            </div> : null}
            {destinationMode === "TOPICS" ? <p className="text-xs text-muted-foreground">Priv-View will use the standard Topics field, creating it only after you approve the proposals.</p> : null}
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="topic-sample-size">Sample chunks</Label>
              <Input id="topic-sample-size" type="number" min={10} max={200000} step={10} {...register("sample_size", { valueAsNumber: true })} />
              {errors.sample_size ? <p className="text-sm text-destructive">{errors.sample_size.message}</p> : null}
            </div>
            {mode === "FIXED" ? (
              <div className="space-y-2">
                <Label htmlFor="topic-count">Number of topics</Label>
                <Input id="topic-count" type="number" min={2} max={200} {...register("requested_topic_count", { setValueAs: (value) => value === "" ? undefined : Number(value) })} />
                {errors.requested_topic_count ? <p className="text-sm text-destructive">{errors.requested_topic_count.message}</p> : null}
              </div>
            ) : null}
          </div>
          <div className="space-y-2">
            <Label>Document scope</Label>
            <Controller control={control} name="scope_mode" render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="ENTIRE_MATTER">Entire matter</SelectItem>
                  <SelectItem value="INCLUDE_SAVED_SEARCH" disabled={savedSearches.length === 0}>Include only a saved search</SelectItem>
                  <SelectItem value="EXCLUDE_SAVED_SEARCH" disabled={savedSearches.length === 0}>Entire matter excluding a saved search</SelectItem>
                </SelectContent>
              </Select>
            )} />
            {scopeMode !== "ENTIRE_MATTER" ? <Controller control={control} name="saved_search_id" render={({ field }) => (
              <Select value={field.value} onValueChange={field.onChange}>
                <SelectTrigger aria-label="Saved search"><SelectValue placeholder="Select a Keyword saved search" /></SelectTrigger>
                <SelectContent>{savedSearches.map((saved) => <SelectItem key={saved.id} value={saved.id}>{saved.name}</SelectItem>)}</SelectContent>
              </Select>
            )} /> : null}
            {errors.saved_search_id ? <p className="text-sm text-destructive">{errors.saved_search_id.message}</p> : null}
            {scopeMode === "ENTIRE_MATTER" ? (
              <p className="text-xs leading-5 text-muted-foreground">Use every document with usable embeddings.{savedSearches.length === 0 ? " Create a Keyword saved search to use an include or exclude scope." : ""}</p>
            ) : (
              <p className="text-xs leading-5 text-muted-foreground">The selected Keyword search is snapshotted when the job is created. Later edits do not change this run.</p>
            )}
          </div>
          <label className="flex items-start gap-3 rounded-lg border bg-muted/25 p-4 text-sm">
            <input type="checkbox" className="mt-0.5 size-4 rounded border-input accent-primary" {...register("replace_existing")} />
            <span><span className="block font-semibold">Replace existing values in the destination field when approved</span><span className="mt-1 block text-muted-foreground">After review, clear current destination-field values before applying this run. Metadata history remains available.</span></span>
          </label>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Start discovery</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
