export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

function csrfToken() {
  if (typeof document === "undefined") return undefined;
  const entry = document.cookie.split("; ").find((cookie) => cookie.startsWith("pv_csrf="));
  return entry ? decodeURIComponent(entry.split("=").slice(1).join("=")) : undefined;
}

async function apiError(response: Response) {
  const payload = (await response.json().catch(() => null)) as
    | { message?: string; detail?: string; error?: { message?: string } }
    | null;
  return new ApiError(
    payload?.message ?? payload?.detail ?? payload?.error?.message ?? "Request failed.",
    response.status,
  );
}

export async function coreApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken();
    if (token) headers.set("x-csrf-token", token);
  }

  const response = await fetch(`/api/core${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) throw await apiError(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export interface CoreContent {
  bytes: Uint8Array;
  mediaType: string;
}

export async function coreApiContent(path: string): Promise<CoreContent> {
  const response = await fetch(`/api/core${path}`, {
    headers: { accept: "application/octet-stream" },
    cache: "no-store",
  });
  if (!response.ok) throw await apiError(response);
  return {
    bytes: new Uint8Array(await response.arrayBuffer()),
    mediaType: response.headers.get("content-type") ?? "application/octet-stream",
  };
}

export async function logout() {
  const token = csrfToken();
  await fetch("/api/auth/logout", {
    method: "POST",
    headers: token ? { "x-csrf-token": token } : undefined,
  });
}
