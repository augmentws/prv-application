export type CollectionSection = "documents" | "custodians" | "file-dates" | "processing";

const SECTION_PATHS: Record<CollectionSection, string> = {
  documents: "",
  custodians: "/custodians",
  "file-dates": "/file-dates",
  processing: "/processing",
};

/** The one place collection section URLs are built. */
export function collectionHref(clientId: string, collectionId: string, section: CollectionSection = "documents", params?: URLSearchParams | Record<string, string>) {
  const query = params ? new URLSearchParams(params).toString() : "";
  return `/app/clients/${clientId}/collections/${collectionId}${SECTION_PATHS[section]}${query ? `?${query}` : ""}`;
}

export function collectionSectionFromPath(pathname: string): CollectionSection | null {
  const match = /^\/app\/clients\/[^/]+\/collections\/[^/]+(\/[^?#]*)?$/.exec(pathname);
  if (!match) return null;
  const segments = (match[1] ?? "").split("/").filter(Boolean);
  if (segments.length > 1) return null;
  switch (segments[0] ?? "") {
    case "":
      return "documents";
    case "custodians":
    case "file-dates":
    case "processing":
      return segments[0] as CollectionSection;
    default:
      return null;
  }
}

/** True on a collection section page, where the collection shell draws the header. */
export function isCollectionSectionPage(clientId: string, collectionId: string, pathname: string): boolean {
  const path = pathname.replace(/\/$/, "");
  const section = collectionSectionFromPath(path);
  return section !== null && path === collectionHref(clientId, collectionId, section);
}
