import type { Metadata } from "next";

import { ClientView } from "@/components/views/client-view";

export const metadata: Metadata = { title: "Client" };

export default async function ClientPage({ params }: PageProps<"/app/clients/[clientId]">) {
  const { clientId } = await params;
  return <ClientView clientId={clientId} />;
}
