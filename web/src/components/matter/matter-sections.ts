import { Activity, BrainCircuit, Coins, Database, FileSearch, FileText, Layers, LayoutDashboard, ListTree, Sparkles, Tags, UsersRound, type LucideIcon } from "lucide-react";

import { type MatterSection, matterHref, matterJobsHref, searchAndReviewHref } from "@/lib/matter-routes";

export interface MatterNavItem {
  label: string;
  icon: LucideIcon;
  /** A matter section route, or "search-and-review" for the separate review workspace. */
  target: MatterSection | "search-and-review";
  /** Matter permission needed to see the item. */
  permission?: string;
}

export interface MatterNavGroup {
  /** Groups without a label sit at the top of the menu. */
  label?: string;
  items: MatterNavItem[];
}

/** The matter menu, grouped by the work being done. Shared by the sidebar and the review workspace. */
export const MATTER_NAV: MatterNavGroup[] = [
  { items: [{ label: "Overview", icon: LayoutDashboard, target: "overview" }] },
  {
    label: "Review",
    items: [
      { label: "Search and review", icon: FileSearch, target: "search-and-review" },
      { label: "Batches", icon: Layers, target: "batches" },
    ],
  },
  {
    label: "Findings",
    items: [
      { label: "Facts", icon: ListTree, target: "facts", permission: "fact.view" },
      { label: "Entities", icon: UsersRound, target: "entities", permission: "entity.view" },
    ],
  },
  {
    label: "Agents and models",
    items: [
      { label: "Analysis tasks", icon: Sparkles, target: "analysis" },
      { label: "Classifiers", icon: BrainCircuit, target: "classification" },
      { label: "Jobs", icon: Activity, target: "jobs" },
    ],
  },
  {
    label: "Setup",
    items: [
      { label: "Matter definition", icon: FileText, target: "definition" },
      { label: "Metadata fields", icon: Tags, target: "metadata" },
      { label: "Search index", icon: Database, target: "search-index" },
      { label: "Token usage", icon: Coins, target: "usage" },
    ],
  },
];

/** Sub-pages that belong to a menu item. */
const SECTION_PARENTS: Partial<Record<MatterSection, MatterSection>> = {
  "metadata-groups": "metadata",
  "facts-timeline": "facts",
  "entities-proposed": "entities",
};

export function matterNavTarget(section: MatterSection): MatterSection {
  return SECTION_PARENTS[section] ?? section;
}

export function matterSectionLabel(section: MatterSection): string {
  const target = matterNavTarget(section);
  for (const group of MATTER_NAV) {
    const item = group.items.find((candidate) => candidate.target === target);
    if (item) return item.label;
  }
  return "Overview";
}

/** The menu groups and items this user may see; groups left empty are dropped. UI visibility only. */
export function visibleMatterNav(permissionKeys: ReadonlySet<string>): MatterNavGroup[] {
  return MATTER_NAV
    .map((group) => ({ ...group, items: group.items.filter((item) => !item.permission || permissionKeys.has(item.permission)) }))
    .filter((group) => group.items.length);
}

export function matterNavHref(clientId: string, matterId: string, target: MatterNavItem["target"]) {
  if (target === "search-and-review") return searchAndReviewHref(matterId);
  // Jobs opens on document imports rather than redirecting through the bare section.
  if (target === "jobs") return matterJobsHref(clientId, matterId, "DOCUMENT_IMPORTS");
  return matterHref(clientId, matterId, target);
}
