# Matter navigation: contextual sidebar and route-based sections

**Status:** Proposed, not started
**Scope:** web only. No API or backend changes.

## Problem

Matter navigation is one client component, `MatterView`
(`web/src/components/views/matter-view.tsx`, 1,033 lines). It shows eleven flat tabs, chosen
with `?tab=` and changed with `router.replace`. Because of that:

- the browser Back button skips past tab changes;
- a matter opens on Metadata (`matter-view.tsx:82`) instead of Overview;
- the tab row scrolls sideways at laptop widths;
- Jobs hides seven job types behind a Select, and the last one picked is kept in `localStorage`;
- entity, fact, topic and classifier detail pages each build their own breadcrumb or back link,
  and the links don't agree;
- Search & Review (`/review/matters/:id`) sits outside the matter. The only way back is a small
  "Matter" or "Batches" ghost link.

### Confirmed in the running app (2026-10-17, Tenant One / TREC / Trec-JBush, 1440px)

- A matter opens on Metadata. The page has no matter title, because the "Search & Review"
  button takes the title's slot in `ResourcePageHeader`.
- Overview → Jobs → Back lands on the client page. Both matter tabs are skipped.
- Jobs opened on "Classification analysis", the last type kept in `localStorage`. The whole tab
  shows a skeleton for about 5 seconds while every job-type query resolves.
- **Tenant context drifts.** The tenant selected in the sidebar is in-memory state
  (`workspace-context.tsx:21`). Any reload of a matter URL resets the sidebar and header to the
  home tenant ("Priv-View Root") while the content shows Tenant One's matter. Tenant-scoped
  permissions are then worked out for the wrong tenant.
- The tenant name appears three times on list pages: sidebar, top header ("Secure workspace"),
  and the page eyebrow.
- **The global sidebar (256px) is already a left menu.** Facts adds its own filter column, Matter
  definition has three panes, and Search & Review is a full-screen layout with four panes.
  Adding another rail would leave Facts with three left columns and squeeze the review panes.
- The fact detail breadcrumb starts at the client ("TREC") rather than "Clients". It also has a
  "Back to facts" link that repeats the last crumb, and a generic "Fact" title.
- At 400px the tab row scrolls and the active tab is off screen. "Matter definition" wraps onto
  two lines.
- Overview counts are unformatted (`290099`) and show "—" for several seconds with no loading
  state.

## Target

Inside a matter, **the global sidebar changes to show the matter's sections**. It doesn't add a
second rail. Every section is a real route, so Back, bookmarks and link sharing all work.

```
Outside a matter (unchanged)        Inside a matter
┌──────────────────────┐            ┌──────────────────────┐
│ Priv-View            │            │ Priv-View            │
│ [Tenant ▾]           │            │ ← TREC               │  back to the client
│ Overview             │            │ [Trec-JBush ▾]       │  matter switcher
│ Clients              │            │ Overview             │
│ Documentation        │            │ Review               │
│ Access control       │            │   Search and review  │
│ Audit log            │            │   Batches            │
│ Tenants / Agents /   │            │ Findings …           │
│ Skills (superuser)   │            │ Agents and models …  │
│                      │            │ Setup …              │
│ [user ▾]             │            │ [user ▾]  ⋯ Workspace│  global links in a menu
└──────────────────────┘            └──────────────────────┘
```

- The sidebar keeps its width, collapse button (icons with tooltips) and mobile drawer. On a
  phone, the drawer becomes the matter menu, which fixes the off-screen tab problem.
- While you're in a matter, global links (Overview, Clients, Documentation, Access control,
  Audit log, admin) move to a "Workspace" menu next to the user menu. "← TREC" and the
  breadcrumb also lead back out.
- Inside a matter, the tenant comes from the matter's client (`client.tenant_id`), not from
  in-memory selection. The tenant select is hidden there, because a matter belongs to exactly
  one tenant.
- Search & Review stays full screen and gets no rail. Its existing "← Matter" button becomes a
  matter menu (a dropdown with the same sections), so it costs no pane width.

```
Overview                      /app/clients/:c/matters/:m
Review
  Search and review           /review/matters/:m                  (unchanged URL, leaves the shell)
  Batches                     …/batches
Findings
  Facts                       …/facts            …/facts/timeline
  Entities                    …/entities         …/entities/proposed
Agents and models
  Analysis tasks              …/analysis
  Classifiers                 …/classification   (+ existing …/classification-models/:id)
  Jobs                        …/jobs/:jobType    (+ existing …/topic-jobs/:id)
Setup
  Matter definition           …/definition
  Metadata fields             …/metadata         …/metadata/groups
  Search index                …/search-index
  Token usage                 …/usage
```

