"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowRight, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateCollectionDialog, type CreateCollectionValues } from "@/components/forms/create-collection-dialog";
import { CreateMatterDialog, type CreateMatterValues } from "@/components/forms/create-matter-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { ClientRead, CollectionRead, MatterRead, MatterTemplateRead, TenantRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export function ClientView({ clientId }: { clientId: string }) {
  const queryClient = useQueryClient();
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matters = useQuery({ queryKey: ["matters", clientId], queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`) });
  const templates = useQuery({ queryKey: ["matter-templates", clientId], queryFn: () => coreApi<MatterTemplateRead[]>(`/v1/clients/${clientId}/matter-templates`) });
  const collections = useQuery({
    queryKey: ["collections", client.data?.tenant_id, clientId],
    queryFn: () => coreApi<CollectionRead[]>(`/v1/tenants/${client.data!.tenant_id}/clients/${clientId}/collections`),
    enabled: Boolean(client.data),
  });
  const matterMutation = useMutation({
    mutationFn: (values: CreateMatterValues) => coreApi<MatterRead>(`/v1/clients/${clientId}/matters`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (matter) => { void queryClient.invalidateQueries({ queryKey: ["matters", clientId] }); toast.success(`${matter.name} was created.`); },
  });
  const collectionMutation = useMutation({
    mutationFn: async (values: CreateCollectionValues) => {
      if (!client.data) throw new Error("Client is still loading.");
      const tenant = await coreApi<TenantRead>(`/v1/tenants/${client.data.tenant_id}`);
      await coreApi(`/v1/tenants/${tenant.id}/artifact-storage/ensure`, {
        method: "POST",
        body: JSON.stringify({ tenant_slug: tenant.slug }),
      });
      return coreApi<CollectionRead>(`/v1/tenants/${tenant.id}/clients/${clientId}/collections`, {
        method: "POST",
        body: JSON.stringify({ name: values.name, description: values.description || null }),
      });
    },
    onSuccess: (collection) => {
      void queryClient.invalidateQueries({ queryKey: ["collections"] });
      toast.success(`${collection.name} was created.`);
    },
  });
  const createMatter = useCallback((values: CreateMatterValues) => matterMutation.mutateAsync(values).then(() => undefined), [matterMutation]);
  const createCollection = useCallback((values: CreateCollectionValues) => collectionMutation.mutateAsync(values).then(() => undefined), [collectionMutation]);
  const matterColumns = useMemo<ColumnDef<MatterRead>[]>(() => [
    { accessorKey: "name", header: "Matter", cell: ({ row }) => <Link className="font-semibold text-primary hover:underline" href={`/app/clients/${clientId}/matters/${row.original.id}`}>{row.original.name}</Link> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "open", header: "", cell: ({ row }) => <Button asChild variant="ghost" size="sm"><Link href={`/app/clients/${clientId}/matters/${row.original.id}`}>Open <ArrowRight /></Link></Button> },
  ], [clientId]);
  const collectionColumns = useMemo<ColumnDef<CollectionRead>[]>(() => [
    {
      accessorKey: "name",
      header: "Collection",
      cell: ({ row }) => (
        <div>
          <Link className="font-semibold text-primary hover:underline" href={`/app/clients/${clientId}/collections/${row.original.id}`}>{row.original.name}</Link>
          {row.original.description ? <p className="mt-0.5 max-w-xl truncate text-xs text-muted-foreground">{row.original.description}</p> : null}
        </div>
      ),
    },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "open", header: "", cell: ({ row }) => <Button asChild variant="ghost" size="sm"><Link href={`/app/clients/${clientId}/collections/${row.original.id}`}>Open <ArrowRight /></Link></Button> },
  ], [clientId]);

  if (client.isPending) return <TableLoading />;
  if (client.error) return <QueryError message={client.error.message} />;

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex items-center gap-1 text-sm text-muted-foreground"><Link href="/app/clients" className="hover:text-foreground">Clients</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{client.data.name}</span></nav>
      <PageHeader eyebrow="Client" title={client.data.name} description="Manage this client's evidence collections and review matters." actions={<CreateCollectionDialog onCreate={createCollection} />} />
      <section>
        <h2 className="mb-3 text-base font-semibold">Collections</h2>
        {collections.isPending ? <TableLoading /> : collections.error ? <QueryError message={collections.error.message} /> : <DataTable columns={collectionColumns} data={collections.data} emptyMessage="No collections yet. Create one to receive imported evidence." />}
      </section>
      <section className="mt-8">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold">Matters</h2>
          <CreateMatterDialog templates={templates.data ?? []} onCreate={createMatter} />
        </div>
        {matters.isPending ? <TableLoading /> : matters.error ? <QueryError message={matters.error.message} /> : <DataTable columns={matterColumns} data={matters.data} emptyMessage="No matters yet. Add this client's first matter." />}
      </section>
    </>
  );
}
