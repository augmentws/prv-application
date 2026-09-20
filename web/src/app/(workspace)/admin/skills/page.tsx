import type { Metadata } from "next";

import { SystemSkillsView } from "@/components/views/system-skills-view";

export const metadata: Metadata = { title: "Skills" };

export default function SkillsPage() {
  return <SystemSkillsView />;
}
