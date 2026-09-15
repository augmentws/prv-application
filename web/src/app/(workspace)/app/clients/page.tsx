"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { CreateClientDialog, type CreateClientValues } from "@/components/forms/create-client-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/components/workspace-context";
import type { ClientRead } from "@/generated/models";
import { useWebMcpTool } from "@/hooks/use-webmcp-tool";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

const toolInput = z.object({ name: z.string().trim().min(2).max(200) });

export default function ClientsPage() {
  const { tenant, selectedTenantId } = useWorkspace();
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["clients", selectedTenantId], queryFn: () => coreApi<ClientRead[]>(`/v1/tenants/${selectedTenantId}/clients`) });
  const mutation = useMutation({
    mutationFn: (values: CreateClientValues) => coreApi<ClientRead>(`/v1/tenants/${selectedTenantId}/clients`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (client) => { void queryClient.invalidateQueries({ queryKey: ["clients", selectedTenantId] }); toast.success(`${client.name} was created.`); },
  });
  const createClient = useCallback((values: CreateClientValues) => mutation.mutateAsync(values).then(() => undefined), [mutation]);
  const webMcpTool = useMemo(() => ({
    name: "create_client",
    title: "Create client",
    description: `Create a client in the active tenant, ${tenant.name}.`,
    inputSchema: { type: "object", properties: { name: { type: "string", minLength: 2, maxLength: 200 } }, required: ["name"], additionalProperties: false },
    annotations: { readOnlyHint: false, untrustedContentHint: false },
    async execute(input: unknown) { const values = toolInput.parse(input); const client = await mutation.mutateAsync(values); return { id: client.id, name: client.name, status: client.status }; },
  }), [mutation, tenant.name]);
  useWebMcpTool(webMcpTool);

  const columns = useMemo<ColumnDef<ClientRead>[]>(() => [
    { accessorKey: "name", header: "Client", cell: ({ row }) => <Link className="font-semibold text-primary hover:underline" href={`/app/clients/${row.original.id}`}>{row.original.name}</Link> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "open", header: "", cell: ({ row }) => <Button asChild variant="ghost" size="sm"><Link href={`/app/clients/${row.original.id}`}>Open <ArrowRight /></Link></Button> },
  ], []);

  return (
    <><PageHeader eyebrow={tenant.name} title="Clients" description="Clients organize matters within the active tenant." actions={<CreateClientDialog onCreate={createClient} />} />
      {query.isPending ? <TableLoading /> : query.error ? <QueryError message={query.error.message} /> : <DataTable columns={columns} data={query.data} emptyMessage="No clients yet. Add the first client to begin." />}
    </>
  );
}
