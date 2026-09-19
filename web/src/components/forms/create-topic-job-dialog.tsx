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
import type { MatterTopicJobCreate } from "@/generated/models";

const schema = z.object({
  operating_mode: z.enum(["AUTO", "FIXED"]),
  sample_size: z.number().int().min(10).max(200_000),
  requested_topic_count: z.number().int().min(2).max(200).optional(),
  replace_existing: z.boolean(),
}).superRefine((values, context) => {
  if (values.operating_mode === "FIXED" && values.requested_topic_count === undefined) {
    context.addIssue({ code: "custom", path: ["requested_topic_count"], message: "Enter the number of topics" });
  }
  if (values.requested_topic_count !== undefined && values.requested_topic_count > values.sample_size) {
    context.addIssue({ code: "custom", path: ["requested_topic_count"], message: "Topic count cannot exceed sample size" });
  }
});

type FormValues = z.infer<typeof schema>;

export function CreateTopicJobDialog({ onCreate, disabled }: {
  onCreate: (values: MatterTopicJobCreate) => Promise<void>;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, watch, formState: { errors, isSubmitting } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { operating_mode: "AUTO", sample_size: 10_000, replace_existing: true },
  });
  // React Hook Form owns field subscriptions; React Compiler safely skips this form component.
  // eslint-disable-next-line react-hooks/incompatible-library
  const mode = watch("operating_mode");

  async function submit(values: FormValues) {
    try {
      await onCreate({
        operating_mode: values.operating_mode,
        sample_size: values.sample_size,
        requested_topic_count: values.operating_mode === "FIXED" ? values.requested_topic_count : null,
        assignment_mode: values.replace_existing ? "REPLACE" : "APPEND",
      });
      reset({ operating_mode: "AUTO", sample_size: 10_000, replace_existing: true });
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Topic clustering could not be started." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button disabled={disabled}><Network />Cluster topics</Button></DialogTrigger>
      <DialogContent className="max-w-lg">
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
          <label className="flex items-start gap-3 rounded-lg border bg-muted/25 p-4 text-sm">
            <input type="checkbox" className="mt-0.5 size-4 rounded border-input accent-primary" {...register("replace_existing")} />
            <span><span className="block font-semibold">Replace existing topic values when approved</span><span className="mt-1 block text-muted-foreground">After review, clear current values before applying this run. Metadata history remains available.</span></span>
          </label>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Start discovery</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