Segment names keep the current tab keys. There are two exceptions:

- `search` becomes `search-index`, so it can't be confused with Search and review.
- Facts and entities sub-tabs become child segments. Existing detail routes (`facts/[factId]`,
  `entities/[entityId]`) live next to them. Next gives the static segments `timeline` and
  `proposed` priority over the dynamic ones, and the IDs are UUIDs, so they can't collide.

Job type slugs (`jobs/[jobType]`), mapped to the existing `MatterJobType` values:

| slug | MatterJobType |
|---|---|
| `imports` | DOCUMENT_IMPORTS |
| `bulk-tagging` | BULK_TAGGING |
| `embeddings` | DOCUMENT_EMBEDDINGS |
| `topic-clustering` | TOPIC_CLUSTERING |
| `question-answering` | QUESTION_ANSWERING |
| `classification-analysis` | CLASSIFICATION_ANALYSIS |
| `search-exports` | SEARCH_EXPORTS |

`jobs/` with no slug redirects to `jobs/imports`. A selected job stays a query param (`?job=`),
because job detail renders inline above the table today. An unknown slug calls `notFound()`.

## Files

### New

| File | Purpose |
|---|---|
| `src/lib/matter-routes.ts` | `matterHref(clientId, matterId, section, params?)`, `JOB_TYPE_SLUGS` (both directions), `legacyMatterTabHref(clientId, matterId, searchParams)`. This is the only place matter URLs are built. |
| `src/lib/matter-routes.test.ts` | The mapping table below, case by case. |
| `src/components/matter/matter-sections.ts` | Section configuration (shared by the sidebar nav and the review menu): groups, items, icon, segment, required permission. |
| `src/components/matter/matter-context.tsx` | `MatterProvider` and `useMatter()`: `{ clientId, matterId, client, matter, permissionKeys }`. Uses the existing query keys (`["client", id]`, `["matter", id]`, `["effective-permissions","MATTER",id]`), so the cache is shared with the current code. |
| `src/components/matter/matter-shell.tsx` | Client component used by the matter layout. Loads context and renders the page breadcrumb (Clients / Client / Matter / Section) plus `children`. Handles loading and error states once instead of per tab. Draws no navigation chrome of its own. |
| `src/components/matter/matter-sidebar-nav.tsx` | The matter's section list, rendered by `AppShell` inside the existing sidebar when the path is inside a matter. Works out the active item from `usePathname()`; it can't use `useSelectedLayoutSegments()` because `AppShell` sits above the matter layout. Sets `aria-current="page"` and supports the existing collapsed (icon-only) and mobile-drawer modes. Hides Facts and Entities without `fact.view` / `entity.view`, the same checks as today. |
| `src/components/matter/matter-section-menu.tsx` | Dropdown with the same sections, used in the Search & Review header in place of the "← Matter" button. |
| `src/lib/matter-path.ts` | `parseMatterPath(pathname)` returns `{ clientId, matterId } \| null` for `/app/clients/:c/matters/:m/**`. Used by `AppShell` and `WorkspaceProvider`. |
| `src/components/matter/matter-switcher.tsx` | Dropdown of the client's matters, built on the existing `["matters", clientId]` query (`GET /v1/clients/:id/matters`). Switching keeps the current section when the target matter allows it. |
| `src/components/matter/*.test.tsx`, `src/lib/matter-path.test.ts` | Tests for the sidebar nav, section menu, switcher and path parsing. |
| `app/(workspace)/app/clients/[clientId]/matters/[matterId]/layout.tsx` | `LayoutProps<…>`; wraps `children` in `MatterShell`. |
| `…/[matterId]/{batches,facts,facts/timeline,entities,entities/proposed,analysis,classification,definition,metadata,metadata/groups,search-index,usage}/page.tsx` | One thin server page per section, each with its own `metadata.title`. |
| `…/[matterId]/jobs/page.tsx` | Redirects to `jobs/imports`. |
| `…/[matterId]/jobs/[jobType]/page.tsx` | Validates the slug and renders that job type's section. |

### Changed

