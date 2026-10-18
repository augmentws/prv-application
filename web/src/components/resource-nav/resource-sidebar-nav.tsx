"use client";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Check, ChevronLeft, ChevronsUpDown } from "lucide-react";
import Link from "next/link";

import type { NavGroup } from "@/components/resource-nav/types";
import { cn } from "@/lib/utils";

interface NamedResource {
  id: string;
  name?: string;
}

export interface ResourceSidebarNavProps<T extends string> {
  /** The resource kind, singular and capitalized ("Matter", "Collection"): the nav's name and the switcher's label. */
  label: string;
  /** The resource kind in a sentence ("matters", "collections"), used in the switcher's loading and error rows. */
  pluralLabel: string;
  client: NamedResource;
  current: NamedResource;
  /** The client's other resources of this kind, for the switcher. */
  siblings: { data?: { id: string; name: string }[]; isPending: boolean; error: Error | null };
  /** Menu groups, already filtered to what the user may see. */
  groups: NavGroup<T>[];
  activeTarget: T;
  hrefFor: (target: T) => string;
  /** Called with another resource's ID. The caller decides where it lands, normally the same section. */
  onSwitch: (id: string) => void;
  /** Shown under the menu when the resource's permissions could not be loaded. */
  permissionsError?: Error | null;
  collapsed: boolean;
  onNavigate: () => void;
}

/**
 * A resource's own menu, shown in the app sidebar in place of the workspace links while a matter or
 * collection is open: a link back to the client, a switcher, and the grouped sections.
 */
export function ResourceSidebarNav<T extends string>({
  label,
  pluralLabel,
  client,
  current,
  siblings,
  groups,
  activeTarget,
  hrefFor,
  onSwitch,
  permissionsError,
  collapsed,
  onNavigate,
}: ResourceSidebarNavProps<T>) {
  const clientName = client.name ?? "Client";
  const currentName = current.name ?? label;

  return (
    <nav aria-label={label} className="flex-1 overflow-y-auto p-3">
      <Link
        href={`/app/clients/${client.id}`}
        title={collapsed ? clientName : undefined}
        onClick={onNavigate}
        className={cn("mb-2 flex h-8 items-center gap-1.5 rounded-lg px-2 text-sm text-muted-foreground outline-none hover:bg-card hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring", collapsed && "justify-center px-0")}
      >
        <ChevronLeft className="size-4 shrink-0" />
        {!collapsed ? <span className="truncate">{clientName}</span> : <span className="sr-only">{clientName}</span>}
      </Link>

      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            aria-label={`${label}: ${currentName}. Switch ${label.toLowerCase()}`}
            title={collapsed ? currentName : undefined}
            className={cn("mb-3 flex w-full items-center gap-2 rounded-lg border border-sidebar-border bg-card px-3 py-2 text-left outline-none hover:border-input focus-visible:ring-2 focus-visible:ring-ring", collapsed && "justify-center px-0")}
          >
            {collapsed ? (
              <span className="grid size-6 place-items-center rounded bg-primary/10 text-xs font-bold text-primary">{currentName.slice(0, 1).toUpperCase()}</span>
            ) : (
              <>
                <span className="min-w-0 flex-1 truncate text-sm font-semibold">{currentName}</span>
                <ChevronsUpDown className="size-4 shrink-0 text-muted-foreground" />
              </>
            )}
          </button>
        </DropdownMenu.Trigger>
        <DropdownMenu.Portal>
          <DropdownMenu.Content side="bottom" align="start" className="z-50 mt-1 max-h-80 min-w-56 overflow-y-auto rounded-lg border bg-popover p-1 text-popover-foreground shadow-xl">
            {siblings.error ? <p role="alert" className="px-3 py-2 text-sm text-destructive">{capitalize(pluralLabel)} could not be loaded: {siblings.error.message}</p> : null}
            {siblings.isPending ? <p className="px-3 py-2 text-sm text-muted-foreground">Loading {pluralLabel}…</p> : null}
            {siblings.data?.map((item) => (
              <DropdownMenu.Item
                key={item.id}
                onSelect={() => { if (item.id !== current.id) onSwitch(item.id); }}
                className="flex cursor-default items-center gap-2 rounded-md px-3 py-2 text-sm outline-none focus:bg-muted"
              >
                <Check className={cn("size-4", item.id === current.id ? "opacity-100" : "opacity-0")} aria-hidden="true" />
                <span className="truncate">{item.name}</span>
              </DropdownMenu.Item>
            ))}
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <div className="space-y-3">
        {groups.map((group) => (
          <div key={group.label ?? "top"} role="group" aria-label={group.label}>
            {group.label && !collapsed ? <p className="mb-1 px-3 text-xs font-medium text-muted-foreground">{group.label}</p> : null}
            <ul className="space-y-0.5">
              {group.items.map(({ label: itemLabel, icon: Icon, target }) => {
                const active = target === activeTarget;
                return (
                  <li key={target}>
                    <Link
                      href={hrefFor(target)}
                      title={collapsed ? itemLabel : undefined}
                      aria-current={active ? "page" : undefined}
                      onClick={onNavigate}
                      className={cn(
                        "flex h-9 items-center gap-3 rounded-lg px-3 text-sm font-medium outline-none transition hover:bg-card focus-visible:ring-2 focus-visible:ring-ring",
                        active && "bg-primary text-primary-foreground shadow-sm hover:bg-primary",
                        collapsed && "justify-center px-0",
                      )}
                    >
                      <Icon className="size-[1.05rem] shrink-0" aria-hidden="true" />
                      {!collapsed ? <span className="truncate">{itemLabel}</span> : <span className="sr-only">{itemLabel}</span>}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
      {permissionsError ? <p role="alert" className="mt-3 px-3 text-xs text-destructive">{label} permissions could not be loaded: {permissionsError.message}</p> : null}
    </nav>
  );
}

function capitalize(text: string) {
  return text.slice(0, 1).toUpperCase() + text.slice(1);
}
