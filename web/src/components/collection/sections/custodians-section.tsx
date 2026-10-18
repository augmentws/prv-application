"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { useMemo, useState } from "react";

import { useCollection } from "@/components/collection/collection-context";
import { useClientCustodians, useClientMatters, useStartCollectionImport } from "@/components/collection/sections/shared";
import { DataTable } from "@/components/data-table";
import { AddToMatterDialog } from "@/components/forms/add-to-matter-dialog";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import type { CollectionCustodianSummary } from "@/generated/models";
import { coreApi } from "@/lib/api-client";

interface CollectionCustodianRow {
  id: string;
  displayName: string;
  emailAddresses: string[];
  externalReference: string | null;
  status: string | null;
  itemCount: number;
}

/** Custodians with at least one document in the collection. Selected custodians' documents can be added to a matter. */
export function CustodiansSection() {
  const { clientId, collectionId } = useCollection();
  const [selectedCustodianIds, setSelectedCustodianIds] = useState<string[]>([]);
  const matters = useClientMatters(clientId);
  const custodians = useClientCustodians(clientId);
  const collectionCustodianSummary = useQuery({
    queryKey: ["collection-custodians", collectionId],
    queryFn: () => coreApi<CollectionCustodianSummary[]>(`/v1/collections/${collectionId}/custodians`),
  });
  const createImportJob = useStartCollectionImport(clientId);

  const collectionCustodians = useMemo<CollectionCustodianRow[]>(() => {
    const detailsById = new Map((custodians.data ?? []).map((custodian) => [custodian.id, custodian]));
    return (collectionCustodianSummary.data ?? [])
      .map((summary) => {
        const details = detailsById.get(summary.custodian_id);
        return {
          id: summary.custodian_id,
          displayName: details?.display_name ?? "Unknown custodian",
          emailAddresses: details?.email_addresses ?? [],
          externalReference: details?.external_reference ?? null,
          status: details?.status ?? null,
          itemCount: summary.item_count,
        };
      })
      .sort((left, right) => left.displayName.localeCompare(right.displayName));
  }, [collectionCustodianSummary.data, custodians.data]);
  const custodianColumns = useMemo<ColumnDef<CollectionCustodianRow>[]>(() => [
    {
      id: "select",
      size: 52,
      minSize: 52,
      maxSize: 52,
      enableResizing: false,
      header: () => (
        <input
          type="checkbox"
          aria-label="Select every custodian"
          checked={collectionCustodians.length > 0 && selectedCustodianIds.length === collectionCustodians.length}
          onChange={(event) => setSelectedCustodianIds(event.target.checked ? collectionCustodians.map((row) => row.id) : [])}
          className="size-4 rounded border-input accent-primary"
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          aria-label={`Select ${row.original.displayName}`}
          checked={selectedCustodianIds.includes(row.original.id)}
          onChange={(event) => setSelectedCustodianIds((current) => event.target.checked ? [...current, row.original.id] : current.filter((id) => id !== row.original.id))}
          className="size-4 rounded border-input accent-primary"
        />
      ),
    },
    {
      accessorKey: "displayName",
      header: "Custodian",
      size: 210,
      cell: ({ row }) => <span className="font-semibold">{row.original.displayName}</span>,
    },
    {
      id: "emailAddresses",
      header: "Email",
      size: 280,
      cell: ({ row }) => {
        const value = row.original.emailAddresses.join(", ");
        return <span className="block max-w-md truncate text-muted-foreground" title={value || undefined}>{value || "—"}</span>;
      },
    },
    {
      accessorKey: "externalReference",
      header: "Source reference",
      size: 180,
      cell: ({ row }) => <span className="text-muted-foreground">{row.original.externalReference || "—"}</span>,
    },
    {
      accessorKey: "itemCount",
      header: "Documents",
      size: 110,
      cell: ({ row }) => <span className="font-mono text-sm tabular-nums">{row.original.itemCount.toLocaleString()}</span>,
    },
    {
      accessorKey: "status",
      header: "Status",
      size: 130,
      cell: ({ row }) => row.original.status ? <StatusBadge status={row.original.status} /> : <span className="text-muted-foreground">—</span>,
    },
  ], [collectionCustodians, selectedCustodianIds]);

  if (custodians.isPending || collectionCustodianSummary.isPending) return <TableLoading />;
  if (custodians.error || collectionCustodianSummary.error) return <QueryError message={custodians.error?.message ?? collectionCustodianSummary.error?.message} />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {selectedCustodianIds.length ? `${selectedCustodianIds.length.toLocaleString()} selected` : "Select custodians to add all of their documents."}
        </p>
        <AddToMatterDialog
          matters={matters.data ?? []}
          triggerLabel="Add documents to matter"
          disabled={!selectedCustodianIds.length || matters.isPending}
          selectionDescription={`Add documents associated with ${selectedCustodianIds.length.toLocaleString()} selected ${selectedCustodianIds.length === 1 ? "custodian" : "custodians"}.`}
          onAdd={(matterId) => createImportJob(matterId, {
            source_collection_id: collectionId,
            selection: {
              mode: "QUERY",
              q: null,
              custodian_ids: selectedCustodianIds,
              file_extensions: [],
              record_types: [],
              processing_statuses: [],
              item_ids: [],
            },
            selection_summary: `${selectedCustodianIds.length} selected ${selectedCustodianIds.length === 1 ? "custodian" : "custodians"}`,
          })}
        />
      </div>
      <DataTable
        columns={custodianColumns}
        data={collectionCustodians}
        emptyMessage="No custodians are represented in this collection yet."
      />
    </div>
  );
}
