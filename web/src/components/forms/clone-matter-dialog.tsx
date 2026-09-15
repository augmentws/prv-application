"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Copy, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const schema = z.object({ name: z.string().trim().min(2, "Enter a matter name").max(200) });
export type CloneMatterValues = z.infer<typeof schema>;

export function CloneMatterDialog({ sourceName, onClone }: { sourceName: string; onClone: (values: CloneMatterValues) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<CloneMatterValues>({ resolver: zodResolver(schema) });
  async function submit(values: CloneMatterValues) {
    try { await onClone(values); reset(); setOpen(false); }
    catch (error) { setError("root", { message: error instanceof Error ? error.message : "Matter could not be cloned." }); }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant="outline"><Copy />Clone configuration</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Clone matter configuration</DialogTitle><DialogDescription>Create a new matter from {sourceName}&apos;s shared fields and groups. Evidence, members, values, personal groups, and preferences are not copied.</DialogDescription></DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="clone-name">New matter name</Label><Input id="clone-name" autoFocus placeholder={`${sourceName} Copy`} {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create cloned matter</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
