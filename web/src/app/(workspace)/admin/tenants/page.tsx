"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateTenantDialog, type CreateTenantValues } from "@/components/forms/create-tenant-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { useWorkspace } from "@/components/workspace-context";
import type { TenantCreated, TenantRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export default function TenantsPage() {
  const { user, selectedTenantId } = useWorkspace();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["tenants"], queryFn: () => coreApi<TenantRead[]>("/v1/tenants"), enabled: user.is_superuser });
  const mutation = useMutation({
    mutationFn: (values: CreateTenantValues) => coreApi<TenantCreated>("/v1/tenants", { method: "POST", body: JSON.stringify({ parent_tenant_id: selectedTenantId, name: values.name, slug: values.slug, initial_admin: { display_name: values.admin_name, email: values.admin_email, password: values.admin_password, role: "ADMIN" } }) }),
    onSuccess: ({ tenant }) => { void queryClient.invalidateQueries({ queryKey: ["tenants"] }); toast.success(`${tenant.name} was created.`); },
  });
  const createTenant = useCallback((values: CreateTenantValues) => mutation.mutateAsync(values).then(() => undefined), [mutation]);
  const columns = useMemo<ColumnDef<TenantRead>[]>(() => [
    { accessorKey: "name", header: "Tenant", cell: ({ row }) => <div><p className="font-semibold">{row.original.name}</p><p className="text-xs text-muted-foreground">{row.original.slug}</p></div> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "is_root", header: "Level", cell: ({ row }) => <Badge variant={row.original.is_root ? "accent" : "outline"}>{row.original.is_root ? "root" : "subtenant"}</Badge> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
  ], []);
  if (!user.is_superuser) return <QueryError message="Root administrator access is required." />;
  return (
    <><PageHeader eyebrow="Administration" title="Tenants" description="Create and oversee tenant workspaces. New tenants receive an initial administrator." actions={<CreateTenantDialog onCreate={createTenant} />} />
      {query.isPending ? <TableLoading /> : query.error ? <QueryError message={query.error.message} /> : <DataTable columns={columns} data={query.data ?? []} emptyMessage="No tenants have been created." />}
    </>
  );
}
