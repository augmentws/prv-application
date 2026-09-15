"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateUserDialog, type CreateUserValues } from "@/components/forms/create-user-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { useWorkspace } from "@/components/workspace-context";
import type { UserRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export default function UsersPage() {
  const { tenant, selectedTenantId } = useWorkspace();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["users", selectedTenantId], queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${selectedTenantId}/users`) });
  const mutation = useMutation({
    mutationFn: (values: CreateUserValues) => coreApi<UserRead>(`/v1/tenants/${selectedTenantId}/users`, { method: "POST", body: JSON.stringify({ ...values, role: "ADMIN" }) }),
    onSuccess: (user) => { void queryClient.invalidateQueries({ queryKey: ["users", selectedTenantId] }); toast.success(`${user.display_name} was added.`); },
  });
  const createUser = useCallback((values: CreateUserValues) => mutation.mutateAsync(values).then(() => undefined), [mutation]);
  const columns = useMemo<ColumnDef<UserRead>[]>(() => [
    { accessorKey: "display_name", header: "User", cell: ({ row }) => <div><p className="font-semibold">{row.original.display_name}</p><p className="text-xs text-muted-foreground">{row.original.email}</p></div> },
    { accessorKey: "tenant_role", header: "Role", cell: ({ row }) => <Badge>{row.original.tenant_role.toLowerCase()}</Badge> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Added", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
  ], []);
  return (
    <><PageHeader eyebrow={tenant.name} title="Users" description="Manage the administrators who can work within this tenant." actions={<CreateUserDialog onCreate={createUser} />} />
      {query.isPending ? <TableLoading /> : query.error ? <QueryError message={query.error.message} /> : <DataTable columns={columns} data={query.data} emptyMessage="No users are assigned to this tenant." />}
    </>
  );
}
