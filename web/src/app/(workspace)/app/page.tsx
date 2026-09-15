"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowUpRight, Braces, FolderKanban, Users } from "lucide-react";
import Link from "next/link";

import type { ClientRead, UserRead } from "@/generated/models";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useWorkspace } from "@/components/workspace-context";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";

export default function OverviewPage() {
  const { tenant, selectedTenantId } = useWorkspace();
  const clients = useQuery({ queryKey: ["clients", selectedTenantId], queryFn: () => coreApi<ClientRead[]>(`/v1/tenants/${selectedTenantId}/clients`) });
  const users = useQuery({ queryKey: ["users", selectedTenantId], queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${selectedTenantId}/users`) });

  return (
    <>
      <PageHeader eyebrow="Workspace" title={tenant.name} description="Manage access, clients, matters, and the metadata structure used by review teams and agents." />
      {clients.error || users.error ? <QueryError /> : null}
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <MetricCard icon={FolderKanban} label="Clients" value={clients.data?.length} href="/app/clients" />
        <MetricCard icon={Users} label="Users" value={users.data?.length} href="/app/users" />
        <Card className="overflow-hidden border-primary/20 bg-primary text-primary-foreground">
          <CardHeader><span className="mb-3 grid size-9 place-items-center rounded-lg bg-white/10"><Braces className="size-4" /></span><CardTitle>Metadata lives with matters</CardTitle><CardDescription className="text-white/70">Open a client and matter to define review fields.</CardDescription></CardHeader>
        </Card>
      </div>

      <section className="mt-8">
        <div className="mb-3 flex items-center justify-between"><h2 className="text-base font-semibold">Recent clients</h2><Link href="/app/clients" className="text-sm font-semibold text-primary hover:underline">View all</Link></div>
        {clients.isPending ? <TableLoading /> : clients.data?.length ? (
          <Card className="divide-y">
            {clients.data.slice(0, 5).map((client) => (
              <Link key={client.id} href={`/app/clients/${client.id}`} className="flex items-center gap-4 p-4 outline-none transition hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-secondary font-bold text-secondary-foreground">{client.name.slice(0, 1).toUpperCase()}</span>
                <span className="min-w-0 flex-1"><span className="block truncate font-medium">{client.name}</span><span className="block text-xs text-muted-foreground">Added {formatDate(client.created_at)}</span></span>
                <Badge variant="active">{client.status.toLowerCase()}</Badge><ArrowUpRight className="size-4 text-muted-foreground" />
              </Link>
            ))}
          </Card>
        ) : <Card className="p-6"><p className="text-sm text-muted-foreground">No clients have been created in this tenant.</p></Card>}
      </section>
    </>
  );
}

function MetricCard({ icon: Icon, label, value, href }: { icon: typeof Users; label: string; value?: number; href: string }) {
  return (
    <Link href={href} className="rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-ring">
      <Card className="h-full transition hover:-translate-y-0.5 hover:border-primary/25 hover:shadow-md"><CardContent className="flex items-center gap-4 p-5"><span className="grid size-11 place-items-center rounded-xl bg-secondary text-primary"><Icon className="size-5" /></span><span><span className="block text-2xl font-semibold tabular-nums">{value ?? "—"}</span><span className="text-sm text-muted-foreground">{label}</span></span><ArrowUpRight className="ml-auto size-4 text-muted-foreground" /></CardContent></Card>
    </Link>
  );
}