| File | Change |
|---|---|
| `src/components/app-shell.tsx` | When `parseMatterPath` matches: render "← Client", the matter switcher and `MatterSidebarNav` in place of the primary links; move the global links into a "Workspace" dropdown next to the user menu; hide the tenant select. Otherwise the sidebar is unchanged. |
| `src/components/workspace-context.tsx` | When inside a matter, set the selected tenant to the matter client's `tenant_id`, so a reload or deep link can't show the wrong tenant. Outside a matter, keep the in-memory selection. Persisting it across reloads is a separate question, not part of this change. |
| `…/[matterId]/page.tsx` | If `searchParams.tab` (or `metadata`, `entities`, `facts`, `job_type`) is present, `redirect(legacyMatterTabHref(...))`. Otherwise render Overview. |
| `src/components/views/matter-view.tsx` | Phase 1: keep as-is behind a `section` prop, with its header and tab row removed. Phase 2: split into section components (below) and delete. |
| `src/components/resource-page-header.tsx` | No API change. Matter pages pass a section-relative breadcrumb because the shell already shows client and matter. |
| `entities/entity-detail.tsx`, `facts/fact-detail.tsx`, `topic-review-page.tsx`, `classification-model-detail.tsx` | Remove the hand-built Clients / Client / Matter crumbs and back links. The breadcrumb becomes `Section / Item`, built with `matterHref`. These pages now render inside the shell, so they get matter navigation in the sidebar automatically. |
| `analysis-tasks-panel.tsx:444`, `batch-review-workspace.tsx:785,889,890`, `review-workspace.tsx:786,1105`, `views/collection-view.tsx:379`, `facts/fact-task-workspace.tsx:468`, `lib/facts.ts:194` | Replace the hand-built `?tab=` URLs with `matterHref`. After this, nothing in `src` builds `?tab=`; only the shim reads it. |
| `review-workspace.tsx:751`, `batch-review-workspace.tsx:785` | Phase 4: replace the "← Matter" ghost button with `MatterSectionMenu`, labelled with the matter name, with "Search and review" checked. Batch review keeps "← Batches" as its parent link and adds the same menu. No change to pane widths. `clientId` comes from `matter.data.client_id`, which is already fetched. |
| `entities/entity-detail.tsx`, `facts/fact-detail.tsx` | Also remove the "Back to …" link that repeats the last crumb. Use the item itself as the page title (for a fact, its short summary) instead of "Fact" or "Entity". |
| `content/docs/{clients-and-matters,facts,matter-jobs,…}.mdx` | Change "the **Jobs** tab" to "**Jobs** in the matter menu" and so on. |

### Phase 2 split of `MatterView`

Each section owns its own queries and mutations, so the 20+ `enabled: tab === …` gates go away:

| Component (`src/components/matter/sections/`) | Comes from |
|---|---|
| `overview-section.tsx` | Summary cards, Clone and Save template dialogs |
| `metadata-section.tsx` | Definitions table and columns, `MetadataGroupsPanel`, create and visibility mutations |
| `batches-section.tsx` | Review batch table, `ReviewBatchActions`, create dialog |
| `jobs/*.tsx` | One file per job type, with that type's columns and mutations, plus `DeletableJobActions` and the shared `JobDetail` |
| `search-index-section.tsx`, `usage-section.tsx` | Thin wrappers around the existing panels |

Facts, Entities, Analysis, Classification and Definition already have panels (`FactsListPanel`,
`EntitiesPanel`, and so on). Their pages render those panels directly.

## Redirect shim

`legacyMatterTabHref` is a server-side `redirect()` from the matter root page. It's a pure
function of the old query string. All other query params pass through unchanged; facts filters
(`ff_*`, `fentity`) and entity filters must survive.

| Legacy query | New path |
|---|---|
| no `tab` | Overview (root). **Behavior change:** the old default was Metadata. |
| `tab=overview` | root |
| `tab=metadata`, `tab=metadata&metadata=groups`, `tab=groups` | `metadata`, `metadata/groups`, `metadata/groups` |
| `tab=definition` | `definition` |
| `tab=facts[&facts=timeline]` | `facts[/timeline]` |
| `tab=entities[&entities=proposed]` | `entities[/proposed]` |
| `tab=analysis`, `batches`, `classification`, `usage` | same segment |
| `tab=search` | `search-index` |
| `tab=jobs&job_type=X[&job=Y]` | `jobs/<slug(X)>[?job=Y]` |
| `tab=jobs&job=Y` (no type) | `jobs/imports?job=Y` (matches current `matter-view.tsx:92`) |
| `tab=jobs` | `jobs/imports` |
| unknown `tab` | root |

The old `localStorage` key `priv-view:matter-jobs:selected-type` is no longer read. Nothing
needs migrating; the stale key is harmless.

## As built (phases 0 and 1)

Where the implementation differs from the plan above:

