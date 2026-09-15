import type { Metadata } from "next";

import { AgentDetailView } from "@/components/views/agent-detail-view";

export const metadata: Metadata = { title: "Agent" };

export default async function AgentPage({ params }: { params: Promise<{ agentId: string }> }) {
  const { agentId } = await params;
  return <AgentDetailView agentId={agentId} />;
}
