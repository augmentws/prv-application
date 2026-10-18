import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";

import { CollectionShell } from "@/components/collection/collection-shell";

export const COLLECTION_BASE_PATH = "/app/clients/client-1/collections/collection-1";

/**
 * Renders a collection section inside the real collection layout for client-1/collection-1.
 * The calling test file mocks `next/navigation`, including `usePathname`, so the header matches the route.
 */
export function renderInCollection(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><CollectionShell clientId="client-1" collectionId="collection-1">{ui}</CollectionShell></QueryClientProvider>);
}
