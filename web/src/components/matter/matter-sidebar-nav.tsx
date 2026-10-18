"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronLeft, ChevronsUpDown } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { matterNavHref, matterNavTarget, visibleMatterNav } from "@/components/matter/matter-sections";
import type { ClientRead, EffectivePermissionsRead, MatterRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { matterHref, matterSectionFromPath } from "@/lib/matter-routes";
import { cn } from "@/lib/utils";

/** The matter's own menu, shown in the app sidebar in place of the workspace links while a matter is open. */
export function MatterSidebarNav({ clientId, matterId, collapsed, onNavigate }: {
  clientId: string;
  matterId: string;
  collapsed: boolean;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const router = useRouter();
  // Same keys and requests as the matter pages, so these responses are shared with them.
  const client = useQuery({ queryKey: ["client", clientId], queryFn: () => coreApi<ClientRead>(`/v1/clients/${clientId}`) });
  const matter = useQuery({ queryKey: ["matter", matterId], queryFn: () => coreApi<MatterRead>(`/v1/matters/${matterId}`) });
  const matters = useQuery({ queryKey: ["matters", clientId], queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${clientId}/matters`) });
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });
  const permissionKeys = new Set(permissions.data?.permission_keys ?? []);
  const section = matterSectionFromPath(pathname) ?? "overview";
  const activeTarget = matterNavTarget(section);
  const matterName = matter.data?.name ?? "Matter";

  function switchMatter(nextMatterId: string) {
    onNavigate();
    // Detail pages map to their section, so switching never points at another matter's record.
    router.push(matterHref(clientId, nextMatterId, activeTarget));
  }

  return (
    <nav aria-label="Matter" className="flex-1 overflow-y-auto p-3">
      <Link
        href={`/app/clients/${clientId}`}
        title={collapsed ? client.data?.name ?? "Client" : undefined}
        onClick={onNavigate}
        className={cn("mb-2 flex h-8 items-center gap-1.5 rounded-lg px-2 text-sm text-muted-foreground outline-none hover:bg-card hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring", collapsed && "justify-center px-0")}
      >
        <ChevronLeft className="size-4 shrink-0" />
        {!collapsed ? <span className="truncate">{client.data?.name ?? "Client"}</span> : <span className="sr-only">{client.data?.name ?? "Client"}</span>}
      </Link>

      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            aria-label={`Matter: ${matterName}. Switch matter`}
            title={collapsed ? matterName : undefined}
            className={cn("mb-3 flex w-full items-center gap-2 rounded-lg border border-sidebar-border bg-card px-3 py-2 text-left outline-none hover:border-input focus-visible:ring-2 focus-visible:ring-ring", collapsed && "justify-center px-0")}
          >
            {collapsed ? (
              <span className="grid size-6 place-items-center rounded bg-primary/10 text-xs font-bold text-primary">{matterName.slice(0, 1).toUpperCase()}</span>
            ) : (
              <>
                <span className="min-w-0 flex-1 truncate text-sm font-semibold">{matterName}</span>
                <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
              </>
            )}
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content side="bottom" align="start" className="z-50 mt-1 max-h-80 min-w-56 overflow-y-auto rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
            {matters.error ? <p role="alert" className="px-3 py-2 text-sm text-destructive">Matters could not be loaded: {matters.error.message}</p> : null}
            {matters.isPending ? <p className="px-3 py-2 text-sm text-muted-foreground">Loading matters…</p> : null}
            {matters.data?.map((item) => (
              <DropdownMenu.Item
                key={item.id}
                onSelect={() => { if (item.id !== matterId) switchMatter(item.id); }}
                className="flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted"
              >
                <Check className={cn("size-4", item.id === matterId ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                <span className="truncate">{item.name}</span>
              </DropdownMenu.Item>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <div className="space-y-3">
        {visibleMatterNav(permissionKeys).map((group) => {
          return (
            <div key={group.label ?? "top"} role="group" aria-label={group.label}>
              {group.label && !collapsed ? <p className="mb-1 px-3 text-xs font-medium text-muted-foreground">{group.label}</p> : null}
              <ul className="space-y-0.5">
                {group.items.map(({ label, icon: Icon, target }) => {
                  const active = target === activeTarget;
                  const href = matterNavHref(clientId, matterId, target);
                  return (
                    <li key={target}>
                      <Link
                        href={href}
                        title={collapsed ? label : undefined}
                        aria-current={active ? "page" : undefined}
                        onClick={onNavigate}
                        className={cn(
                          "flex h-9 items-center gap-3 rounded-lg px-3 text-sm font-medium outline-none transition hover:bg-card focus-visible:ring-2 focus-visible:ring-ring",
                          active && "bg-primary text-primary-foreground shadow-sm hover:bg-primary",
                          collapsed && "justify-center px-0",
                        )}
                      >
                        <Icon className="size-[1.05rem] shrink-0" aria-hidden="true" />
                        {!collapsed ? <span className="truncate">{label}</span> : <span className="sr-only">{label}</span>}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
      </div>
      {permissions.error ? <p role="alert" className="mt-3 px-3 text-xs text-destructive">Matter permissions could not be loaded: {permissions.error.message}</p> : null}
    </nav>
  );
}
