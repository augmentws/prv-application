"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LayoutTemplate, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

const schema = z.object({
  name: z.string().trim().min(2, "Enter a template name").max(200),
  description: z.string().trim().max(4000).optional(),
  scope: z.enum(["TENANT", "CLIENT"]),
});
export type SaveMatterTemplateValues = z.infer<typeof schema>;

export function SaveMatterTemplateDialog({ onCreate }: { onCreate: (values: SaveMatterTemplateValues) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, control, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<SaveMatterTemplateValues>({
    resolver: zodResolver(schema),
    defaultValues: { scope: "TENANT" },
  });
  async function submit(values: SaveMatterTemplateValues) {
    try { await onCreate(values); reset(); setOpen(false); }
    catch (error) { setError("root", { message: error instanceof Error ? error.message : "Template could not be created." }); }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant="outline"><LayoutTemplate />Save as template</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Save matter configuration</DialogTitle><DialogDescription>Capture shared fields, matter groups, ordering, and default visibility. Personal groups and individual preferences are excluded.</DialogDescription></DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="template-name">Template name</Label><Input id="template-name" autoFocus placeholder="Standard investigation" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
          <div className="space-y-2"><Label htmlFor="template-description">Description</Label><Textarea id="template-description" placeholder="When should this configuration be used?" {...register("description")} /></div>
          <div className="space-y-2"><Label>Availability</Label><Controller control={control} name="scope" render={({ field }) => <Select value={field.value} onValueChange={field.onChange}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="TENANT">All clients in this tenant</SelectItem><SelectItem value="CLIENT">Only this client</SelectItem></SelectContent></Select>} /></div>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Save template</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
