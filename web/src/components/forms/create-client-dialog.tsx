"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LoaderCircle, Plus } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const schema = z.object({ name: z.string().trim().min(2, "Enter a client name").max(200) });
export type CreateClientValues = z.infer<typeof schema>;

export function CreateClientDialog({ onCreate }: { onCreate: (values: CreateClientValues) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<CreateClientValues>({ resolver: zodResolver(schema) });

  async function submit(values: CreateClientValues) {
    try {
      await onCreate(values);
      reset();
      setOpen(false);
    } catch (error) {
      setError("root", { message: error instanceof Error ? error.message : "Client could not be created." });
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Plus />Add client</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Add client</DialogTitle><DialogDescription>Create a client within the active tenant.</DialogDescription></DialogHeader>
        <form onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="client-name">Client name</Label><Input id="client-name" autoFocus placeholder="Acme Corporation" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
          {errors.root ? <p role="alert" className="mt-4 text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create client</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
