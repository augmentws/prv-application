"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { LoaderCircle, UserPlus } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const schema = z.object({
  display_name: z.string().trim().min(2, "Enter the user's name").max(200),
  email: z.email("Enter a valid email address"),
  password: z.string().min(12, "Use at least 12 characters").max(1024),
});
export type CreateUserValues = z.infer<typeof schema>;

export function CreateUserDialog({ onCreate }: { onCreate: (values: CreateUserValues) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<CreateUserValues>({ resolver: zodResolver(schema) });
  async function submit(values: CreateUserValues) {
    try { await onCreate(values); reset(); setOpen(false); }
    catch (error) { setError("root", { message: error instanceof Error ? error.message : "User could not be created." }); }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><UserPlus />Add user</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Add user</DialogTitle><DialogDescription>Create an administrator in the active tenant.</DialogDescription></DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit(submit)}>
          <div className="space-y-2"><Label htmlFor="user-name">Name</Label><Input id="user-name" autoFocus {...register("display_name")} />{errors.display_name ? <p className="text-sm text-destructive">{errors.display_name.message}</p> : null}</div>
          <div className="space-y-2"><Label htmlFor="user-email">Email</Label><Input id="user-email" type="email" {...register("email")} />{errors.email ? <p className="text-sm text-destructive">{errors.email.message}</p> : null}</div>
          <div className="space-y-2"><Label htmlFor="user-password">Temporary password</Label><Input id="user-password" type="password" autoComplete="new-password" {...register("password")} /><p className="text-xs leading-5 text-muted-foreground">At least 12 characters. Share it through a secure channel.</p>{errors.password ? <p className="text-sm text-destructive">{errors.password.message}</p> : null}</div>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create user</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
