const CLIENT_PATH = /^\/app\/clients\/([^/?#]+)(?:\/|$)/;
const MATTER_PATH = /^\/app\/clients\/([^/?#]+)\/matters\/([^/?#]+)(?:\/|$)/;

/** Client routes own their tenant: the client's tenant, not the sidebar selection, scopes the page. */
export function parseClientPath(pathname: string): { clientId: string } | null {
  const match = CLIENT_PATH.exec(pathname);
  return match ? { clientId: decodeURIComponent(match[1]) } : null;
}

export function parseMatterPath(pathname: string): { clientId: string; matterId: string } | null {
  const match = MATTER_PATH.exec(pathname);
  return match ? { clientId: decodeURIComponent(match[1]), matterId: decodeURIComponent(match[2]) } : null;
}
