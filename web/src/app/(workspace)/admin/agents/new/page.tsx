import type { Metadata } from "next";

import { CreateSystemAgentView } from "@/components/views/create-system-agent-view";

export const metadata: Metadata = { title: "Create agent" };

export default function NewAgentPage() {
  return <CreateSystemAgentView />;
}
