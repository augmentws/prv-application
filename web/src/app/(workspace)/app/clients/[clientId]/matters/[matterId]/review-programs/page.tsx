import type { Metadata } from "next";

import { ReviewProgramsSection } from "@/components/matter/sections/review-programs-section";

export const metadata: Metadata = { title: "Review programs" };

export default function MatterReviewProgramsPage() {
  return <ReviewProgramsSection />;
}
