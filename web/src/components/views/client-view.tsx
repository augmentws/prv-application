"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { ArrowRight, ChevronRight } from "lucide-react";
import Link from "next/link";
import { useCallback, useMemo, type ReactNode } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { CreateCollectionDialog, type CreateCollectionValues } from "@/components/forms/create-collection-dialog";
import { DeleteCollectionDialog } from "@/components/forms/delete-collection-dialog";
import { CreateMatterDialog, type CreateMatterValues } from "@/components/forms/create-matter-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import type { ClientRead, CollectionDeletionJobRead, CollectionRead, EffectivePermissionsRead, MatterRead, MatterTemplateRead, TenantRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { collectionHref } from "@/lib/collection-routes";
import { formatDate } from "@/lib/format";

export function ClientView({ clientId }: { clientId: string }) {
  const queryClient = useQueryClient();
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "CLIENT", clientId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=CLIENT&resource_id=${clientId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const canViewCollections = permissionKeys.has("collection.view");
  const canCreateCollection = permissionKeys.has("collection.create");
  const canConfigureClient = permissionKeys.has("client.configure");
  const matters = useQuery({ queryKey: ["matters", clientId], queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`) });
  const templates = useQuery({ queryKey: ["matter-templates", clientId], queryFn: () => coreApi<MatterTemplateRead[]>(`/v1/clients/${clientId}/matter-templates`), enabled: canConfigureClient });
  const collections = useQuery({
    queryKey: ["collections", client.data?.tenant_id, clientId],
    queryFn: () => coreApi<CollectionRead[]>(`/v1/tenants/${client.data!.tenant_id}/clients/${clientId}/collections`),
    enabled: Boolean(client.data) && canViewCollections,
    refetchInterval: (query) => query.state.data?.some((collection) => collection.status === "DELETING") ? 2_000 : false,
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
  // Deleting a collection needs client administration (client.configure), the same rule the API applies.
  const { mutateAsync: queueCollectionDeletion } = useMutation({
    mutationFn: (collectionId: string) => coreApi<CollectionDeletionJobRead>(`/v1/collections/${collectionId}`, { method: "DELETE" }),
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: ["collections"] });
      toast.success(`Deletion of ${job.collection_name} was queued.`);
    },
  });
  const deleteCollection = useCallback((collectionId: string) => queueCollectionDeletion(collectionId).then(() => undefined), [queueCollectionDeletion]);
  const createMatter = useCallback((values: CreateMatterValues) => matterMutation.mutateAsync(values).then(() => undefined), [matterMutation]);
  const createCollection = useCallback((values: CreateCollectionValues) => collectionMutation.mutateAsync(values).then(() => undefined), [collectionMutation]);
  const matterColumns = useMemo<ColumnDef<MatterRead>[]>(() => [
    { accessorKey: "name", header: "Matter", cell: ({ row }) => <MatterDestinationLink className="font-semibold text-primary hover:underline" clientId={clientId} matter={row.original}>{row.original.name}</MatterDestinationLink> },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "open", header: "", cell: ({ row }) => <Button asChild variant="ghost" size="sm"><MatterDestinationLink clientId={clientId} matter={row.original}>Open <ArrowRight /></MatterDestinationLink></Button> },
  ], [clientId]);
  const collectionColumns = useMemo<ColumnDef<CollectionRead>[]>(() => [
    {
      accessorKey: "name",
      header: "Collection",
      cell: ({ row }) => (
        <div>
          <Link className="font-semibold text-primary hover:underline" href={collectionHref(clientId, row.original.id)}>{row.original.name}</Link>
          {row.original.description ? <p className="mt-0.5 max-w-xl truncate text-xs text-muted-foreground">{row.original.description}</p> : null}
        </div>
      ),
    },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    { accessorKey: "created_at", header: "Created", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.created_at)}</span> },
    { id: "open", header: "", cell: ({ row }) => <Button asChild variant="ghost" size="sm"><Link href={collectionHref(clientId, row.original.id)}>Open <ArrowRight /></Link></Button> },
    ...(canConfigureClient ? [{
      id: "delete",
      header: () => <span className="sr-only">Delete</span>,
      size: 52,
      enableResizing: false,
      // A collection already being deleted shows its Deleting status instead.
      cell: ({ row }) => row.original.status === "DELETING" ? null : <DeleteCollectionDialog collectionName={row.original.name} onDelete={() => deleteCollection(row.original.id)} />,
    } satisfies ColumnDef<CollectionRead>] : []),
  ], [canConfigureClient, clientId, deleteCollection]);

  if (client.isPending) return <TableLoading />;
  if (client.error) return <QueryError message={client.error.message} />;

  return (
    <>
      <nav aria-label="Breadcrumb" className="mb-4 flex items-center gap-1 text-sm text-muted-foreground"><Link href="/app/clients" className="hover:text-foreground">Clients</Link><ChevronRight className="size-4" /><span aria-current="page" className="text-foreground">{client.data.name}</span></nav>
      <PageHeader eyebrow="Client" title={client.data.name} description="Open the review matters and evidence collections available to you." actions={canCreateCollection ? <CreateCollectionDialog onCreate={createCollection} /> : undefined} />
      {canViewCollections ? <section>
        <h2 className="mb-3 text-base font-semibold">Collections</h2>
        {collections.isPending ? <TableLoading /> : collections.error ? <QueryError message={collections.error.message} /> : <DataTable columns={collectionColumns} data={collections.data} emptyMessage="No collections yet. Create one to receive imported evidence." />}
      </section> : null}
      <section className="mt-8">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-base font-semibold">Matters</h2>
          {canConfigureClient ? <CreateMatterDialog templates={templates.data ?? []} onCreate={createMatter} /> : null}
        </div>
        {matters.isPending ? <TableLoading /> : matters.error ? <QueryError message={matters.error.message} /> : <DataTable columns={matterColumns} data={matters.data} emptyMessage="No matters yet. Add this client's first matter." />}
      </section>
    </>
  );
}

function MatterDestinationLink({ clientId, matter, className, children }: {
  clientId: string;
  matter: MatterRead;
  className?: string;
  children: ReactNode;
}) {
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matter.id],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matter.id}`),
  });
  const canConfigure = permissions.data?.permission_keys.includes("matter.configure") ?? false;
  const href = canConfigure
    ? `/app/clients/${clientId}/matters/${matter.id}`
    : `/review/matters/${matter.id}`;
  return <Link className={className} href={href}>{children}</Link>;
}
