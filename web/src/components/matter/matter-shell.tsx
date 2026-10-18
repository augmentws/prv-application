"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { HelpLink } from "@/components/help-link";
import { MatterProvider, useMatter } from "@/components/matter/matter-context";
import { matterSectionLabel } from "@/components/matter/matter-sections";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { StatusBadge } from "@/components/status-badge";
import type { HelpTopic } from "@/lib/help-topics";
import { isMatterSectionPage, type MatterSection, matterHref, matterSectionFromPath } from "@/lib/matter-routes";

const SECTION_HELP: Partial<Record<MatterSection, HelpTopic>> = {
  definition: "matterDefinition",
  facts: "facts",
  "facts-timeline": "facts",
  entities: "entities",
  "entities-proposed": "entities",
  classification: "classification",
  jobs: "matterJobs",
  usage: "tokenUsage",
  "search-index": "matterSearch",
};

/** The matter layout: loads the matter once and draws the header for section pages. */
export function MatterShell({ clientId, matterId, children }: { clientId: string; matterId: string; children: ReactNode }) {
  return (
    <MatterProvider clientId={clientId} matterId={matterId}>
      <MatterFrame>{children}</MatterFrame>
    </MatterProvider>
  );
}

function MatterFrame({ children }: { children: ReactNode }) {
  const { clientId, matterId, client, matter } = useMatter();
  const pathname = usePathname();
  const section = matterSectionFromPath(pathname) ?? "overview";
  // Detail pages (a fact, an entity, a topic job, a classifier) draw their own header.
  if (!isMatterSectionPage(clientId, matterId, pathname)) return children;

  const definition = section === "definition";
  return (
    <div className={definition ? "matter-definition-page-frame" : undefined}>
      <div className={definition ? "matter-definition-page-header" : undefined}>
        <ResourcePageHeader
          breadcrumbs={<>
            <Link href="/app/clients" className="hover:text-foreground">Clients</Link>
            <ChevronRight className="size-4" />
            <Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.name}</Link>
            <ChevronRight className="size-4" />
            {section === "overview"
              ? <span aria-current="page" className="text-foreground">{matter.name}</span>
              : <>
                  <Link href={matterHref(clientId, matterId)} className="hover:text-foreground">{matter.name}</Link>
                  <ChevronRight className="size-4" />
                  <span aria-current="page" className="text-foreground">{matterSectionLabel(section)}</span>
                </>}
          </>}
          title={matter.name}
          leading={<>
            <HelpLink topic={SECTION_HELP[section] ?? "metadata"} />
            <StatusBadge status={matter.status} />
          </>}
        />
      </div>
      <div className={definition ? "matter-definition-page-content" : undefined}>{children}</div>
    </div>
  );
}
