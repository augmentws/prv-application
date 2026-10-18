import { MatterShell } from "@/components/matter/matter-shell";

export default async function MatterLayout({ children, params }: LayoutProps<"/app/clients/[clientId]/matters/[matterId]">) {
  const { clientId, matterId } = await params;
  return <MatterShell clientId={clientId} matterId={matterId}>{children}</MatterShell>;
}
