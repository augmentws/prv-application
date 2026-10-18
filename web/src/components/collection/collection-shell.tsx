"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { CollectionProvider, useCollection } from "@/components/collection/collection-context";
import { collectionSectionLabel } from "@/components/collection/collection-sections";
import { HelpLink } from "@/components/help-link";
import { ResourcePageHeader } from "@/components/resource-page-header";
import { StatusBadge } from "@/components/status-badge";
import { collectionHref, collectionSectionFromPath, isCollectionSectionPage } from "@/lib/collection-routes";

/** The collection layout: loads the collection once and draws the header for section pages. Collections are deleted from the client page. */
export function CollectionShell({ clientId, collectionId, children }: { clientId: string; collectionId: string; children: ReactNode }) {
  return (
    <CollectionProvider clientId={clientId} collectionId={collectionId}>
      <CollectionFrame>{children}</CollectionFrame>
    </CollectionProvider>
  );
}

function CollectionFrame({ children }: { children: ReactNode }) {
  const { clientId, collectionId, client, collection } = useCollection();
  const pathname = usePathname();
  if (!isCollectionSectionPage(clientId, collectionId, pathname)) return children;

  const section = collectionSectionFromPath(pathname.replace(/\/$/, "")) ?? "documents";
  return (
    <>
      <ResourcePageHeader
        breadcrumbs={<>
          <Link href="/app/clients" className="hover:text-foreground">Clients</Link>
          <ChevronRight className="size-4" />
          <Link href={`/app/clients/${clientId}`} className="hover:text-foreground">{client.name}</Link>
          <ChevronRight className="size-4" />
          {section === "documents"
            ? <span aria-current="page" className="text-foreground">{collection.name}</span>
            : <>
                <Link href={collectionHref(clientId, collectionId)} className="hover:text-foreground">{collection.name}</Link>
                <ChevronRight className="size-4" />
                <span aria-current="page" className="text-foreground">{collectionSectionLabel(section)}</span>
              </>}
        </>}
        title={collection.name}
        leading={<>
          <HelpLink topic={section === "processing" ? "documentCleaner" : "collections"} />
          <StatusBadge status={collection.status} />
        </>}
      />
      {children}
    </>
  );
}
