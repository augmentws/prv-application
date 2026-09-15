"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LoaderCircle, Plus } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { MatterTemplateRead } from "@/generated/models";

const schema = z.object({
  name: z.string().trim().min(2, "Enter a matter name").max(200),
  template_id: z.string().optional(),
});
export type CreateMatterValues = z.infer<typeof schema>;

export function CreateMatterDialog({
  templates,
  onCreate,
}: {
  templates: MatterTemplateRead[];
  onCreate: (values: CreateMatterValues) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<CreateMatterValues>({
    resolver: zodResolver(schema),
    defaultValues: { template_id: "default" },
  });
  async function submit(values: CreateMatterValues) {
    try {
      await onCreate({ name: values.name, template_id: values.template_id === "default" ? undefined : values.template_id });
      reset({ name: "", template_id: "default" });
      setOpen(false);
    }
    catch (error) { setError("root", { message: error instanceof Error ? error.message : "Matter could not be created." }); }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Plus />Add matter</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Add matter</DialogTitle><DialogDescription>Create a matter with the standard profile or a reusable configuration template.</DialogDescription></DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="matter-name">Matter name</Label><Input id="matter-name" autoFocus placeholder="Regulatory Inquiry" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
          <div className="space-y-2">
            <Label>Configuration</Label>
            <Controller
              control={control}
              name="template_id"
              render={({ field }) => (
                <Select value={field.value} onValueChange={field.onChange}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="default">Standard EDRM profile</SelectItem>
                    {templates.map((template) => (
                      <SelectItem key={template.id} value={template.id}>{template.name} · {template.scope.toLowerCase()}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
            <p className="text-xs leading-5 text-muted-foreground">Templates copy shared fields, groups, ordering, and default visibility into the new matter.</p>
          </div>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create matter</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
