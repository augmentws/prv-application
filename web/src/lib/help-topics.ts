export const helpTopics = {
  agents: {
    title: "Agent administration help",
    summary: "Learn how root administrators create, version, publish, suspend, and archive system agents.",
    href: "/docs/agents",
  },
  collections: {
    title: "Collections help",
    summary: "Learn how to browse, filter, and open imported evidence.",
    href: "/docs/collections",
  },
  metadata: {
    title: "Metadata definitions help",
    summary: "Learn how matter fields, groups, visibility, and reusable configuration work.",
    href: "/docs/metadata-definitions",
  },
  matterJobs: {
    title: "Matter jobs help",
    summary: "Learn how document selections are added to matters and how to follow background job progress.",
    href: "/docs/matter-jobs",
  },
  matterSearch: {
    title: "Matter search help",
    summary: "Learn how matter indexes, searches, filters, facets, and rebuilds work.",
    href: "/docs/matter-search",
  },
  matterDefinition: {
    title: "Matter Definition help",
    summary: "Learn how to draft reviewer guidance, work with the setup agent, approve changes, and publish a revision.",
    href: "/docs/matter-definition",
  },
  matterReview: {
    title: "Search and review help",
    summary: "Learn how to search a matter, filter results, read documents, and update review coding.",
    href: "/docs/matter-search#search-and-review-workspace",
  },
} as const;

export type HelpTopic = keyof typeof helpTopics;
