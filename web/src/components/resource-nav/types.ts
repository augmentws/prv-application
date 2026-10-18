import type { LucideIcon } from "lucide-react";

/** One entry in a resource's sidebar menu (a matter's or a collection's). */
export interface NavItem<T extends string> {
  label: string;
  icon: LucideIcon;
  /** The section the item opens. Each resource maps targets to URLs itself. */
  target: T;
  /** Resource permission needed to see the item. UI visibility only: the API enforces every permission. */
  permission?: string;
}

export interface NavGroup<T extends string> {
  /** Groups without a label sit at the top of the menu. */
  label?: string;
  items: NavItem<T>[];
}

/** The groups and items this user may see; groups left empty are dropped. */
export function visibleNav<T extends string>(groups: readonly NavGroup<T>[], permissionKeys: ReadonlySet<string>): NavGroup<T>[] {
  return groups
    .map((group) => ({ ...group, items: group.items.filter((item) => !item.permission || permissionKeys.has(item.permission)) }))
    .filter((group) => group.items.length);
}
