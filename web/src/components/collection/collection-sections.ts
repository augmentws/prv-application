import { CalendarDays, Files, UsersRound, WandSparkles } from "lucide-react";

import type { NavGroup, NavItem } from "@/components/resource-nav/types";
import { visibleNav } from "@/components/resource-nav/types";
import { type CollectionSection, collectionHref } from "@/lib/collection-routes";

export type CollectionNavItem = NavItem<CollectionSection>;
export type CollectionNavGroup = NavGroup<CollectionSection>;

/** The collection menu, grouped like the matter menu. */
export const COLLECTION_NAV: CollectionNavGroup[] = [
  { items: [{ label: "Documents", icon: Files, target: "documents" }] },
  {
    label: "Explore",
    items: [
      { label: "Custodians", icon: UsersRound, target: "custodians" },
      { label: "File dates", icon: CalendarDays, target: "file-dates" },
    ],
  },
  {
    label: "Setup",
    items: [{ label: "Text processing", icon: WandSparkles, target: "processing" }],
  },
];

export function collectionSectionLabel(section: CollectionSection): string {
  for (const group of COLLECTION_NAV) {
    const item = group.items.find((candidate) => candidate.target === section);
    if (item) return item.label;
  }
  return "Documents";
}

/** The menu groups and items this user may see. UI visibility only. */
export function visibleCollectionNav(permissionKeys: ReadonlySet<string>): CollectionNavGroup[] {
  return visibleNav(COLLECTION_NAV, permissionKeys);
}

export function collectionNavHref(clientId: string, collectionId: string, target: CollectionSection) {
  return collectionHref(clientId, collectionId, target);
}
