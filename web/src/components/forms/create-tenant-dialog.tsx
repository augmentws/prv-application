"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Building2, LoaderCircle } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const schema = z.object({
  name: z.string().trim().min(2, "Enter a tenant name").max(200),
  slug: z.string().trim().min(3).max(80).regex(/^[a-z][a-z0-9-]*[a-z0-9]$/, "Use lowercase letters, numbers, and hyphens"),
  admin_name: z.string().trim().min(2, "Enter the administrator's name").max(200),
  admin_email: z.email("Enter a valid email address"),
  admin_password: z.string().min(12, "Use at least 12 characters").max(1024),
});
export type CreateTenantValues = z.infer<typeof schema>;

export function CreateTenantDialog({ onCreate }: { onCreate: (values: CreateTenantValues) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const { register, handleSubmit, reset, setError, formState: { errors, isSubmitting } } = useForm<CreateTenantValues>({ resolver: zodResolver(schema) });
  async function submit(values: CreateTenantValues) {
    try { await onCreate(values); reset(); setOpen(false); }
    catch (error) { setError("root", { message: error instanceof Error ? error.message : "Tenant could not be created." }); }
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Building2 />Add tenant</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader><DialogTitle>Add tenant</DialogTitle><DialogDescription>Create the tenant and its first administrator together.</DialogDescription></DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit(submit)}>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2"><Label htmlFor="tenant-name">Tenant name</Label><Input id="tenant-name" autoFocus placeholder="Acme Legal" {...register("name")} />{errors.name ? <p className="text-sm text-destructive">{errors.name.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="tenant-slug">Tenant slug</Label><Input id="tenant-slug" placeholder="acme-legal" {...register("slug")} />{errors.slug ? <p className="text-sm text-destructive">{errors.slug.message}</p> : null}</div>
          </div>
          <div className="border-t pt-4"><p className="mb-4 text-sm font-semibold">Initial administrator</p><div className="space-y-4">
            <div className="space-y-2"><Label htmlFor="tenant-admin-name">Name</Label><Input id="tenant-admin-name" {...register("admin_name")} />{errors.admin_name ? <p className="text-sm text-destructive">{errors.admin_name.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="tenant-admin-email">Email</Label><Input id="tenant-admin-email" type="email" {...register("admin_email")} />{errors.admin_email ? <p className="text-sm text-destructive">{errors.admin_email.message}</p> : null}</div>
            <div className="space-y-2"><Label htmlFor="tenant-admin-password">Temporary password</Label><Input id="tenant-admin-password" type="password" autoComplete="new-password" {...register("admin_password")} />{errors.admin_password ? <p className="text-sm text-destructive">{errors.admin_password.message}</p> : null}</div>
          </div></div>
          {errors.root ? <p role="alert" className="text-sm text-destructive">{errors.root.message}</p> : null}
          <DialogFooter><Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={isSubmitting}>{isSubmitting ? <LoaderCircle className="animate-spin" /> : null}Create tenant</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
