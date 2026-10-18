"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { useQuery } from "@tanstack/react-query";
import { Check, ChevronsUpDown } from "lucide-react";
import Link from "next/link";

import { type MatterNavItem, matterNavHref, visibleMatterNav } from "@/components/matter/matter-sections";
import type { EffectivePermissionsRead } from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { cn } from "@/lib/utils";

/**
 * The matter menu for full-screen workspaces that sit outside the app sidebar (Search and Review,
 * batch review). It takes no width from the panes: the trigger shows the matter and opens its sections.
 */
export function MatterSectionMenu({ clientId, matterId, matterName, clientName, current }: {
  clientId: string;
  matterId: string;
  matterName: string;
  clientName?: string;
  current: MatterNavItem["target"];
}) {
  // Same key and request as the matter pages and the sidebar, so the response is shared.
  const permissions = useQuery({
    queryKey: ["effective-permissions", "MATTER", matterId],
    queryFn: () => coreApi<EffectivePermissionsRead>(`/v1/authorization/effective?resource_type=MATTER&resource_id=${matterId}`),
  });
  const groups = visibleMatterNav(new Set(permissions.data?.permission_keys ?? []));

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label={`Matter: ${matterName}. Open matter menu`}
          className="flex min-w-0 shrink-0 items-center gap-2 rounded-lg px-2 py-1 text-left outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="min-w-0">
            <span className="block max-w-52 truncate text-sm font-semibold">{matterName}</span>
            {clientName ? <span className="block max-w-52 truncate text-xs text-muted-foreground">{clientName}</span> : null}
          </span>
          <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content side="bottom" align="start" className="z-50 mt-1 max-h-[min(32rem,80vh)] min-w-60 overflow-y-auto rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
          {permissions.error ? <p role="alert" className="px-3 py-2 text-xs text-destructive">Matter permissions could not be loaded: {permissions.error.message}</p> : null}
          {groups.map((group, index) => (
            <DropdownMenu.Group key={group.label ?? "top"}>
              {index ? <DropdownMenu.Separator className="my-1 h-px bg-border" /> : null}
              {group.label ? <DropdownMenu.Label className="px-3 pb-1 pt-1.5 text-xs font-medium text-muted-foreground">{group.label}</DropdownMenu.Label> : null}
              {group.items.map(({ label, icon: Icon, target }) => {
                const active = target === current;
                return (
                  <DropdownMenu.Item key={target} asChild className={cn("flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted", active && "font-semibold text-primary")}>
                    <Link href={matterNavHref(clientId, matterId, target)} aria-current={active ? "page" : undefined}>
                      <Icon className="size-4 shrink-0" aria-hidden="true" />
                      <span className="flex-1">{label}</span>
                      {active ? <Check className="size-4 shrink-0" aria-hidden="true" /> : null}
                    </Link>
                  </DropdownMenu.Item>
                );
              })}
            </DropdownMenu.Group>
          ))}
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
