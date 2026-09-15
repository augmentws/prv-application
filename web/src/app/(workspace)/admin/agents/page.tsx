import type { Metadata } from "next";

import { SystemAgentsView } from "@/components/views/system-agents-view";

export const metadata: Metadata = { title: "Agents" };

export default function AgentsPage() {
  return <SystemAgentsView />;
}
