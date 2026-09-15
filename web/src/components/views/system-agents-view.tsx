"use client";

import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Bot, Plus } from "lucide-react";
import Link from "next/link";
import { useMemo } from "react";

import { DataTable } from "@/components/data-table";
import { HelpLink } from "@/components/help-link";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useWorkspace } from "@/components/workspace-context";
import type { AgentDefinitionRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export function SystemAgentsView() {
  const { user } = useWorkspace();
  const query = useQuery({
    queryKey: ["system-agents"],
    queryFn: () => coreApi<AgentDefinitionRead[]>("/v1/admin/agents"),
    enabled: user.is_superuser,
  });
  const columns = useMemo<ColumnDef<AgentDefinitionRead>[]>(() => [
    {
      accessorKey: "name",
      header: "Agent",
      cell: ({ row }) => (
        <div>
          <Link href={`/admin/agents/${row.original.id}`} className="font-semibold text-primary hover:underline">{row.original.name}</Link>
          <p className="mt-0.5 font-mono text-xs text-muted-foreground">{row.original.key}</p>
        </div>
      ),
    },
    { accessorKey: "status", header: "Status", cell: ({ row }) => <StatusBadge status={row.original.status} /> },
    {
      id: "version",
      header: "Version",
      cell: ({ row }) => (
        <div className="flex flex-wrap items-center gap-2">
          <span>v{row.original.current_version}</span>
          {row.original.published_version ? <Badge variant="active">v{row.original.published_version} published</Badge> : <Badge variant="outline">not published</Badge>}
        </div>
      ),
    },
    { accessorKey: "updated_at", header: "Updated", cell: ({ row }) => <span className="text-muted-foreground">{formatDate(row.original.updated_at)}</span> },
  ], []);

  if (!user.is_superuser) return <QueryError message="Root administrator access is required." />;
  return (
    <>
      <PageHeader
        eyebrow="Root administration"
        title="Agents"
        description="Create and publish reusable system agents for compatible platform workflows. Prompts and tool assignments are immutable once versioned."
        actions={(
          <>
            <HelpLink topic="agents" />
            <Button asChild><Link href="/admin/agents/new"><Plus />Create agent</Link></Button>
          </>
        )}
      />
      {query.isPending ? <TableLoading /> : query.error ? <QueryError message={query.error.message} /> : query.data?.length ? (
        <DataTable columns={columns} data={query.data} emptyMessage="No system agents have been created." />
      ) : (
        <div className="grid min-h-64 place-items-center rounded-xl border bg-card p-8 text-center">
          <div><span className="mx-auto grid size-12 place-items-center rounded-xl bg-primary/10 text-primary"><Bot /></span><h2 className="mt-4 font-semibold">Create the first system agent</h2><p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">Define a prompt, select code-owned tools, and publish a version before it becomes available to matter workflows.</p></div>
        </div>
      )}
    </>
  );
}
