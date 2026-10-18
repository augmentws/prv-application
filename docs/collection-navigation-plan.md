# Collection navigation: the same contextual sidebar and routes as matters

**Status:** Proposed, not started
**Scope:** web only. No API or backend changes.
**Depends on:** [matter-navigation-plan.md](matter-navigation-plan.md) (PR #12). Build this on
top of it, not alongside it.

## Goal

Collections and matters should be navigated the same way. Open either one and the global sidebar
becomes that resource's menu: a "← Client" link, a switcher, and grouped sections. Every section
is a real route, so Back, reload, bookmarks and link sharing all work. Both use one sidebar
component, one shell pattern and one way of building URLs, so they can't drift apart later.

## Problem

`CollectionView` (`web/src/components/views/collection-view.tsx`, 606 lines) is where
`MatterView` was before PR #12, and in one way it is worse:

- **No URL state at all.** The four tabs (Documents, Custodians, Date Histogram, Processing) are
  `useState`, not even `?tab=`. Search text, facet filters, the file-date range and the page
  offset are also only in memory.
- So **Back skips past every tab and filter change** and lands on the client page. Reload drops
  you on Documents with nothing filtered. A filtered view can't be bookmarked or sent to someone.
- **The histogram drill-down is one-way.** Picking a bucket calls `setFileDateRange` and
  `setTab("documents")` (`collection-view.tsx:~510`); Back can't return to the chart.
- **The sidebar stays global** inside a collection. Nothing in it shows where you are, and the
  only way to another collection is back through the client page. A matter now has a switcher
  and its own menu; a collection has neither.
- **Hand-built breadcrumb and header** (`collection-view.tsx:393`), with its own loading and
  error handling instead of a shell.
- **Every section's queries sit in one component**, switched on and off with `enabled: tab === …`.
- **Collection permissions aren't used for visibility.** The API has a `COLLECTION` resource
  type (`app/authorization.py:28`) and keys such as `collection.process`,
  `collection.configure`, `collection.delete` and `collection.publish`, but the page only knows
  the client-level `collection.view`/`collection.create` checks made on the client page.
  Processing and Delete appear for everyone who can open the collection; the API rejects the
  request afterwards.
- **No way back from a matter.** A document import job has `source_collection_id`, but the
  Document imports page doesn't link to the collection.

What already works and stays as it is: the tenant comes from the route
(`parseClientPath` covers `/collections/`), and "Add all matching" opens the matter's Document
imports through `matterJobsHref`.

## Target

```
Inside a matter (as built)          Inside a collection (new)
┌──────────────────────┐            ┌──────────────────────┐
│ Priv-View            │            │ Priv-View            │
│ ← TREC               │            │ ← TREC               │  back to the client
│ [Trec-JBush ▾]       │            │ [Enron mailboxes ▾]  │  switcher (same client)
│ Overview             │            │ Explore              │
│ Review …             │            │   Documents          │
│ Findings …           │            │   Custodians         │
│ Agents and models …  │            │   File dates         │
│ Setup …              │            │ Setup                │
│                      │            │   Text processing    │
│ ⋯ Workspace          │            │ ⋯ Workspace          │  global links, same menu
│ [user ▾]             │            │ [user ▾]             │
└──────────────────────┘            └──────────────────────┘
```

Everything that holds for matters holds for collections:

- Same width, collapse button (icons and tooltips) and mobile drawer. On a phone the drawer is the
  collection menu.
- Global links move to the same **Workspace** menu next to the user menu.
- The tenant comes from the route and the tenant select is hidden (already true).
- The page breadcrumb is Clients / Client / Collection / Section, drawn by a shell, with the
  section name as the last crumb, as in `MatterShell`.
- The switcher keeps the current section when you switch.
- The in-page tab row goes away; the sidebar is the section navigation.

### Routes

```
Explore
  Documents        /app/clients/:c/collections/:k            (root, unchanged URL)
  Custodians       …/custodians
  File dates       …/file-dates
Setup
  Text processing  …/processing
```

- The root stays Documents, so every existing collection link still lands where it did. No
  redirect shim is needed, because there was never any URL state to translate.
- "Date Histogram" becomes **File dates**. The chart plots canonical file dates, and the menu label
  then says what it's for. The docs already call the field "File date".
- Text processing sits under Setup, matching where Matter definition and Metadata fields sit in
  the matter menu. It's shown only with `collection.process` or `collection.configure`.

### Query string (Documents)

The Documents search state lives in the URL, read and written by one hook.

| Param | Meaning |
|---|---|
| `q` | filename / source path search |
| `custodian` (repeated) | custodian IDs |
| `ext` (repeated) | file extensions |
| `type` (repeated) | record types |
| `status` (repeated) | processing statuses |
| `from`, `to` | inclusive file-date range (`YYYY-MM-DD`) |
| `offset` | page offset; omitted when 0 |
| `item` | open document in the viewer, like `?job=` on job pages |

- Typing in the search box doesn't touch the URL; **Search** (submit) does, with `router.push`.
  Facet and date changes also `push`, so each is one Back step. Paging uses `replace`, so paging
  doesn't fill history.
- Any filter change resets `offset`.
- **File dates → Documents** becomes a plain link: `…/collections/:k?from=…&to=…`. Back
  returns to the chart.
- "Add all matching" builds its frozen selection from the parsed URL, so what's on screen is
  exactly what gets imported.
- Which facet groups are expanded, and the custodian checkboxes on Custodians, stay in component
  state. They're selection, not location.

## Shared navigation

The matter nav was written for matters only. This change lifts the generic part out so both
resources use it.

| New shared piece | Taken from | Used by |
|---|---|---|
| `src/components/resource-nav/resource-sidebar-nav.tsx` | the chrome in `MatterSidebarNav`: "← Client" link, switcher dropdown, grouped item list, collapsed/drawer modes, `aria-current` | `MatterSidebarNav`, `CollectionSidebarNav` |
| `src/components/resource-nav/types.ts` | `MatterNavItem` / `MatterNavGroup` made generic: `NavItem<T>` (label, icon, target, permission), `NavGroup<T>`, `visibleNav(groups, permissionKeys)` | both section configs |
| `src/lib/workspace-path.ts` | add `parseCollectionPath(pathname)` and `parseResourcePath(pathname)` returning `{ kind: "matter", clientId, matterId } \| { kind: "collection", clientId, collectionId } \| null` | `AppShell` |

`ResourceSidebarNav` takes data, not queries:

```ts
interface ResourceSidebarNavProps<T extends string> {
  label: string;                        // "Matter" | "Collection": nav aria-label and switcher label
  client: { id: string; name?: string };
  current: { id: string; name?: string };
  siblings: { data?: { id: string; name: string }[]; isPending: boolean; error: Error | null };
  groups: NavGroup<T>[];                // already filtered by permission
  activeTarget: T;
  hrefFor: (target: T) => string;
  onSwitch: (id: string) => void;       // caller keeps the section
  collapsed: boolean;
  onNavigate: () => void;
}
```

`MatterSidebarNav` keeps its queries and props, and renders `ResourceSidebarNav`. Its tests don't
change; they're the proof the extraction kept behavior.

## Files

### New

| File | Purpose |
|---|---|
| `src/lib/collection-routes.ts` | `CollectionSection` (`documents`, `custodians`, `file-dates`, `processing`), `collectionHref(clientId, collectionId, section?, params?)`, `collectionSectionFromPath(pathname)`, `isCollectionSectionPage(…)`. The only place collection URLs are built. |
| `src/lib/collection-routes.test.ts` | Paths both ways, params preserved, unknown segment → `null`. |
| `src/lib/collection-filters.ts` | `parseCollectionFilters(searchParams)` → `{ search, facets, fileDateRange, offset, itemId }`; `collectionFilterSearch(filters)` → `URLSearchParams`; `toImportSelection(filters)` for "Add all matching". Pure functions. |
| `src/lib/collection-filters.test.ts` | Round trip, repeated params, empty values dropped, offset reset, the import selection matches the current query. |
| `src/components/collection/collection-sections.ts` | `COLLECTION_NAV` (groups above, with icons and permissions), `collectionNavTarget`, `collectionSectionLabel`, `collectionNavHref`. Same shape as `matter-sections.ts`. |
| `src/components/collection/collection-context.tsx` | `CollectionProvider` / `useCollection()`: `{ clientId, collectionId, client, collection, permissionKeys }`. Keys `["client", id]`, `["collection", id]`, `["effective-permissions", "COLLECTION", id]`, shared with the sidebar. Also checks `collection.client_id === clientId` (moved from `collection-view.tsx`). |
| `src/components/collection/collection-shell.tsx` | Layout body: breadcrumb (Clients / Client / Collection / Section), title, `StatusBadge`, `DeleteCollectionDialog` (shown with `collection.delete`), `HelpLink` per section. Loading and error once. |
| `src/components/collection/collection-sidebar-nav.tsx` | Loads client, collection, sibling collections (`["collections", tenantId, clientId]`, the client page's key) and permissions; renders `ResourceSidebarNav`. Switching keeps the section and drops Documents filters (custodian and item IDs don't carry across collections). |
| `src/components/collection/use-collection-filters.ts` | Hook over `useSearchParams` + `collection-filters.ts`; exposes setters that `push` or `replace` as described above. |
| `src/components/collection/sections/{documents,custodians,file-dates,processing}-section.tsx` | One per section, each with its own queries. Processing renders `CollectionProcessingPanel`. |
| `src/components/collection/test-utils.tsx` | `renderInCollection`, mirroring `renderInMatter`. |
| `app/(workspace)/app/clients/[clientId]/collections/[collectionId]/layout.tsx` | Wraps `children` in `CollectionShell`. |
| `…/[collectionId]/{custodians,file-dates,processing}/page.tsx` | Thin server pages with their own `metadata.title`. |
| `src/components/resource-nav/*` | Shared pieces above, with `resource-sidebar-nav.test.tsx`. |

### Changed

| File | Change |
|---|---|
| `src/components/app-shell.tsx` | Use `parseResourcePath`. `kind: "matter"` → `MatterSidebarNav`; `kind: "collection"` → `CollectionSidebarNav`. The Workspace menu shows for either. Nothing else changes. |
| `src/components/matter/matter-sidebar-nav.tsx` | Renders `ResourceSidebarNav`; keeps its own queries and switch logic. |
| `src/components/matter/matter-sections.ts` | Types come from `resource-nav/types.ts`; `visibleMatterNav` calls `visibleNav`. Exports stay, so callers don't change. |
| `src/components/matter/matter-shell.tsx` | Optional: share a `ResourceBreadcrumb` with `CollectionShell`. Only if it stays readable; no behavior change. |
| `…/collections/[collectionId]/page.tsx` | Renders `DocumentsSection`. |
| `src/components/views/collection-view.tsx` | Deleted at the end of phase 3. |
| `src/components/views/client-view.tsx` | Collection links use `collectionHref`. |
| `src/components/matter/sections/jobs/import-jobs.tsx` | Source collection column / job detail links to `collectionHref(clientId, source_collection_id)`, shown when the user can see collections. Names come from the client's collections query (already cached from the client page and sidebar). |
| `content/docs/collections.mdx`, `clients-and-matters.mdx`, `help-topics.ts` (if needed) | "The collection workspace has four tabs" → the collection menu; Date Histogram → File dates; addresses and Back. |

## Phases

One branch (`feature/<ticket>-collection-navigation`), one commit per phase, each passing
`pnpm typecheck`, `pnpm lint` and `pnpm test` in `web/`.

0. **Extract the shared sidebar.** `resource-nav/` plus `MatterSidebarNav` on top of it. Pure
   refactor: the matter sidebar, section menu and app-shell tests pass unchanged.
1. **Collection routes, shell and sidebar.** `collection-routes.ts`, the collection context,
   shell, sidebar nav, `parseResourcePath` in `AppShell`, the layout and the three section pages.
   Each page renders the existing `CollectionView` with a `section` prop and its header and tab
   row removed, so behavior otherwise stays put. Back works between sections from here.
2. **Documents filters in the URL.** `collection-filters.ts`, the hook, File dates → Documents as
   a link, "Add all matching" built from the URL, `?item=` for the viewer.
3. **Split `CollectionView`.** One section component at a time, each with its own queries; then
   delete `CollectionView`. Permission-gated Processing and Delete land here.
4. **Matter ↔ collection links.** Source collection links from Document imports.
5. **Docs.**

Phases 1 and 2 fix the user-visible bugs; 0 and 3 are what keep the two navigations the same over
time.

## Tests

Nothing deleted or weakened. Where a test clicked a tab button, it now follows a link `href` or
renders the section page directly.

- **`resource-sidebar-nav.test.tsx` (new).** Groups and order, `aria-current`, collapsed mode has
  accessible names, switcher lists siblings and calls `onSwitch`, loading and error rows.
- **`matter-sidebar-nav.test.tsx`, `matter-section-menu.test.tsx`, `app-shell.test.tsx`.**
  Unchanged in phase 0. `app-shell` gains: on a collection path it shows the collection menu and
  Workspace menu and hides the tenant select; on the client page it shows global links.
- **`collection-sidebar-nav.test.tsx` (new).** Active item from `usePathname` for each section;
  Text processing hidden without `collection.process`/`collection.configure`; switching keeps
  the section and drops filters.
- **`collection-routes.test.ts`, `collection-filters.test.ts` (new).** As listed above.
- **Section tests (new).** Documents: filters from the URL reach the search request; a facet
  click pushes the new URL with `offset` removed; "Add all matching" sends the URL's selection.
  File dates: a bucket is a link with `from`/`to`. Custodians: "Add documents to matter" still
  posts the selected custodians.
- **`import-jobs` / `jobs-section` test.** The source collection renders as a link.
- **e2e.** Same gap as the matter plan: no signed-in, seeded fixture. If that harness gets
  added for matters, add `e2e/collection-navigation.spec.ts` (filter → Back restores; File
  dates bucket → Back returns to the chart). Otherwise, unit tests plus a manual pass.

**Manual check before merge:** each collection section from the sidebar; Back through tabs and
filter changes; reload a filtered Documents URL; File dates → Documents → Back; switch collection
from every section; Add all matching with filters, then confirm the import's selection summary;
phone width; keyboard through the sidebar expanded and collapsed; a matter page, to confirm
nothing moved.

## Risks and open questions

- **Labels.** "File dates" for Date Histogram and "Text processing" for Processing are
  suggestions. If people know the old names, keep them; only the menu labels change.
- **Should the collection menu have an Overview?** Matters land on Overview; collections land on
  Documents because there's nothing to summarize yet. If a summary page (counts, custodians,
  processing state, matters fed) is wanted later, it becomes the root and Documents moves to
  `…/documents`, with the root redirecting while query params are present.
- **Matter → Sources.** A matter menu item listing the collections it imported from would need
  imports grouped by `source_collection_id`, either client-side from the import jobs or with a
  small endpoint. Left out of scope; phase 4 covers the per-job link.
- **Collection-level permissions in the UI are new.** Someone who could see the Processing tab
  and only got errors will now not see it. That's the intent, but it's a visible change.
- **Shared extraction touches finished matter code.** Phase 0 is deliberately a pure refactor
  with the matter tests as its check, so that risk is contained.
- **Ticket ID** for the branch name.