- The path parser is `src/lib/workspace-path.ts` (`parseClientPath`, `parseMatterPath`), not
  `matter-path.ts`. The tenant now comes from the route on every `/app/clients/:id/**` page, not
  only on matter pages.
- The matter `layout.tsx` and `MatterShell` are deferred to phase 2. The sidebar reads the URL
  inside `AppShell`, so phase 1 didn't need them.
- The section name is the last breadcrumb item (Clients / Client / Matter / Section) instead of a
  heading, because the panels already have their own headings.
- `matterSectionFromPath` lives in `matter-routes.ts`; menu labels and groups live in
  `components/matter/matter-sections.ts`.
- Jobs keeps `?job_type=` until phase 3. The Topic review "Back to jobs" link now opens Topic
  clustering.
- `app-shell.test.tsx` already existed; the new cases were added to it.

## As built (phase 2)

- `MatterView` is deleted. `[matterId]/layout.tsx` renders `MatterShell`, which wraps
  `MatterProvider` (`components/matter/matter-context.tsx`). The provider loads client, matter
  and permissions once and waits only on client and matter. The shell draws the breadcrumb and
  title only on section pages; detail pages keep their own headers.
- Sections live in `components/matter/sections/` (overview, metadata, facts, entities, batches,
  classification, usage, search-index). Jobs has one file per type in `sections/jobs/`, with
  shared `DeletableJobActions`, `useDeleteJob` and `ProgressBar` in `job-actions.tsx`.
  Analysis and Matter definition pages render their existing panels directly.
- Behavior changes: each job type loads only its own data (before, every job list loaded at
  once), and the job-type picker appears straight away instead of after all lists load.
  Metadata groups load only on the Groups subtab.
- The 13 `matter-view` tests moved, as they were, to `facts-section`, `jobs/jobs-section`,
  `batches-section`, `metadata-section` and `usage-section` tests. They render through
  `components/matter/test-utils.tsx` (`renderInMatter`), which uses the real `MatterShell`.
- (Phase 3, below, replaces the job-type picker.)
- Still open: the detail pages' own breadcrumbs (they start at the client, repeat the last crumb
  with a "Back to …" link, and use a generic title) are unchanged.

## Phases

Everything goes on one branch, with one commit per phase. Each phase passes typecheck, lint and
tests on its own.

0. **Tenant context fix.** `workspace-context.tsx` derives the tenant from the matter. This is
   small and can ship ahead of everything else, because it's a correctness bug.
1. **Contextual sidebar and routes.** Adds `matter-routes.ts` and `matter-path.ts` with tests,
   the matter layout and shell, `MatterSidebarNav` in `AppShell`, the switcher, the section
   pages and the shim. At this stage each section page renders
   `<MatterView section="…">` with its tab row removed, so all behavior stays in place. Internal
   links switch to `matterHref`. Back works from this phase on.
2. **Split `MatterView`.** A pure refactor into the section components, done one section at a
   time. Moves the matching `describe` blocks out of `matter-view.test.tsx`, then deletes
   `MatterView`.
3. **Jobs per route.** Adds `jobs/[jobType]` and removes the job-type Select and the
   `localStorage` store. QA and classification job pages get a "Analysis tasks" link through
   `matterHref`.
4. **Review integration.** Adds the matter section menu in both review workspaces.
5. **Docs.** Wording updates in `content/docs`.

## Tests

Nothing gets deleted or weakened. Where an assertion checked the old mechanism (a
`router.replace` call carrying `tab=`), it changes to check the new link `href` or the redirect
target. The behavior under test stays the same.

- **New `matter-routes.test.ts`.** Every row of the shim table, plus preserving facts and entity
  filters and the round-trip between job slugs and types.
- **New `matter-sidebar-nav.test.tsx`.** Groups and order; Facts and Entities hidden without
  permission; active item and `aria-current` from a mocked `usePathname`, including detail
  routes (a fact detail highlights Facts, a topic job highlights Jobs); collapsed mode has
  accessible names.
- **`app-shell` test (new; none exists today).** Inside a matter path it shows matter navigation
  and the Workspace menu and hides the tenant select. Outside, it shows the global links
  unchanged.
- **`workspace-context` test.** On a matter path, the tenant comes from the matter's client,
  whatever was selected before.
- **New `matter-switcher.test.tsx`.** Lists the client's matters and keeps the section on switch.
- **`views/matter-view.test.tsx` (13 tests).** These move to the section tests as they stand:
  facts sub-navigation becomes facts and timeline links that keep filters in the `href`; the jobs
  tests (4) go to the job-type section tests; batches (1); metadata navigation (5), where
  "configuration actions only on Overview" goes to the overview section and "legacy groups link"
  becomes a shim test; token usage (1).
