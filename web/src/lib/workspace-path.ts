const CLIENT_PATH = /^\/app\/clients\/([^/?#]+)(?:\/|$)/;
const MATTER_PATH = /^\/app\/clients\/([^/?#]+)\/matters\/([^/?#]+)(?:\/|$)/;
const COLLECTION_PATH = /^\/app\/clients\/([^/?#]+)\/collections\/([^/?#]+)(?:\/|$)/;

/** Client routes own their tenant: the client's tenant, not the sidebar selection, scopes the page. */
export function parseClientPath(pathname: string): { clientId: string } | null {
  const match = CLIENT_PATH.exec(pathname);
  return match ? { clientId: decodeURIComponent(match[1]) } : null;
}

export function parseMatterPath(pathname: string): { clientId: string; matterId: string } | null {
  const match = MATTER_PATH.exec(pathname);
  return match ? { clientId: decodeURIComponent(match[1]), matterId: decodeURIComponent(match[2]) } : null;
}

export function parseCollectionPath(pathname: string): { clientId: string; collectionId: string } | null {
  const match = COLLECTION_PATH.exec(pathname);
  return match ? { clientId: decodeURIComponent(match[1]), collectionId: decodeURIComponent(match[2]) } : null;
}

export type ResourcePath =
  | { kind: "matter"; clientId: string; matterId: string }
  | { kind: "collection"; clientId: string; collectionId: string };

/** The matter or collection a path is inside, which decides the sidebar's menu. */
export function parseResourcePath(pathname: string): ResourcePath | null {
  const matter = parseMatterPath(pathname);
  if (matter) return { kind: "matter", ...matter };
  const collection = parseCollectionPath(pathname);
  if (collection) return { kind: "collection", ...collection };
  return null;
}
