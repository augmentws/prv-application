import { CollectionShell } from "@/components/collection/collection-shell";

export default async function CollectionLayout({ children, params }: LayoutProps<"/app/clients/[clientId]/collections/[collectionId]">) {
  const { clientId, collectionId } = await params;
  return <CollectionShell clientId={clientId} collectionId={collectionId}>{children}</CollectionShell>;
}
