import type { Metadata } from "next";

import { AccessControlView } from "@/components/views/access-control-view";

export const metadata: Metadata = { title: "Access control" };

export default function AccessControlPage() {
  return <AccessControlView />;
}