- **`?tab=` assertions in 7 other test files:** `facts-timeline-panel`, `fact-task-workspace`,
  `topic-review-page`, `analysis-tasks-panel`, `entity-detail`, `facts-list-panel` and
  `entities-panel`. Update the expected `href`s.
- **Review workspace tests (phase 4).** Add mocks for the effective-permissions query the
  section menu reads, and one assertion that the menu lists the sections with Search and review
  checked.
- **e2e.** The only spec today, `e2e/login.spec.ts`, doesn't authenticate or seed data. I'm
  proposing `e2e/matter-navigation.spec.ts` (old `?tab=` URL redirects; Back returns to the
  previous section), but it needs a signed-in fixture and a seeded matter. Whether to add that
  harness is your call. Without it, this is a known gap covered by the unit tests and a manual
  check.

**Verification after each phase:** `pnpm typecheck`, `pnpm lint` and `pnpm test` in `web/`.
Before merging, a manual pass in the running app: each sidebar section, Back across sections, an old
bookmark URL for each tab, a phone-width layout, keyboard focus through the sidebar (expanded and collapsed), and both
review workspaces.

## Risks and open questions

- **Old links outside the app** (emails, notes, bookmarks) keep working through the shim. The
  shim stays until we decide to drop it.
- **A changed default landing page.** People used to Metadata will land on Overview. Overview is
  still the thin summary until the "needs attention" redesign, which is a separate follow-up and
  not part of this plan.
- **A running-jobs indicator in the sidebar** would need either seven list queries or a new summary
  endpoint. It's left out of scope; I'd suggest a backend `GET /v1/matters/:id/jobs/summary`
  later.
- **Global links are one click further away inside a matter,** because they sit in the
  Workspace menu. In return, matter navigation takes no extra width. If people often jump to
  Audit log or Documentation from inside a matter, those two could stay pinned at the bottom of
  the sidebar.
- **A superuser looking at another tenant's matter** will see the sidebar tenant change to match
  it. That's intended, but it's a visible change in behavior.
- **Branch name and ticket.** `feature/<ticket>-matter-navigation`. Which ticket ID should
  it use?

## As built (phase 3)

- Each job type has its own page at `jobs/[jobType]` (slugs in `MATTER_JOB_TYPES`,
  `lib/matter-routes.ts`); an unknown slug is a 404. `?job=` still selects a job on the page.
- `jobs/` with no slug, and older `jobs?job_type=…` and `?tab=jobs…` links, redirect through
  `legacyMatterJobsHref`; a `job` without a type opens Document imports, as before.
- The job-type picker and its `localStorage` memory are gone. The job types are links in a
  subtab row, so Back moves between them, and the sidebar's Jobs link opens Document imports.
- A selected classification run is read from the URL, so Back closes its detail.
- `isMatterSectionPage` decides where `MatterShell` draws the header, so job-type pages get it.

## As built (phase 4)

- `components/matter/matter-section-menu.tsx`: a dropdown whose trigger shows the matter and
  client names and whose items are the same sections as the sidebar, filtered by permission.
  It takes no pane width.
- Search and Review: the menu replaces the "← Matter" button and the name block, with
  "Search and review" marked current.
- Batch review: "← Batches" stays as the parent link; the menu sits next to it with "Batches"
  marked current, and the batch subtitle now reads "Batch review" (the menu shows matter and
  client).
- `visibleMatterNav` and `matterNavHref` in `matter-sections.ts` are shared by the sidebar and
  the menu, so the two can't drift apart.

## As built (phase 5)

- `web/content/docs/clients-and-matters.mdx` has a new "Moving around a matter" section: the
  menu groups, client link, matter switcher, Workspace menu, per-section addresses and old
  `?tab=` links, the tenant shown inside a client, and the menu in Search & Review and batch
  review.
- Matter sections are referred to as "X in the matter menu" instead of "the X tab" across
  `matter-jobs`, `matter-search`, `facts`, `entities`, `glossary`, `matter-definition`,
  `statistical-classification`, `metadata-definitions`, `review-batches` and `collections`.
  Real in-page tabs (Facts List/Timeline, collection tabs, Coding/Metadata) are unchanged.
- `matter-jobs.mdx` describes one page per job type instead of the picker and its saved
  selection. The Classification section is called **Classifiers**, matching the menu.
- Help-topic summaries (`src/lib/help-topics.ts`) needed no change.
