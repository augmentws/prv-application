"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Braces, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import type { MetadataDefinitionCreate } from "@/generated/models";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const types = ["TEXT", "LONG_TEXT", "INTEGER", "DECIMAL", "BOOLEAN", "DATE", "DATETIME", "ENUM", "JSON"] as const;
const schema = z.object({
  display_name: z.string().trim().min(2, "Enter a display name").max(200),
  key: z.string().trim().min(1).max(100).regex(/^[a-z][a-z0-9_]*$/, "Use lowercase letters, numbers, and underscores"),
  description: z.string().max(4000).optional(),
  type: z.enum(types),
  cardinality: z.enum(["SINGLE", "MULTIPLE"]),
  enum_options: z.string().optional(),
  searchable: z.boolean(),
  facetable: z.boolean(),
  normalize_to_lowercase: z.boolean(),
  reviewable: z.boolean(),
  ai_assignable: z.boolean(),
}).superRefine((values, context) => {
  if (values.type === "ENUM" && !values.enum_options?.trim()) {
    context.addIssue({ code: "custom", path: ["enum_options"], message: "Add at least one enum option" });
  }
  if (values.facetable && ["LONG_TEXT", "JSON"].includes(values.type)) {
    context.addIssue({ code: "custom", path: ["facetable"], message: "Long text and JSON fields cannot be faceted" });
  }
});

type FormValues = z.infer<typeof schema>;

function enumValues(source?: string) {
  if (!source) return null;
  return source.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const [rawKey, ...labelParts] = line.split("|");
    const key = rawKey.trim();
    const label = (labelParts.join("|").trim() || key.replaceAll("_", " ")).replace(/^./, (value) => value.toUpperCase());
    return { key, label, description: null, active: true };
  });
}

export function CreateMetadataDialog({ onCreate }: { onCreate: (values: MetadataDefinitionCreate) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, watch, formState: { errors, isSubmitting } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { type: "TEXT", cardinality: "SINGLE", searchable: true, facetable: false, normalize_to_lowercase: false, reviewable: true, ai_assignable: false },
  });
  // React Hook Form owns field subscriptions; React Compiler safely skips this form component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const selectedType = watch("type");

  async function submit(values: FormValues) {
    try {
      await onCreate({
        key: values.key,
        display_name: values.display_name,
        description: values.description || null,
        type: values.type,
        cardinality: values.cardinality,
        allowed_values: values.type === "ENUM" ? enumValues(values.enum_options) : null,
        assertion_policy: "IMMEDIATE",
        resolution_policy: "EXPLICIT_ONLY",
        searchable: values.searchable,
        facetable: values.facetable,
        normalize_to_lowercase: ["TEXT", "LONG_TEXT"].includes(values.type) && values.normalize_to_lowercase,
        reviewable: values.reviewable,
        ai_assignable: values.ai_assignable,
      });
      reset();
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Field definition could not be created." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Braces />Add field</Button></DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader><DialogTitle>Add metadata field</DialogTitle><DialogDescription>Define a field available within this matter.</DialogDescription></DialogHeader>
        <form className="space-y-5" onSubmit={handleSubmit(submit)}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="field-name">Display name</Label><Input id="field-name" autoFocus placeholder="Responsiveness" {...register("display_name")} />{errors.display_name ? <p className="text-sm text-destructive">{errors.display_name.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="field-key">Field key</Label><Input id="field-key" placeholder="responsiveness" {...register("key")} />{errors.key ? <p className="text-sm text-destructive">{errors.key.message}</p> : null}</div>
          </div>
          <div className="space-y-2"><Label htmlFor="field-description">Description</Label><Textarea id="field-description" placeholder="Explain how this field should be used." {...register("description")} /></div>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label>Value type</Label><Controller control={control} name="type" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{types.map((type) => <SelectItem key={type} value={type}>{type.replace("_", " ").toLowerCase().replace(/^./, (value) => value.toUpperCase())}</SelectItem>)}</SelectContent></Select>} /></div>
            <div className="space-y-2"><Label>Cardinality</Label><Controller control={control} name="cardinality" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="SINGLE">Single value</SelectItem><SelectItem value="MULTIPLE">Multiple values</SelectItem></SelectContent></Select>} /></div>
          </div>
          {selectedType === "ENUM" ? <div className="space-y-2"><Label htmlFor="enum-options">Enum options</Label><Textarea id="enum-options" placeholder={"responsive | Responsive\nnot_responsive | Not responsive"} {...register("enum_options")} /><p className="text-xs leading-5 text-muted-foreground">One option per line: stable_key | Display label</p>{errors.enum_options ? <p className="text-sm text-destructive">{errors.enum_options.message}</p> : null}</div> : null}
          <fieldset className="grid gap-3 rounded-lg border bg-muted/25 p-4 sm:grid-cols-2"><legend className="px-1 text-sm font-semibold">Behavior</legend>
            {[
              { name: "searchable", label: "Searchable" },
              { name: "facetable", label: "Available as a filter" },
              { name: "normalize_to_lowercase", label: "Normalize values to lowercase", disabled: !["TEXT", "LONG_TEXT"].includes(selectedType) },
              { name: "reviewable", label: "Visible to reviewers" },
              { name: "ai_assignable", label: "Agents may assign values" },
            ].map((option) => <label key={option.name} className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={option.disabled} className="size-4 rounded border-input accent-primary disabled:opacity-50" {...register(option.name as "searchable" | "facetable" | "normalize_to_lowercase" | "reviewable" | "ai_assignable")} />{option.label}</label>)}
            {errors.facetable ? <p className="col-span-full text-sm text-destructive">{errors.facetable.message}</p> : null}
          </fieldset>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create field</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
