import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";

import { MatterShell } from "@/components/matter/matter-shell";

export const MATTER_BASE_PATH = "/app/clients/client-1/matters/matter-1";

/**
 * Renders a matter section inside the real matter layout for client-1/matter-1.
 * The calling test file mocks `next/navigation`, including `usePathname`, so the header matches the route.
 */
export function renderInMatter(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const tree = (child: ReactElement) => <QueryClientProvider client={client}><MatterShell clientId="client-1" matterId="matter-1">{child}</MatterShell></QueryClientProvider>;
  const view = render(tree(ui));
  return { ...view, rerender: (next: ReactElement) => view.rerender(tree(next)) };
}
